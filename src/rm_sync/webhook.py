"""
webhook.py — FastAPI router for the rmfakecloud webhook integration.

Endpoints
---------
POST /webhook
    Receives multipart/form-data events from rmfakecloud.
    Extracts document IDs from the ``data`` field and triggers background
    sync tasks for each.

GET /health
    Returns a JSON health-check response with sync statistics.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Form, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from rm_sync.hwr_client import recognize_page_result
from rm_sync.notebook_content import (
    NotebookPageContent,
    filter_empty_pages,
    notebook_has_real_content,
)
from rm_sync.rm_client import RmClient
from rm_sync.rm_parser import extract_metadata, parse_rmdoc_pages
from rm_sync.stroke_renderer import render_page_to_png
from rm_sync.vault_writer import mark_note_skipped, resolve_note_path, write_note
from rm_sync.config import config

logger = logging.getLogger(__name__)

router = APIRouter()

_sync_stats: dict[str, Any] = {
    "last_sync": None,
    "pending": 0,
    "errors": [],
    "total_synced": 0,
}
_stats_lock = asyncio.Lock()


def _build_unparseable_page_notice(
    page_number: int,
    page_id: str,
    file_format: str,
    reason: str,
) -> str:
    """Return visible Markdown for a page blob that could not be parsed."""
    return (
        f"*Page {page_number} ({page_id}) could not be parsed "
        f"[{file_format}]: {reason}*"
    )


async def _record_error(msg: str) -> None:
    async with _stats_lock:
        _sync_stats["errors"].append(
            {"time": datetime.now(timezone.utc).isoformat(), "message": msg}
        )
        # Keep only the last 50 errors
        _sync_stats["errors"] = _sync_stats["errors"][-50:]


async def _record_sync_complete() -> None:
    async with _stats_lock:
        _sync_stats["last_sync"] = datetime.now(timezone.utc).isoformat()
        _sync_stats["total_synced"] = _sync_stats.get("total_synced", 0) + 1
        p = _sync_stats["pending"]
        _sync_stats["pending"] = max(0, p - 1)


async def _increment_pending() -> None:
    async with _stats_lock:
        _sync_stats["pending"] = _sync_stats.get("pending", 0) + 1


# ──────────────────────────────────────────────────────────────────────────────
# Document ID extraction
# ──────────────────────────────────────────────────────────────────────────────


def _extract_doc_ids(destinations: list[dict[str, Any]]) -> list[str]:
    """
    Extract document UUIDs from the webhook ``destinations`` array.

    Tries ``id``, ``ID``, and ``documentId`` keys defensively.

    Args:
        destinations: List of destination objects from the webhook payload.

    Returns:
        De-duplicated list of document UUID strings.
    """
    ids: list[str] = []
    for dest in destinations:
        for key in ("id", "ID", "documentId", "document_id"):
            val = dest.get(key)
            if val and isinstance(val, str):
                ids.append(val)
                break
    # De-duplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for doc_id in ids:
        if doc_id not in seen:
            seen.add(doc_id)
            unique.append(doc_id)
    return unique


# ──────────────────────────────────────────────────────────────────────────────
# Background sync task
# ──────────────────────────────────────────────────────────────────────────────


async def _sync_document(
    doc_id: str,
    display_name: str | None = None,
    doc_size: int = 0,
) -> None:
    """
    Full sync pipeline for a single document:

    1. Download the .rmdoc from rmfakecloud.
    2. Extract metadata; optionally override the display name with the value
       from the rmfakecloud document list (more accurate than .content VissibleName).
    3. If it is a notebook, parse strokes and run HWR.
    4. Write/update the vault note.

    Args:
        doc_id:       Document UUID.
        display_name: Optional display name from the rmfakecloud listing. When
                      provided, overrides the ``name`` value embedded in the
                      .content file (which often reads ``"Untitled"``).
        doc_size:     Size reported by rmfakecloud, persisted so blank notebooks
                      are still tracked for later change detection.

    All errors are caught internally so one bad document doesn't break
    the background-task runner.
    """
    logger.info("Starting sync for document %s", doc_id)
    try:
        async with RmClient() as client:
            # ---------------------------------------------------------------- #
            # 1. Download
            # ---------------------------------------------------------------- #
            rmdoc_bytes = await client.download_document_zip(doc_id)

            # ---------------------------------------------------------------- #
            # 2. Metadata — prefer the API display name over .content VissibleName
            # ---------------------------------------------------------------- #
            metadata = extract_metadata(rmdoc_bytes)
            if display_name:
                metadata["name"] = display_name
            file_type = metadata.get("fileType", "notebook")
            doc_name = metadata.get("name", "Untitled")

            logger.info(
                "Document %s: name=%r fileType=%s pages=%d",
                doc_id,
                doc_name,
                file_type,
                len(metadata.get("pages", [])),
            )

            # Add the doc_id into metadata so vault_writer can access it
            metadata["id"] = doc_id
            metadata["size"] = doc_size

            # ---------------------------------------------------------------- #
            # 3. HWR + stroke rendering (notebooks only)
            # ---------------------------------------------------------------- #
            page_texts: list[str] = []
            page_images: list[bytes] = []

            if file_type not in ("notebook", None, ""):
                logger.info(
                    "Skipping HWR for document %s (fileType=%s)", doc_id, file_type
                )
                page_texts = [f"*This note is a {file_type} — stroke recognition skipped.*"]
                page_images = [b""]
            else:
                parsed_pages = parse_rmdoc_pages(rmdoc_bytes)
                prepared_pages: list[NotebookPageContent] = []

                for i, parsed_page in enumerate(parsed_pages):
                    logger.info(
                        "Inspecting page %d/%d for %s …",
                        i + 1, len(parsed_pages), doc_id,
                    )
                    if parsed_page.parse_error:
                        logger.warning(
                            "Notebook %s (%s) page %s could not be parsed (%s): %s",
                            doc_name,
                            doc_id[:8],
                            parsed_page.page_id,
                            parsed_page.file_format,
                            parsed_page.parse_error,
                        )
                        prepared_pages.append(
                            NotebookPageContent(
                                recognized_text=_build_unparseable_page_notice(
                                    i + 1,
                                    parsed_page.page_id,
                                    parsed_page.file_format,
                                    parsed_page.parse_error,
                                )
                            )
                        )
                        continue

                    if not parsed_page.strokes and not parsed_page.typed_text.strip():
                        logger.info(
                            "Page %d/%d for %s is blank — no strokes or typed text",
                            i + 1, len(parsed_pages), doc_id,
                        )
                        prepared_pages.append(NotebookPageContent())
                        continue

                    png_bytes = b""
                    recognized_text = ""
                    if parsed_page.strokes:
                        logger.info(
                            "Rendering + recognising page %d/%d for %s …",
                            i + 1, len(parsed_pages), doc_id,
                        )
                        try:
                            png_bytes = await asyncio.get_event_loop().run_in_executor(
                                None, render_page_to_png, parsed_page.strokes
                            )
                        except Exception as exc:
                            logger.error("PNG render failed page %d: %s", i + 1, exc)
                            png_bytes = b""

                        try:
                            recognition = await recognize_page_result(parsed_page.strokes)
                            recognized_text = recognition.text
                            if recognized_text:
                                logger.info(
                                    "Page %d/%d for %s recognised via %s",
                                    i + 1,
                                    len(parsed_pages),
                                    doc_id,
                                    recognition.source_label(),
                                )
                        except Exception as exc:
                            logger.error("HWR failed page %d: %s", i + 1, exc)
                            recognized_text = ""
                    else:
                        logger.info(
                            "Page %d/%d for %s has typed text only — skipping render + HWR",
                            i + 1, len(parsed_pages), doc_id,
                        )

                    prepared_pages.append(
                        NotebookPageContent(
                            strokes=parsed_page.strokes,
                            typed_text=parsed_page.typed_text,
                            recognized_text=recognized_text,
                            image_bytes=png_bytes,
                        )
                    )

                pages_for_note = filter_empty_pages(
                    prepared_pages,
                    skip_empty_pages=config.skip_empty_notebooks,
                )
                if config.skip_empty_notebooks and not notebook_has_real_content(pages_for_note):
                    mark_note_skipped(doc_id, doc_size=doc_size)
                    await _record_sync_complete()
                    logger.info(
                        "Skipping empty notebook %s (%s) — no strokes or text found",
                        doc_name,
                        doc_id[:8],
                    )
                    return

                if not pages_for_note and not parsed_pages:
                    page_texts = ["*No strokes found in document.*"]
                    page_images = [b""]
                else:
                    page_texts = [page.body_text() for page in pages_for_note]
                    page_images = [page.image_bytes for page in pages_for_note]

            # ---------------------------------------------------------------- #
            # 4. Write vault note (with per-page PNG attachments)
            # ---------------------------------------------------------------- #
            note_path = resolve_note_path(metadata, folder_path="")
            write_note(
                note_path,
                "",          # legacy positional arg; body is built from page_texts
                metadata,
                page_texts=page_texts,
                page_images=page_images,
            )

        await _record_sync_complete()
        logger.info("Sync complete for document %s → %s", doc_id, note_path)

    except Exception as exc:
        msg = f"Sync failed for {doc_id}: {exc}"
        logger.error(msg, exc_info=True)
        await _record_error(msg)
        # Still decrement pending so the counter doesn't get stuck
        async with _stats_lock:
            _sync_stats["pending"] = max(0, _sync_stats.get("pending", 0) - 1)


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/webhook", status_code=202)
async def receive_webhook(
    background_tasks: BackgroundTasks,
    data: str = Form(...),
    attachment: UploadFile | None = File(default=None),
) -> JSONResponse:
    """
    Receive a document-sync event from rmfakecloud.

    rmfakecloud sends ``multipart/form-data`` with:

    * ``data`` — JSON string of the event payload.
    * ``attachment`` — optional PNG thumbnail (ignored).

    The handler extracts document IDs from ``destinations``, schedules
    async sync tasks, and immediately returns ``202 Accepted``.
    """
    logger.info("Webhook received (payload size=%d chars)", len(data))

    # Parse the JSON payload
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in webhook data field: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON in 'data' field")

    destinations: list[dict[str, Any]] = payload.get("destinations", [])
    if not isinstance(destinations, list):
        logger.warning("Webhook payload 'destinations' is not a list")
        destinations = []

    doc_ids = _extract_doc_ids(destinations)
    logger.info("Webhook triggered sync for doc_ids: %s", doc_ids)

    if not doc_ids:
        logger.warning(
            "No document IDs found in webhook payload — nothing to sync"
        )
        return JSONResponse(
            {"status": "accepted", "syncing": 0},
            status_code=202,
        )

    for doc_id in doc_ids:
        await _increment_pending()
        background_tasks.add_task(_sync_document, doc_id)

    return JSONResponse(
        {"status": "accepted", "syncing": len(doc_ids)},
        status_code=202,
    )


@router.get("/health")
async def health_check() -> JSONResponse:
    """
    Return the current health and sync statistics of the daemon.

    Response body::

        {
            "status": "ok",
            "last_sync": "<ISO-8601 or null>",
            "pending": <int>,
            "total_synced": <int>,
            "errors": [{"time": "...", "message": "..."}]
        }
    """
    async with _stats_lock:
        snapshot = dict(_sync_stats)

    return JSONResponse(
        {
            "status": "ok",
            **snapshot,
        }
    )


# ──────────────────────────────────────────────────────────────────────────────
# Obsidian → reMarkable upload routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/upload/seed", status_code=202)
async def seed_vault(background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Trigger a full initial upload of all eligible Obsidian vault notes
    to rmfakecloud.  Runs as a background task; returns immediately.

    Use this once after first setup to populate the tablet with all
    existing vault content.
    """
    from rm_sync.obsidian_uploader import ObsidianUploader

    async def _run_seed() -> None:
        uploader = ObsidianUploader()
        uploaded, failed = await uploader.seed_all()
        logger.info("Seed complete — uploaded=%d failed=%d", uploaded, failed)

    background_tasks.add_task(_run_seed)
    return JSONResponse({"status": "accepted", "action": "seed"}, status_code=202)


@router.post("/upload/sync", status_code=202)
async def sync_vault(background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Upload all Obsidian vault notes that have changed since the last upload.
    Runs as a background task; returns immediately.
    """
    from rm_sync.obsidian_uploader import ObsidianUploader

    async def _run_sync() -> None:
        uploader = ObsidianUploader()
        uploaded, skipped = await uploader.upload_changed()
        logger.info("Vault sync complete — uploaded=%d skipped=%d", uploaded, skipped)

    background_tasks.add_task(_run_sync)
    return JSONResponse({"status": "accepted", "action": "sync"}, status_code=202)

