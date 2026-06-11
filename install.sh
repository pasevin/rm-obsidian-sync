#!/usr/bin/env bash
# install.sh — set up rm-obsidian-sync as a systemd user service
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# What it does:
#   1. Creates a Python virtual environment with uv (or falls back to venv)
#   2. Installs the package in editable mode
#   3. Installs the systemd user service and enables it on login
#
# Prerequisites: Python 3.11+, uv (https://github.com/astral-sh/uv) or pip,
#                and a filled-in .env file (copy from .env.example).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="rm-obsidian-sync"
SERVICE_FILE="$REPO_DIR/deploy/${SERVICE_NAME}.service"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "==> rm-obsidian-sync installer"
echo "    Repo: $REPO_DIR"
echo ""

# ── 1. Check .env ─────────────────────────────────────────────────────────────
if [[ ! -f "$REPO_DIR/.env" ]]; then
    echo "ERROR: .env not found. Copy .env.example and fill in your values:"
    echo "  cp .env.example .env && \$EDITOR .env"
    exit 1
fi

# ── 2. Virtual environment ────────────────────────────────────────────────────
if command -v uv &>/dev/null; then
    echo "==> Creating venv with uv …"
    cd "$REPO_DIR"
    uv venv .venv
    uv pip install -e .
else
    echo "==> uv not found, falling back to python3 -m venv …"
    python3 -m venv "$REPO_DIR/.venv"
    "$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR"
fi

# ── 3. Register device (if not already done) ──────────────────────────────────
AUTH_FILE="${AUTH_STATE_FILE:-$HOME/.rm-obsidian-sync/auth.json}"
if [[ ! -f "$AUTH_FILE" ]]; then
    echo ""
    echo "==> Device not yet registered with rmfakecloud."
    echo "    Open the rmfakecloud web UI, go to Device Pairing, and copy"
    echo "    the one-time code, then run:"
    echo ""
    echo "      $REPO_DIR/.venv/bin/rm-register --code <your-code>"
    echo ""
    echo "    Then re-run this script to install the service."
    exit 0
fi

# ── 4. Systemd service ────────────────────────────────────────────────────────
echo "==> Installing systemd user service …"
mkdir -p "$SYSTEMD_DIR"

# Substitute %h with the actual home directory and set the repo path
sed "s|%h/rm-obsidian-sync|$REPO_DIR|g; s|%h|$HOME|g" \
    "$SERVICE_FILE" > "$SYSTEMD_DIR/${SERVICE_NAME}.service"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user start  "$SERVICE_NAME"

echo ""
echo "✓ Service installed and started."
echo ""
echo "Useful commands:"
echo "  systemctl --user status  $SERVICE_NAME"
echo "  journalctl --user -u $SERVICE_NAME -f"
echo "  systemctl --user restart $SERVICE_NAME"
