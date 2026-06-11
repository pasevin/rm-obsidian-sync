"""
config.py — Centralised configuration for rm-obsidian-sync.

Reads environment variables (and an optional .env file) and exposes them
as a typed :class:`Config` dataclass.  A module-level singleton
:data:`config` is created at import time so every other module can do::

    from rm_sync.config import config
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the working directory (or wherever the daemon is launched).
load_dotenv()


def _state_dir() -> Path:
    """Return (and create) the per-user state directory."""
    d = Path.home() / ".rm-obsidian-sync"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class Config:
    """All runtime settings, populated from environment variables."""

    # ------------------------------------------------------------------ #
    # rmfakecloud connection
    # ------------------------------------------------------------------ #
    rmfakecloud_url: str = field(
        default_factory=lambda: os.environ.get(
            "RMFAKECLOUD_URL", "http://localhost:3000"
        ).rstrip("/")
    )
    """Base URL of the rmfakecloud instance (no trailing slash)."""

    rmfakecloud_user: str = field(
        default_factory=lambda: os.environ.get("RMFAKECLOUD_USER", "")
    )
    """Username / e-mail used to log in to rmfakecloud."""

    rmfakecloud_pass: str = field(
        default_factory=lambda: os.environ.get("RMFAKECLOUD_PASS", "")
    )
    """Password for the rmfakecloud account."""

    # ------------------------------------------------------------------ #
    # Obsidian vault
    # ------------------------------------------------------------------ #
    vault_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("VAULT_PATH", str(Path.home() / "vault"))
        )
    )
    """Absolute path to the Obsidian vault root."""

    vault_rm_root: str = field(
        default_factory=lambda: os.environ.get(
            "VAULT_RM_ROOT", "Inbox/reMarkable"
        )
    )
    """Sub-path inside the vault where reMarkable notes are written."""

    # ------------------------------------------------------------------ #
    # Webhook server
    # ------------------------------------------------------------------ #
    webhook_port: int = field(
        default_factory=lambda: int(os.environ.get("WEBHOOK_PORT", "9090"))
    )
    """TCP port the webhook daemon listens on."""

    # ------------------------------------------------------------------ #
    # HWR — LLM Vision (primary, via OpenRouter)
    # ------------------------------------------------------------------ #
    openrouter_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", "")
    )
    """OpenRouter API key — enables LLM vision HWR (primary backend)."""

    llm_vision_model: str = field(
        default_factory=lambda: os.environ.get(
            "LLM_VISION_MODEL", "nvidia/nemotron-nano-12b-v2-vl:free"
        )
    )
    """OpenRouter model ID used for LLM vision HWR.
    Default: ``nvidia/nemotron-nano-12b-v2-vl:free`` (free, excellent handwriting)."""

    # ------------------------------------------------------------------ #
    # HWR — MyScript (secondary fallback)
    # ------------------------------------------------------------------ #
    myscript_app_key: str = field(
        default_factory=lambda: os.environ.get("MYSCRIPT_APP_KEY", "")
    )
    """MyScript iink Cloud application key."""

    myscript_hmac_key: str = field(
        default_factory=lambda: os.environ.get("MYSCRIPT_HMAC_KEY", "")
    )
    """MyScript iink Cloud HMAC key."""

    hwr_language: str = field(
        default_factory=lambda: os.environ.get("HWR_LANGUAGE", "en_US")
    )
    """BCP-47 language tag sent to the MyScript API (e.g. ``en_US``)."""

    # ------------------------------------------------------------------ #
    # rmfakecloud blob data volume (direct disk access for sync15)
    # ------------------------------------------------------------------ #
    rmfakecloud_data_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "RMFAKECLOUD_DATA_PATH",
                str(Path.home() / "rmfakecloud" / "data"),
            )
        )
    )
    """Path to the rmfakecloud data directory (the Docker volume bind-mount).

    The sync15 blob reader resolves documents directly from disk at:
    ``<rmfakecloud_data_path>/users/<user>/sync/``.

    Set this to the ``data/`` directory of your rmfakecloud Docker Compose
    project, e.g. ``/opt/rmfakecloud/data`` or ``./data`` resolved to an
    absolute path.
    """

    # ------------------------------------------------------------------ #
    # PDF conversion binaries (Obsidian → reMarkable upload pipeline)
    # ------------------------------------------------------------------ #
    pandoc_bin: str = field(
        default_factory=lambda: os.environ.get("PANDOC_BIN", "pandoc")
    )
    """Path or name of the ``pandoc`` binary.  Defaults to ``pandoc`` (PATH
    lookup).  Override if pandoc is installed to a non-standard location,
    e.g. ``/home/user/.local/bin/pandoc``."""

    wkhtmltopdf_bin: str = field(
        default_factory=lambda: os.environ.get("WKHTMLTOPDF_BIN", "wkhtmltopdf")
    )
    """Path or name of the ``wkhtmltopdf`` binary.  Defaults to
    ``wkhtmltopdf`` (PATH lookup).  Override for non-standard installs."""

    wkhtmltopdf_lib_path: str = field(
        default_factory=lambda: os.environ.get("WKHTMLTOPDF_LIB_PATH", "")
    )
    """Optional extra entry prepended to ``LD_LIBRARY_PATH`` when calling
    wkhtmltopdf.  Needed when using the statically-linked upstream build
    whose ``libwkhtmltox.so`` lives outside the system library path."""


    auth_state_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "AUTH_STATE_FILE",
                str(_state_dir() / "auth.json"),
            )
        )
    )
    """Path to the JSON file that stores device/user JWTs."""

    sync_state_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "SYNC_STATE_FILE",
                str(_state_dir() / "state.json"),
            )
        )
    )
    """Path to the JSON file that stores per-document sync state."""

    def validate(self) -> list[str]:
        """Return a list of human-readable warnings about missing settings."""
        warnings: list[str] = []
        if not self.rmfakecloud_url:
            warnings.append("RMFAKECLOUD_URL is not set")
        if not self.myscript_app_key:
            warnings.append("MYSCRIPT_APP_KEY is not set — HWR will be disabled")
        if not self.myscript_hmac_key:
            warnings.append("MYSCRIPT_HMAC_KEY is not set — HWR will be disabled")
        return warnings


# Module-level singleton — import and use directly.
config = Config()
