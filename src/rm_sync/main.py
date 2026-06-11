"""
main.py — FastAPI application entry-point for the rm-obsidian-sync daemon.

Start the server with::

    uvicorn rm_sync.main:app --host 0.0.0.0 --port 9090

Or via the project helper (once installed)::

    python -m rm_sync.main
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rm_sync.config import config
from rm_sync.webhook import router as webhook_router

# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan context
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Startup / shutdown lifecycle handler.

    On startup:
    * Logs a configuration summary.
    * Validates the config and emits warnings for missing settings.
    * Attempts to fetch a user token to validate credentials — warns if
      this fails (the daemon still starts so the webhook can receive
      events once the user runs ``rm-register``).

    On shutdown:
    * Logs a clean shutdown message.
    """
    logger.info("Starting rm-obsidian-sync daemon …")
    logger.info("rmfakecloud URL : %s", config.rmfakecloud_url)
    logger.info("Vault path      : %s", config.vault_path)
    logger.info("Vault RM root   : %s", config.vault_rm_root)
    logger.info("Webhook port    : %d", config.webhook_port)
    logger.info("Auth state file : %s", config.auth_state_file)
    logger.info("Sync state file : %s", config.sync_state_file)

    # Emit config warnings
    for warning in config.validate():
        logger.warning("Configuration warning: %s", warning)

    # Validate credentials (non-fatal)
    try:
        from rm_sync.auth import get_user_token

        token = get_user_token()
        # Only log a prefix of the token for debugging
        logger.info("Auth OK — user token starts with: %s…", token[:12])
    except RuntimeError as exc:
        logger.warning(
            "Startup token validation failed (is device registered?): %s", exc
        )
    except Exception as exc:
        logger.warning("Startup token validation error: %s", exc)

    # Make sure vault root exists
    try:
        vault_rm_full = config.vault_path / config.vault_rm_root
        vault_rm_full.mkdir(parents=True, exist_ok=True)
        logger.info("Vault RM root ensured: %s", vault_rm_full)
    except OSError as exc:
        logger.warning("Could not create vault sub-directory: %s", exc)

    # Start the background polling loop (reMarkable → Obsidian)
    poll_task = asyncio.create_task(_poll_loop())

    # Start the vault watcher (Obsidian → reMarkable)
    from rm_sync.vault_watcher import VaultWatcher
    watcher = VaultWatcher()
    watch_task = asyncio.create_task(watcher.run())

    yield  # ← server is running

    poll_task.cancel()
    watch_task.cancel()
    for task in (poll_task, watch_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("rm-obsidian-sync daemon shutting down — goodbye.")


# ──────────────────────────────────────────────────────────────────────────────
# Background polling loop — detects new/changed documents and triggers sync
# ──────────────────────────────────────────────────────────────────────────────

_POLL_INTERVAL_SECONDS = 60  # check every minute


async def _poll_loop() -> None:
    """
    Background task: poll the rmfakecloud sync root every minute.

    Compares the current root hash against the last-seen value. If it has
    changed, lists all documents and syncs any notebooks modified since the
    last successful sync run.
    """
    from rm_sync.rm_client import RmClient
    from rm_sync.webhook import _sync_document

    last_root_hash: str = ""
    blob_root = config.rmfakecloud_data_path / "users" / config.rmfakecloud_user / "sync"
    root_file = blob_root / "root"

    logger.info("Starting sync poll loop (interval=%ds)", _POLL_INTERVAL_SECONDS)

    # Seed: on first start load existing state so we don't re-sync old notebooks.
    # Also track lastModified per doc so we detect updates to already-known docs.
    from rm_sync.vault_writer import load_state
    _seeded_ids: set[str] = set(load_state().keys())
    # lastModified stamp per doc — populated lazily on first successful list call
    _last_modified: dict[str, str] = {}
    if _seeded_ids:
        logger.info("Poll loop: seeded with %d already-synced doc IDs", len(_seeded_ids))
    last_root_hash = ""

    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

            if not root_file.exists():
                continue

            current_root = root_file.read_text().strip()
            if current_root == last_root_hash:
                continue

            logger.info(
                "Sync root changed: %s… → %s…",
                last_root_hash[:12] if last_root_hash else "(none)",
                current_root[:12],
            )
            last_root_hash = current_root

            # List all documents
            async with RmClient() as client:
                docs = await client.list_documents()

            notebooks = [
                d for d in docs
                if not d.get("isFolder", False)
                and d.get("type") == "notebook"
            ]

            # Determine which notebooks to sync:
            #   1. New doc — ID not in state or seeded set
            #   2. Updated doc — lastModified advanced past its last synced_at
            from rm_sync.vault_writer import load_state
            current_state = load_state()
            known_ids: set[str] = set(current_state.keys()) | _seeded_ids
            to_sync: list[dict] = []

            for doc in notebooks:
                doc_id: str = doc.get("id", "")
                if not doc_id:
                    continue
                last_mod: str = doc.get("lastModified", "")
                prev_mod: str = _last_modified.get(doc_id, "")

                if doc_id not in known_ids:
                    # Brand-new document never seen before
                    to_sync.append(doc)
                elif last_mod and last_mod != prev_mod:
                    # Known doc — check if it was modified after we last synced it
                    synced_at: str = current_state.get(doc_id, {}).get("synced_at", "")
                    if not synced_at or last_mod > synced_at:
                        to_sync.append(doc)

                # Always refresh our stamp for the next tick comparison
                _last_modified[doc_id] = last_mod

            n_new = sum(1 for d in to_sync if d["id"] not in known_ids)
            n_updated = len(to_sync) - n_new
            logger.info(
                "Poll: %d notebook(s) to sync (%d new, %d updated)",
                len(to_sync), n_new, n_updated,
            )

            for doc in to_sync:
                doc_id = doc.get("id")
                if doc_id:
                    # Prefer the rmfakecloud listing name — more accurate than
                    # the VissibleName baked into the .content file (often "Untitled").
                    api_name: str | None = doc.get("name") or None
                    logger.info(
                        "Poll: scheduling sync for '%s' (%s)",
                        api_name or "?", doc_id[:8],
                    )
                    asyncio.create_task(_sync_document(doc_id, display_name=api_name))

        except asyncio.CancelledError:
            logger.info("Poll loop cancelled.")
            raise
        except Exception as exc:
            logger.warning("Poll loop error (will retry): %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns:
        Configured :class:`~fastapi.FastAPI` instance.
    """
    app = FastAPI(
        title="rm-obsidian-sync",
        description="reMarkable ↔ Obsidian bidirectional sync daemon",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Allow same-host browser tooling to hit the health endpoint
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost", "http://127.0.0.1"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(webhook_router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {"service": "rm-obsidian-sync", "version": "0.1.0"}

    return app


# Module-level app instance (picked up by uvicorn)
app = create_app()


# ──────────────────────────────────────────────────────────────────────────────
# Dev runner
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run the daemon using the port from :attr:`~rm_sync.config.Config.webhook_port`."""
    uvicorn.run(
        "rm_sync.main:app",
        host="0.0.0.0",
        port=config.webhook_port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
