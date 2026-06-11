"""
rm_client.py — Async httpx client for the rmfakecloud REST API.

For sync15 documents (reMarkable Paper Pro firmware 3.x), content is stored
in a content-addressed blob tree on disk. This client reads those blobs
directly from the mounted data volume when the standard API endpoints are
unavailable, assembling a ZIP-equivalent structure for the parser.

The daemon also authenticates via UI login (email + password) for operations
that require credential-based access.

Usage::

    async with RmClient() as client:
        docs = await client.list_documents()
        data = await client.download_document_zip(docs[0]["id"])
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

import httpx

from rm_sync.config import config

logger = logging.getLogger(__name__)

# Cookie name used by rmfakecloud UI sessions
_COOKIE_NAME = ".Authrmfakecloud"


class RmClient:
    """
    Thin async wrapper around the rmfakecloud HTTP API.

    Authenticates via the UI login endpoint (email + password) for API
    operations. For sync15 document download, reads directly from the
    rmfakecloud data volume on disk, bypassing auth entirely.

    Can be used as an async context manager::

        async with RmClient() as client:
            docs = await client.list_documents()
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or config.rmfakecloud_url).rstrip("/")
        self._http: httpx.AsyncClient | None = None
        self._device_http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # Context-manager plumbing
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "RmClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP clients."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._device_http is not None:
            await self._device_http.aclose()
            self._device_http = None

    # ------------------------------------------------------------------ #
    # Authentication — UI session (for listing / polling)
    # ------------------------------------------------------------------ #

    async def _login(self) -> str:
        """
        Authenticate against the rmfakecloud UI login endpoint.

        Returns the session cookie value. Raises RuntimeError if login fails.
        """
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30,
            follow_redirects=True,
        ) as tmp:
            resp = await tmp.post(
                "/ui/api/login",
                json={
                    "email": config.rmfakecloud_user,
                    "password": config.rmfakecloud_pass,
                },
            )

        if resp.status_code != 200:
            raise RuntimeError(
                f"rmfakecloud login failed: HTTP {resp.status_code} — "
                "check RMFAKECLOUD_USER / RMFAKECLOUD_PASS in .env"
            )

        cookie = resp.cookies.get(_COOKIE_NAME)
        # Only use resp.text as a token fallback if it looks like a JWT
        # (starts with "eyJ") — never use full HTML response body as a credential
        token = resp.text.strip()
        if token and not token.startswith("eyJ"):
            token = ""

        if not cookie and not token:
            raise RuntimeError("rmfakecloud login returned no session credential")

        logger.info("rmfakecloud UI login OK")
        return cookie or token

    # ------------------------------------------------------------------ #
    # Authentication — device token (for document upload via /doc/v2/files)
    # ------------------------------------------------------------------ #

    def _load_user_token(self) -> str:
        """
        Load the user token from the device auth state file.

        The ``/doc/v2/files`` endpoint requires device-registered Bearer auth,
        not the UI session cookie.  The token was written to auth.json when
        ``rm-register`` paired the server with rmfakecloud.

        Returns:
            JWT user token string.

        Raises:
            RuntimeError: If the auth state file is missing or has no token.
        """
        import json as _json
        auth_file = config.auth_state_file
        if not auth_file.exists():
            raise RuntimeError(
                f"Device auth state not found at {auth_file} — "
                "run rm-register first"
            )
        data = _json.loads(auth_file.read_text())
        token = data.get("user_token") or data.get("UserToken") or ""
        if not token:
            raise RuntimeError("No user_token in auth state file — re-run rm-register")
        return token

    async def _get_device_client(self) -> httpx.AsyncClient:
        """
        Return (or lazily create) an httpx client authenticated with the
        device user token, used for document upload operations.
        """
        if self._device_http is None:
            token = self._load_user_token()
            self._device_http = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=120,
                follow_redirects=True,
            )
        return self._device_http

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Lazily create (or reuse) the httpx client with a valid session."""
        if self._http is None:
            credential = await self._login()
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                cookies={_COOKIE_NAME: credential},
                headers={"Authorization": f"Bearer {credential}"},
                timeout=60,
                follow_redirects=True,
            )
        return self._http

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        client = await self._ensure_client()
        resp = await client.get(path, **kwargs)
        resp.raise_for_status()
        return resp

    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        client = await self._ensure_client()
        resp = await client.post(path, **kwargs)
        resp.raise_for_status()
        return resp

    async def _put(self, path: str, **kwargs: Any) -> httpx.Response:
        client = await self._ensure_client()
        resp = await client.put(path, **kwargs)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------ #
    # Sync15 blob tree reader (direct disk access)
    # ------------------------------------------------------------------ #

    def _blob_base_path(self) -> Path:
        """Return the path to the rmfakecloud user sync blob directory."""
        user = config.rmfakecloud_user
        return config.rmfakecloud_data_path / "users" / user / "sync"

    def _read_blob(self, h: str) -> bytes:
        """Read a blob by its content hash from the local data volume."""
        path = self._blob_base_path() / h
        if not path.exists():
            raise FileNotFoundError(f"Blob not found: {h[:16]}…")
        return path.read_bytes()

    def _parse_index(self, content: str) -> list[tuple[str, str]]:
        """
        Parse a sync15 index file into (hash, filename) pairs.

        Index format (version 3)::

            3
            <hash>:<attr>:<filename>:<gen>:<size>
            ...

        Returns:
            List of (hash, filename) tuples.
        """
        entries: list[tuple[str, str]] = []
        lines = content.strip().splitlines()
        for line in lines[1:]:  # skip version line
            parts = line.split(":")
            if len(parts) < 3:
                continue
            blob_hash = parts[0]
            # Filename is the third field (may contain hyphens in uuid)
            file_name = parts[2]
            entries.append((blob_hash, file_name))
        return entries

    def _find_doc_hash(self, doc_id: str) -> str:
        """
        Walk the root index to find the blob hash for a given document UUID.

        Args:
            doc_id: Document UUID (e.g. ``bc470ed3-1464-…``).

        Returns:
            Blob hash string for the document's index file.

        Raises:
            KeyError: If the document is not found in the root index.
        """
        root_hash = self._read_blob("root").decode().strip()
        root_index = self._read_blob(root_hash).decode()

        for line in root_index.strip().splitlines()[1:]:
            parts = line.split(":")
            if len(parts) < 3:
                continue
            entry_hash = parts[0]
            entry_uuid = parts[2]
            if entry_uuid == doc_id:
                return entry_hash

        raise KeyError(f"Document {doc_id} not found in sync15 root index")

    async def _build_zip_from_blobs(self, doc_id: str) -> bytes:
        """
        Assemble a ZIP archive for *doc_id* by reading sync15 blobs directly.

        Reads the document index blob, then fetches each constituent file
        (content, metadata, per-page .rm stroke data) and packs them into a
        ZIP compatible with :func:`rm_sync.rm_parser.extract_metadata`.

        Args:
            doc_id: Document UUID.

        Returns:
            ZIP bytes.
        """
        # Locate document index blob
        doc_hash = await asyncio.get_event_loop().run_in_executor(
            None, self._find_doc_hash, doc_id
        )
        logger.info("Document %s → index blob %s…", doc_id[:8], doc_hash[:16])

        # Read and parse the document index
        doc_index_raw = await asyncio.get_event_loop().run_in_executor(
            None, self._read_blob, doc_hash
        )
        entries = self._parse_index(doc_index_raw.decode())
        logger.info("Document index has %d files", len(entries))

        # Assemble ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for blob_hash, file_name in entries:
                try:
                    blob_data = await asyncio.get_event_loop().run_in_executor(
                        None, self._read_blob, blob_hash
                    )
                    # Keep original filename from the index — the parser
                    # expects exactly "bc470ed3-….content" and
                    # "bc470ed3-…/<page>.rm" naming conventions.
                    archive_name = file_name

                    zf.writestr(archive_name, blob_data)
                    logger.debug("ZIP: added %s (%d bytes)", archive_name, len(blob_data))
                except Exception as exc:
                    logger.warning("Could not read blob %s (%s): %s", file_name, blob_hash[:16], exc)

        buf.seek(0)
        result = buf.read()
        logger.info("Assembled ZIP for %s: %d bytes", doc_id[:8], len(result))
        return result

    # ------------------------------------------------------------------ #
    # Public API — document listing
    # ------------------------------------------------------------------ #

    async def list_documents(self) -> list[dict[str, Any]]:
        """
        Return a flat list of all documents from rmfakecloud's UI API.

        Returns:
            Flat list of document/folder metadata dicts with keys:
            ``id``, ``name``, ``type``, ``lastModified``, ``isFolder``.
        """
        resp = await self._get("/ui/api/documents")
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("Could not parse document list JSON: %s", exc)
            return []

        if isinstance(data, dict):
            return self._flatten(data.get("Entries", []))
        if isinstance(data, list):
            return self._flatten(data)
        return []

    def _flatten(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Recursively flatten a nested document tree into a flat list."""
        result: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            result.append(entry)
            for key in ("children", "Children"):
                children = entry.get(key)
                if children:
                    result.extend(self._flatten(children))
        return result

    # ------------------------------------------------------------------ #
    # Public API — document download
    # ------------------------------------------------------------------ #

    async def download_document_zip(self, doc_id: str) -> bytes:
        """
        Download the full document archive for *doc_id* as a ZIP.

        For sync15 documents (Paper Pro, firmware 3.x): reads blobs directly
        from the rmfakecloud data volume, assigning clean archive names.

        Args:
            doc_id: UUID of the document to download.

        Returns:
            ZIP bytes compatible with :func:`rm_sync.rm_parser.extract_metadata`.
        """
        logger.info("Downloading document %s …", doc_id)
        try:
            return await self._build_zip_from_blobs(doc_id)
        except (FileNotFoundError, KeyError) as exc:
            logger.warning("Blob read failed (%s), falling back to API: %s", type(exc).__name__, exc)
            resp = await self._get(f"/documents/{doc_id}/content")
            return resp.content

    # ------------------------------------------------------------------ #
    # Public API — document upload
    # ------------------------------------------------------------------ #

    async def create_folder(
        self,
        name: str,
        parent_id: str | None = None,
    ) -> str:
        """
        Create a folder in rmfakecloud via ``POST /ui/api/folders``.

        Args:
            name:      Display name for the new folder.
            parent_id: UUID of the parent folder, or None / empty for root.

        Returns:
            UUID of the newly created folder.

        Raises:
            httpx.HTTPStatusError: If the API call fails.
        """
        payload: dict[str, str] = {"name": name}
        if parent_id:
            payload["parentId"] = parent_id

        resp = await self._post("/ui/api/folders", json=payload)
        data = resp.json()
        # rmfakecloud returns the document object; ID field varies by backend version
        folder_id = data.get("ID") or data.get("id") or data.get("Id") or ""
        logger.info("Created folder '%s' → %s", name, folder_id)
        return folder_id

    async def upload_document(
        self,
        file_path: Path,
        filename: str,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Upload a PDF file to rmfakecloud via the UI multipart endpoint.

        Uses ``POST /ui/api/documents/upload`` with ``multipart/form-data``,
        authenticated via the UI session cookie.  This is the only endpoint
        that works reliably for both sync10 and sync15 devices — the device
        API (``/doc/v2/files``) requires a short-lived device user token and
        uses the raw sync15 blob protocol.

        Args:
            file_path: Local path of the PDF to send.
            filename:  Display name shown on the reMarkable (without extension).
            folder_id: Optional parent folder UUID from :meth:`create_folder`.

        Returns:
            Parsed JSON response from the server.
        """
        pdf_bytes = file_path.read_bytes()
        logger.info("Uploading '%s' (%d bytes) …", filename, len(pdf_bytes))

        client = await self._ensure_client()

        # Build multipart form.
        # rmfakecloud derives the display name from the multipart filename
        # (stripping the .pdf extension). The `type` field ends up set to the
        # filename stem — a cosmetic rmfakecloud quirk that doesn't affect
        # document opening on the tablet.
        files = {"file": (f"{filename}.pdf", pdf_bytes, "application/pdf")}
        data: dict[str, str] = {}
        if folder_id:
            data["parent"] = folder_id

        resp = await client.post(
            "/ui/api/documents/upload",
            files=files,
            data=data,
        )
        resp.raise_for_status()

        try:
            return resp.json()
        except Exception:
            return {"status": "ok", "raw": resp.text}

    async def get_integrations(self) -> dict[str, Any]:
        """Return the current webhook/integration configuration."""
        resp = await self._get("/api/v1/integrations")
        try:
            return resp.json()
        except Exception:
            return {}

    async def set_webhook(self, webhook_url: str) -> dict[str, Any]:
        """
        Configure rmfakecloud to POST document events to *webhook_url*.

        Args:
            webhook_url: Public URL of this daemon's ``/webhook`` endpoint.

        Returns:
            Updated integration configuration.
        """
        payload = {"webhook": {"enabled": True, "url": webhook_url}}
        logger.info("Configuring webhook → %s", webhook_url)
        resp = await self._put("/api/v1/integrations", json=payload)
        try:
            return resp.json()
        except Exception:
            return {"status": "ok"}
