"""
obsidian_uploader.py — Convert Obsidian Markdown notes to PDF and upload them
to rmfakecloud so they appear on the reMarkable tablet.

Conversion pipeline
-------------------
1. pandoc  -- renders .md → standalone HTML (handles YAML frontmatter,
              wikilinks stripped, code blocks, tables, blockquotes)
2. wkhtmltopdf -- renders HTML → PDF (A4, clean margins)
3. RmClient.upload_document -- pushes PDF to rmfakecloud,
   which syncs it to the tablet automatically

Upload-state tracking
---------------------
State is persisted to ``~/.rm-obsidian-sync/upload_state.json``.
Each entry records the SHA-256 of the last-uploaded content; notes are
re-uploaded only when their content hash changes.

Exclusions
----------
Notes under ``Inbox/reMarkable/`` (our own sync target), AGENTS.md at vault
root, Obsidian config files, and any note whose frontmatter contains
``rm_exclude: true`` are skipped.

Public API
----------
::

    uploader = ObsidianUploader()
    await uploader.upload_changed()   # upload all changed notes
    await uploader.upload_note(path)  # force-upload a single note
    await uploader.seed_all()         # initial full-vault import
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rm_sync.config import config
from rm_sync.rm_client import RmClient

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_UPLOAD_STATE_FILE = Path.home() / ".rm-obsidian-sync" / "upload_state.json"

# Vault-relative paths (or glob patterns) to always skip
_EXCLUDE_PREFIXES = (
    "Inbox/reMarkable",   # our own sync target — reMarkable → Obsidian only
    "AGENTS.md",          # vault meta-config
    ".obsidian",          # Obsidian config
    ".git",               # git internals
    "_templates",         # note templates, not real content
)

# CSS injected into the pandoc HTML to make PDFs readable on e-ink displays
_EREADER_CSS = """
body {
    font-family: Georgia, serif;
    font-size: 14pt;
    line-height: 1.6;
    max-width: 700px;
    margin: 0 auto;
    color: #000;
    background: #fff;
}
h1 { font-size: 22pt; margin-top: 0; }
h2 { font-size: 18pt; }
h3 { font-size: 15pt; }
code, pre { font-family: "Courier New", monospace; font-size: 11pt; }
pre { background: #f5f5f5; padding: 8px; border-radius: 4px; }
blockquote { border-left: 3px solid #888; margin-left: 0; padding-left: 16px; color: #444; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ccc; padding: 6px 10px; }
th { background: #eee; }
img { max-width: 100%; }
"""


# ──────────────────────────────────────────────────────────────────────────────
# Upload state helpers
# ──────────────────────────────────────────────────────────────────────────────


def _load_upload_state() -> dict[str, Any]:
    """Load upload state from disk; returns empty dict on missing/corrupt file."""
    if _UPLOAD_STATE_FILE.exists():
        try:
            return json.loads(_UPLOAD_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read upload state: %s", exc)
    return {}


def _save_upload_state(state: dict[str, Any]) -> None:
    """Atomically persist upload state to disk."""
    _UPLOAD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _UPLOAD_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(_UPLOAD_STATE_FILE)


def _content_hash(path: Path) -> str:
    """SHA-256 of the note's file content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# Markdown preprocessing
# ──────────────────────────────────────────────────────────────────────────────


def _strip_obsidian_syntax(text: str) -> str:
    """
    Convert Obsidian-specific syntax to plain Markdown that pandoc handles.

    * ``[[Wikilink]]`` → plain text (link text only)
    * ``[[Wikilink|Alias]]`` → alias text
    * ``![[image.png]]`` → removed (images won't resolve on the server)
    * Frontmatter ``rm_exclude: true`` detection (caller reads separately)
    """
    # Remove image embeds entirely — paths won't resolve in PDF context
    text = re.sub(r'!\[\[.*?\]\]', '', text)
    # Convert [[Page|Alias]] → Alias
    text = re.sub(r'\[\[.*?\|(.*?)\]\]', r'\1', text)
    # Convert [[Page]] → Page
    text = re.sub(r'\[\[(.*?)\]\]', r'\1', text)
    return text


def _is_excluded(path: Path) -> bool:
    """Return True when *path* should not be uploaded to reMarkable."""
    vault = config.vault_path
    try:
        rel = str(path.relative_to(vault))
    except ValueError:
        return True  # outside vault — skip

    for prefix in _EXCLUDE_PREFIXES:
        if rel.startswith(prefix) or rel == prefix:
            return True
    return False


def _has_rm_exclude_flag(path: Path) -> bool:
    """Return True when the note's frontmatter contains ``rm_exclude: true``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.startswith("---"):
            fm_end = text.find("---", 3)
            if fm_end != -1:
                fm = text[3:fm_end]
                if re.search(r'^\s*rm_exclude\s*:\s*true\s*$', fm, re.MULTILINE):
                    return True
    except OSError:
        pass
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Conversion: Markdown → PDF
# ──────────────────────────────────────────────────────────────────────────────


def _note_title(path: Path, text: str) -> str:
    """
    Extract a display title from the note.

    Tries, in order:
    1. YAML frontmatter ``title:`` field.
    2. First ``# Heading`` in the body.
    3. Filename without extension.
    """
    # Frontmatter title
    if text.startswith("---"):
        fm_end = text.find("---", 3)
        if fm_end != -1:
            m = re.search(r'^title\s*:\s*(.+)$', text[3:fm_end], re.MULTILINE)
            if m:
                return m.group(1).strip().strip('"\'')

    # First H1
    m = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
    if m:
        return m.group(1).strip()

    return path.stem


def _slugify_rm(text: str) -> str:
    """Safe display name for the reMarkable document list."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    return re.sub(r"\s+", " ", text).strip()[:100] or "Note"


def _find_binary(name: str) -> Path:
    """
    Locate a binary using config overrides first, then PATH, then common paths.

    Checks in order:
    1. Config override (``PANDOC_BIN`` / ``WKHTMLTOPDF_BIN`` env vars)
    2. ``PATH`` shutil.which lookup
    3. ``~/.local/bin/<name>`` (common no-sudo user install location)
    4. ``/usr/local/bin/<name>``, ``/usr/bin/<name>``

    Args:
        name: Binary name (e.g. ``"pandoc"`` or ``"wkhtmltopdf"``).

    Returns:
        :class:`~pathlib.Path` to the binary.

    Raises:
        RuntimeError: If the binary cannot be found.
    """
    # Honour explicit config overrides first
    override = getattr(config, f"{name.replace('-', '_')}_bin", "")
    candidates = []
    if override and override != name:
        candidates.append(Path(override))

    which = shutil.which(name)
    if which:
        candidates.append(Path(which))

    candidates += [
        Path.home() / ".local" / "bin" / name,
        Path("/usr/local/bin") / name,
        Path("/usr/bin") / name,
    ]

    for p in candidates:
        if p.exists() and p.is_file():
            return p
    raise RuntimeError(
        f"{name} not found. Install it or set {name.upper().replace('-','_')}_BIN "
        f"in your .env file."
    )


def _convert_to_pdf(md_path: Path, tmp_dir: Path) -> Path:
    """
    Convert a Markdown note to PDF via pandoc → wkhtmltopdf.

    Args:
        md_path: Source ``.md`` file.
        tmp_dir: Writable temp directory for intermediate files.

    Returns:
        Path to the generated PDF.

    Raises:
        RuntimeError: If pandoc or wkhtmltopdf fails.
    """
    pandoc = _find_binary("pandoc")
    wk = _find_binary("wkhtmltopdf")

    text = md_path.read_text(encoding="utf-8", errors="replace")
    title = _note_title(md_path, text)
    cleaned = _strip_obsidian_syntax(text)

    # Write preprocessed markdown
    md_clean = tmp_dir / "note.md"
    md_clean.write_text(cleaned, encoding="utf-8")

    html_path = tmp_dir / "note.html"
    pdf_path = tmp_dir / "note.pdf"

    # pandoc: md → standalone HTML with inline CSS
    # Use --include-in-header to embed styles so wkhtmltopdf needs no
    # external file access (avoids ProtocolUnknownError in headless env).
    css_header = tmp_dir / "style.html"
    css_header.write_text(f"<style>{_EREADER_CSS}</style>")

    pandoc_cmd = [
        str(pandoc),
        str(md_clean),
        "-t", "html5",
        "--standalone",
        f"--metadata=title:{title}",
        "--include-in-header", str(css_header),
        "-o", str(html_path),
    ]
    result = subprocess.run(pandoc_cmd, capture_output=True, text=True, timeout=30, cwd=str(tmp_dir))
    if result.returncode != 0:
        raise RuntimeError(f"pandoc failed: {result.stderr.strip()}")

    # wkhtmltopdf: HTML → PDF
    # --disable-smart-shrinking keeps font sizes predictable on e-ink.
    # No --no-background / --disable-external-links — they cause
    # ProtocolUnknownError in headless (no DISPLAY) daemon environments.
    # Explicit env ensures LD_LIBRARY_PATH is set for the bundled libwkhtmltox.
    import os as _os
    wk_env = _os.environ.copy()
    if config.wkhtmltopdf_lib_path:
        wk_env["LD_LIBRARY_PATH"] = config.wkhtmltopdf_lib_path + ":" + wk_env.get("LD_LIBRARY_PATH", "")
    wk_cmd = [
        str(wk),
        "--quiet",
        "--page-size", "A4",
        "--margin-top", "15mm",
        "--margin-bottom", "15mm",
        "--margin-left", "15mm",
        "--margin-right", "15mm",
        "--encoding", "utf-8",
        "--disable-smart-shrinking",
        str(html_path),
        str(pdf_path),
    ]
    result = subprocess.run(wk_cmd, capture_output=True, text=True, timeout=60, cwd=str(tmp_dir), env=wk_env)
    if result.returncode != 0:
        raise RuntimeError(f"wkhtmltopdf failed: {result.stderr.strip()}")

    if not pdf_path.exists() or pdf_path.stat().st_size < 100:
        raise RuntimeError("wkhtmltopdf produced an empty PDF")

    return pdf_path


# ──────────────────────────────────────────────────────────────────────────────
# Folder structure mirroring
# ──────────────────────────────────────────────────────────────────────────────


def _vault_rel_folder(md_path: Path) -> list[str]:
    """
    Return the vault-relative folder chain for a note, as a list of name segments.

    E.g. ``vault/Work/OpenZeppelin/Projects/Foo.md``
    → ``["Work", "OpenZeppelin", "Projects"]``
    """
    vault = config.vault_path
    try:
        rel = md_path.relative_to(vault)
    except ValueError:
        return []
    # Drop the filename — keep only directory parts
    parts = list(rel.parts[:-1])
    # Skip hidden dirs
    return [p for p in parts if not p.startswith(".")]


# Root folder name on the reMarkable for all Obsidian uploads
_OBSIDIAN_ROOT_FOLDER = "Obsidian"


# ──────────────────────────────────────────────────────────────────────────────
# Main uploader class
# ──────────────────────────────────────────────────────────────────────────────


class ObsidianUploader:
    """
    Manages upload of Obsidian vault notes to rmfakecloud.

    All uploads are placed under a single top-level "Obsidian" folder on the
    tablet, with the vault directory structure mirrored as nested subfolders
    beneath it.  E.g. ``Work/OpenZeppelin/Foo.md`` → ``Obsidian/Work/OpenZeppelin/Foo``.

    Folder IDs are cached in-process to avoid redundant API calls during batch
    uploads.
    """

    def __init__(self) -> None:
        # Maps vault-relative folder path tuple → rmfakecloud folder UUID.
        # Key is a slash-joined path string, e.g. "" for root, "Work" for
        # Obsidian/Work, "Work/Projects" for Obsidian/Work/Projects.
        self._folder_cache: dict[str, str] = {}
        # Flat list of all docs fetched at session start — used for look-ups.
        self._docs_cache: list[dict] | None = None

    # ------------------------------------------------------------------ #
    # Folder mirroring
    # ------------------------------------------------------------------ #

    async def _get_docs(self, client: RmClient) -> list[dict]:
        """Fetch the document/folder list once per uploader instance."""
        if self._docs_cache is None:
            self._docs_cache = await client.list_documents()
        return self._docs_cache

    def _invalidate_docs_cache(self) -> None:
        """Force a fresh list on the next call (e.g. after creating a folder)."""
        self._docs_cache = None

    async def _get_or_create_folder(
        self,
        client: RmClient,
        name: str,
        parent_id: str | None,
    ) -> str:
        """
        Return the ID of a folder named *name* under *parent_id*, creating it
        if it doesn't exist.

        Args:
            client:    Authenticated RmClient.
            name:      Folder display name.
            parent_id: UUID of the parent folder, or None for root.

        Returns:
            UUID of the found or newly created folder.
        """
        docs = await self._get_docs(client)
        existing = next(
            (
                d for d in docs
                if (d.get("type") == "CollectionType" or d.get("isFolder"))
                and d.get("VissibleName", d.get("name", "")) == name
                and d.get("Parent", d.get("parent", "")) == (parent_id or "")
            ),
            None,
        )
        if existing:
            return existing.get("ID") or existing.get("id") or ""

        # Not found — create it and refresh the cache
        new_id = await client.create_folder(name, parent_id)
        self._invalidate_docs_cache()
        return new_id

    async def _ensure_folder(
        self, client: RmClient, parts: list[str]
    ) -> str:
        """
        Return the rmfakecloud folder ID for the full vault-relative path,
        ensuring the complete chain including the "Obsidian" root exists.

        Path hierarchy on the tablet:
            Obsidian/                       ← always created
            Obsidian/<parts[0]>/            ← top-level vault dir
            Obsidian/<parts[0]>/<parts[1]>/ ← nested, etc.

        Args:
            client: Authenticated RmClient instance.
            parts:  Vault-relative folder path segments (may be empty).

        Returns:
            rmfakecloud folder UUID for the deepest folder in the chain.
        """
        # Build the full chain: prepend the Obsidian root
        full_chain = [_OBSIDIAN_ROOT_FOLDER] + parts

        # Walk the chain, creating missing levels
        parent_id: str | None = None
        path_so_far = ""
        for segment in full_chain:
            path_so_far = f"{path_so_far}/{segment}" if path_so_far else segment
            cache_key = path_so_far

            if cache_key in self._folder_cache:
                parent_id = self._folder_cache[cache_key]
            else:
                parent_id = await self._get_or_create_folder(client, segment, parent_id)
                self._folder_cache[cache_key] = parent_id

        return parent_id or ""

    # ------------------------------------------------------------------ #
    # Single note upload
    # ------------------------------------------------------------------ #

    async def upload_note(self, md_path: Path, *, force: bool = False) -> bool:
        """
        Convert *md_path* to PDF and upload to rmfakecloud.

        Skips if:
        * the note is in the exclusion list,
        * it has ``rm_exclude: true`` in frontmatter,
        * content hash matches the last upload (unless *force=True*).

        Args:
            md_path: Absolute path to the ``.md`` file.
            force:   Upload even if content hash is unchanged.

        Returns:
            True if the note was uploaded, False if skipped.
        """
        if _is_excluded(md_path) or _has_rm_exclude_flag(md_path):
            logger.debug("Skipping excluded note: %s", md_path.name)
            return False

        state = _load_upload_state()
        key = str(md_path)
        current_hash = _content_hash(md_path)

        if not force and state.get(key, {}).get("hash") == current_hash:
            logger.debug("Skipping unchanged note: %s", md_path.name)
            return False

        text = md_path.read_text(encoding="utf-8", errors="replace")
        title = _note_title(md_path, text)
        display_name = _slugify_rm(title)
        folder_parts = _vault_rel_folder(md_path)

        rm_path = "/".join([_OBSIDIAN_ROOT_FOLDER] + folder_parts)
        logger.info("Uploading '%s' → %s/", display_name, rm_path)

        with tempfile.TemporaryDirectory(prefix="rm_upload_") as tmp:
            tmp_dir = Path(tmp)
            try:
                pdf_path = await asyncio.get_event_loop().run_in_executor(
                    None, _convert_to_pdf, md_path, tmp_dir
                )
            except Exception as exc:
                logger.error("PDF conversion failed for %s: %s", md_path.name, exc)
                return False

            async with RmClient() as client:
                folder_id = await self._ensure_folder(client, folder_parts)
                await client.upload_document(pdf_path, display_name, folder_id=folder_id)

        # Persist state
        state[key] = {
            "hash": current_hash,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "display_name": display_name,
        }
        _save_upload_state(state)
        logger.info("Uploaded '%s' ✓", display_name)
        return True

    # ------------------------------------------------------------------ #
    # Batch operations
    # ------------------------------------------------------------------ #

    async def upload_changed(self) -> tuple[int, int]:
        """
        Scan the vault and upload any note whose content hash has changed.

        Returns:
            ``(uploaded, skipped)`` counts.
        """
        vault = config.vault_path
        notes = [
            p for p in vault.rglob("*.md")
            if not _is_excluded(p)
        ]
        uploaded = skipped = 0
        for note in sorted(notes):
            result = await self.upload_note(note)
            if result:
                uploaded += 1
            else:
                skipped += 1
        logger.info("upload_changed: %d uploaded, %d skipped", uploaded, skipped)
        return uploaded, skipped

    async def seed_all(self) -> tuple[int, int]:
        """
        Force-upload every eligible vault note regardless of prior state.

        Intended for the initial import. Afterwards the watcher uses
        :meth:`upload_changed` for incremental updates.

        Returns:
            ``(uploaded, failed)`` counts.
        """
        vault = config.vault_path
        notes = [
            p for p in vault.rglob("*.md")
            if not _is_excluded(p)
            and not _has_rm_exclude_flag(p)
        ]
        logger.info("Seeding %d eligible notes to reMarkable …", len(notes))
        uploaded = failed = 0
        for note in sorted(notes):
            try:
                result = await self.upload_note(note, force=True)
                if result:
                    uploaded += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.error("Seed failed for %s: %s", note.name, exc)
                failed += 1
        logger.info("Seed complete: %d uploaded, %d failed", uploaded, failed)
        return uploaded, failed
