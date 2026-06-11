"""
vault_writer.py — Write reMarkable-sourced notes into an Obsidian vault.

Responsibilities
----------------
* Build the on-disk path for each note under the vault root.
* Detect write conflicts (vault note modified after last sync).
* Atomically write Markdown files with YAML frontmatter.
* Persist per-document sync state to ``~/.rm-obsidian-sync/state.json``.

Public API
----------
::

    path = resolve_note_path(doc_metadata, folder_path)
    if conflict_check(path, doc_id):
        backup_conflict(path)
    write_note(path, content, doc_metadata)
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rm_sync.config import config

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Sync-state helpers
# ──────────────────────────────────────────────────────────────────────────────


def load_state() -> dict[str, Any]:
    """
    Load the sync-state file from disk.

    Returns an empty dict if the file does not exist or cannot be parsed.

    State schema::

        {
            "<doc_id>": {
                "hash": "<sha256 of last written content>",
                "synced_at": "<ISO-8601 UTC timestamp>",
                "vault_path": "<absolute path>"
            },
            ...
        }
    """
    path: Path = config.sync_state_file
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read sync state %s: %s", path, exc)
    return {}


def save_state(state: dict[str, Any]) -> None:
    """
    Atomically write *state* to the sync-state file.

    Args:
        state: The full state mapping to persist.
    """
    path: Path = config.sync_state_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
    logger.debug("Sync state saved (%d entries)", len(state))


def _update_state(
    doc_id: str,
    content_hash: str,
    vault_path: Path,
    state: dict[str, Any] | None = None,
) -> None:
    """Update a single document's sync record."""
    if state is None:
        state = load_state()
    state[doc_id] = {
        "hash": content_hash,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "vault_path": str(vault_path),
    }
    save_state(state)


# ──────────────────────────────────────────────────────────────────────────────
# Path-resolution helpers
# ──────────────────────────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """
    Convert *text* into a safe filename fragment.

    * Normalises Unicode to NFC.
    * Strips characters that are forbidden on most filesystems.
    * Collapses whitespace to single spaces.
    * Trims to 200 characters to avoid PATH_MAX issues.
    """
    text = unicodedata.normalize("NFC", text)
    # Remove characters that are unsafe in filenames
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200] or "Untitled"


def resolve_note_path(
    doc_metadata: dict[str, Any],
    folder_path: str = "",
) -> Path:
    """
    Build the absolute vault path for a note.

    Each notebook gets its own subdirectory so PNGs stay contained and the
    sidebar shows a clean folder instead of a flat file dump.  The path is::

        <vault_path>/<vault_rm_root>[/<folder_path>]/<note_name>/<note_name>.md

    The UUID is stored in frontmatter only — never in the filename or folder.
    If two notebooks share the same display name the second one gets a numeric
    suffix (``<name> 2``, ``<name> 3``, …) so they don't collide.

    Args:
        doc_metadata:
            Dict with at least ``"name"`` (str) and optionally ``"id"`` (str).
        folder_path:
            Slash-separated rmfakecloud folder chain to mirror inside the vault.

    Returns:
        Absolute :class:`pathlib.Path` for the ``.md`` file.
    """
    note_name = _slugify(doc_metadata.get("name", "Untitled"))
    doc_id = doc_metadata.get("id", "")

    parts = [str(config.vault_path), config.vault_rm_root]
    if folder_path.strip("/"):
        for part in folder_path.strip("/").split("/"):
            parts.append(_slugify(part))

    # Deduplicate: if a same-named folder exists for a *different* doc ID,
    # append a numeric suffix so they don't collide.
    base_dir = Path(*parts)
    folder_name = _resolve_unique_folder_name(base_dir, note_name, doc_id)

    parts.append(folder_name)
    parts.append(f"{folder_name}.md")
    return Path(*parts)


def _resolve_unique_folder_name(base_dir: Path, name: str, doc_id: str) -> str:
    """
    Return a folder name that is either:

    * *name* — if no sibling folder with that name exists, or if the existing
      folder already belongs to *doc_id* (detected via a ``notebook_id`` match
      in the incumbent ``.md`` file).
    * ``<name> 2``, ``<name> 3``, … — if the name is taken by a different doc.

    Args:
        base_dir: Parent directory to look in (e.g. ``vault/Inbox/reMarkable``).
        name:     Desired display-name folder (already slugified).
        doc_id:   UUID of the document being resolved.

    Returns:
        Safe folder name string.
    """
    candidate = name
    counter = 2
    while True:
        folder = base_dir / candidate
        if not folder.exists():
            return candidate  # free slot

        # Check whether this folder already belongs to our doc
        incumbent_md = folder / f"{candidate}.md"
        if incumbent_md.exists():
            try:
                text = incumbent_md.read_text(encoding="utf-8")
                if doc_id and doc_id in text:
                    return candidate  # same doc — reuse
            except OSError:
                pass

        # Taken by a different doc — try the next suffix
        candidate = f"{name} {counter}"
        counter += 1


# ──────────────────────────────────────────────────────────────────────────────
# Conflict detection
# ──────────────────────────────────────────────────────────────────────────────


def conflict_check(path: Path, doc_id: str) -> bool:
    """
    Return ``True`` when *path* has been modified after the last sync.

    If the note file does not yet exist, or there is no prior sync record,
    no conflict is detected.

    Args:
        path:   Vault path of the note.
        doc_id: Document UUID (used to look up last-sync timestamp).

    Returns:
        ``True`` if a conflict is detected and the existing file should be
        backed up before writing.
    """
    if not path.exists():
        return False

    state = load_state()
    entry = state.get(doc_id)
    if not entry:
        return False

    last_synced_str: str | None = entry.get("synced_at")
    if not last_synced_str:
        return False

    try:
        last_synced = datetime.fromisoformat(last_synced_str)
    except ValueError:
        return False

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if mtime > last_synced:
        logger.info(
            "Conflict detected for %s: file mtime %s > last synced %s",
            path,
            mtime.isoformat(),
            last_synced.isoformat(),
        )
        return True

    return False


def backup_conflict(path: Path) -> Path:
    """
    Rename *path* to a timestamped conflict copy.

    The backup is named::

        <stem>.conflict-<YYYYMMDDTHHMMSS>.md

    Args:
        path: Vault path of the note to back up.

    Returns:
        The new path of the backup file.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    stem = path.stem
    backup_path = path.with_name(f"{stem}.conflict-{ts}.md")
    os.rename(path, backup_path)
    logger.info("Conflict backup: %s → %s", path.name, backup_path.name)
    return backup_path


# ──────────────────────────────────────────────────────────────────────────────
# Frontmatter
# ──────────────────────────────────────────────────────────────────────────────


def _build_frontmatter(doc_metadata: dict[str, Any]) -> str:
    """
    Build a YAML frontmatter block for a note.

    Args:
        doc_metadata: Dict with document info (id, name, …).

    Returns:
        Multi-line string including the ``---`` delimiters.
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    synced_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    doc_id = doc_metadata.get("id", doc_metadata.get("ID", ""))

    lines = [
        "---",
        f"created: {date_str}",
        "source: remarkable",
        f"notebook_id: {doc_id}",
        f"synced: {synced_str}",
        "tags:",
        "  - remarkable",
        "---",
        "",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Image attachment helpers
# ──────────────────────────────────────────────────────────────────────────────


def _image_stem(doc_metadata: dict[str, Any]) -> str:
    """Return the base filename prefix used for page PNG images.

    PNGs live inside the notebook's own subdirectory, so a simple ``page``
    prefix is sufficient — no UUID needed for uniqueness.
    """
    return "page"


def write_page_images(
    directory: Path,
    doc_metadata: dict[str, Any],
    page_images: list[bytes],
) -> list[str]:
    """
    Write per-page PNG files into *directory*.

    Files are named ``<name>-<id[:8]>-page-<N>.png`` (1-indexed).
    Empty ``bytes`` entries are skipped (no file written, empty string in list).

    Args:
        directory:    The directory where the ``.md`` note lives.
        doc_metadata: Document metadata (must contain ``"name"`` and ``"id"``).
        page_images:  Ordered list of raw PNG bytes, one per page.

    Returns:
        List of filenames (basename only) in page order.  Empty string for
        pages where no PNG was written (empty bytes).
    """
    stem = _image_stem(doc_metadata)
    filenames: list[str] = []
    # PNGs go into a raw/ subfolder — keeps the note directory clean and
    # prevents the sidebar from showing attachments alongside the note.
    raw_dir = directory / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i, png_bytes in enumerate(page_images, start=1):
        filename = f"{stem}-page-{i}.png"
        if png_bytes:
            img_path = raw_dir / filename
            try:
                img_path.write_bytes(png_bytes)
                logger.debug("Wrote page image: %s (%d bytes)", img_path, len(png_bytes))
            except OSError as exc:
                logger.error("Failed to write image %s: %s", img_path, exc)
                filename = ""
        else:
            filename = ""
        filenames.append(filename)
    return filenames


def _build_body_with_images(
    page_texts: list[str],
    img_filenames: list[str],
) -> str:
    """
    Build the Markdown body that interleaves ``![[img]]`` tags above each
    page's text, joined with ``\\n\\n---\\n\\n`` between pages.

    Each page block is::

        ![[name-id-page-N.png]]

        page text

    Pages are separated by ``---`` (no trailing separator after last page).

    Args:
        page_texts:    Recognised text per page (same length as img_filenames).
        img_filenames: PNG filename (or empty string) per page.

    Returns:
        Multi-page Markdown body string.
    """
    sections: list[str] = []
    n = max(len(page_texts), len(img_filenames))
    for i in range(n):
        img = img_filenames[i] if i < len(img_filenames) else ""
        text = page_texts[i] if i < len(page_texts) else ""
        if img:
            block = f"![[{img}]]\n\n{text}"
        else:
            block = text
        sections.append(block)
    return "\n\n---\n\n".join(sections)


# ──────────────────────────────────────────────────────────────────────────────
# Atomic write
# ──────────────────────────────────────────────────────────────────────────────


def write_note(
    path: Path,
    content: str,
    doc_metadata: dict[str, Any],
    *,
    page_texts: list[str] | None = None,
    page_images: list[bytes] | None = None,
) -> None:
    """
    Atomically write a Markdown note to *path*.

    Prepends YAML frontmatter and handles conflict backup automatically.

    When *page_texts* and *page_images* are supplied the body is built by
    :func:`_build_body_with_images`, which writes per-page PNG attachments
    (``<name>-<id[:8]>-page-<N>.png``) next to the ``.md`` file and embeds
    Obsidian wikilinks above the corresponding page text.  The legacy
    *content* argument is ignored in that case.

    The write strategy is:

    1. Check for a conflict (existing file modified after last sync).
    2. If conflict, back up the existing file.
    3. Write PNG attachments to the note's parent directory.
    4. Write to ``<path>.tmp``.
    5. Rename ``<path>.tmp`` → ``<path>``.
    6. Update the sync-state file.

    Args:
        path:          Destination ``.md`` path inside the vault.
        content:       Plain text / Markdown body (used only when *page_texts*
                       is ``None``).
        doc_metadata:  Document metadata dict (must contain ``"id"``).
        page_texts:    Per-page recognised text.  When supplied together with
                       *page_images* the structured body is used instead of
                       *content*.
        page_images:   Per-page raw PNG bytes.  Empty ``bytes`` entries produce
                       no file but still count as a page.
    """
    import hashlib

    doc_id: str = doc_metadata.get("id", doc_metadata.get("ID", ""))
    note_name = doc_metadata.get("name", "Untitled")

    # Conflict guard
    if conflict_check(path, doc_id):
        backup_conflict(path)

    # Build body
    if page_texts is not None and page_images is not None:
        img_filenames = write_page_images(path.parent, doc_metadata, page_images)
        body = _build_body_with_images(page_texts, img_filenames)
    else:
        body = content

    # Compose the full note
    frontmatter = _build_frontmatter(doc_metadata)
    heading = f"# {note_name}\n\n"
    full_content = frontmatter + heading + body

    # Atomic write
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(full_content, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.error("Failed to write note %s: %s", path, exc)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

    # Persist sync state
    content_hash = hashlib.sha256(full_content.encode()).hexdigest()
    _update_state(doc_id, content_hash, path)

    logger.info("Note written: %s (%d chars)", path, len(full_content))
