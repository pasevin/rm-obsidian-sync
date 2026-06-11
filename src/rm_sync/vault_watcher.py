"""
vault_watcher.py — Watch the Obsidian vault for changes and upload modified
notes to rmfakecloud via ObsidianUploader.

Uses ``watchfiles`` (already a daemon dependency) for efficient inotify-based
change detection.  Debounced: a 5-second quiet period after the last change
fires the upload, preventing redundant conversions while the user is still
typing.

Public API
----------
::

    watcher = VaultWatcher()
    await watcher.run()   # blocks; runs until cancelled
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from watchfiles import awatch, Change

from rm_sync.config import config
from rm_sync.obsidian_uploader import ObsidianUploader, _is_excluded

logger = logging.getLogger(__name__)

# Seconds of silence after the last change before triggering an upload.
# Prevents redundant conversions while the user is still typing.
_DEBOUNCE_SECONDS = 5


class VaultWatcher:
    """
    Watches the Obsidian vault directory tree and uploads changed Markdown notes.

    Change events are debounced: a note is queued on the first change event,
    then uploaded once ``_DEBOUNCE_SECONDS`` have passed with no further
    changes to that file.
    """

    def __init__(self) -> None:
        self._uploader = ObsidianUploader()
        # Maps absolute path string → asyncio.Task for the pending upload
        self._pending: dict[str, asyncio.Task] = {}

    async def run(self) -> None:
        """
        Start watching ``config.vault_path`` indefinitely.

        Cancels cleanly when the outer task is cancelled.
        """
        vault = config.vault_path
        logger.info("Vault watcher started — watching %s", vault)

        async for changes in awatch(str(vault)):
            for change_type, raw_path in changes:
                path = Path(raw_path)

                # Only process Markdown files
                if path.suffix.lower() != ".md":
                    continue

                # Skip excluded paths
                if _is_excluded(path):
                    continue

                # Skip deletions — nothing to upload
                if change_type == Change.deleted:
                    continue

                await self._schedule_upload(path)

    async def _schedule_upload(self, path: Path) -> None:
        """
        Schedule a debounced upload for *path*.

        If a pending task already exists for this path, cancel it and restart
        the debounce timer so rapid saves are batched into a single upload.
        """
        key = str(path)
        existing = self._pending.get(key)
        if existing and not existing.done():
            existing.cancel()

        task = asyncio.create_task(self._debounced_upload(path))
        self._pending[key] = task

    async def _debounced_upload(self, path: Path) -> None:
        """Wait for the debounce period then perform the upload."""
        try:
            await asyncio.sleep(_DEBOUNCE_SECONDS)
            if not path.exists():
                return  # deleted during debounce window
            logger.info("Vault change detected — uploading %s", path.name)
            await self._uploader.upload_note(path)
        except asyncio.CancelledError:
            pass  # superseded by a newer change — normal
        except Exception as exc:
            logger.error("Upload failed for %s: %s", path.name, exc)
        finally:
            self._pending.pop(str(path), None)
