"""
auth.py — rmfakecloud authentication helpers.

Handles device registration, user-token acquisition, token persistence and
transparent refresh.  All network calls are **synchronous** (uses httpx in
sync mode) so the module can also be driven from the Click CLI without an
asyncio event-loop.

Public entry-point::

    token = get_user_token()   # returns a fresh JWT string

"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import httpx

from rm_sync.config import config

logger = logging.getLogger(__name__)

_DEVICE_DESC = "rm-obsidian-sync-daemon"


# ──────────────────────────────────────────────────────────────────────────────
# Typed storage schema
# ──────────────────────────────────────────────────────────────────────────────


class AuthData(TypedDict, total=False):
    device_token: str
    user_token: str
    device_id: str
    registered_at: str
    refreshed_at: str


# ──────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ──────────────────────────────────────────────────────────────────────────────


def _load_auth() -> AuthData:
    """Load stored auth data from disk, returning an empty dict on miss."""
    path: Path = config.auth_state_file
    if path.exists():
        try:
            return json.loads(path.read_text())  # type: ignore[return-value]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read auth file %s: %s", path, exc)
    return {}


def _save_auth(data: AuthData) -> None:
    """Atomically persist auth data to disk."""
    path: Path = config.auth_state_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    logger.debug("Auth data saved to %s", path)


# ──────────────────────────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────────────────────────


def register_device(one_time_code: str) -> str:
    """
    Exchange a one-time registration code for a device JWT and persist it.

    Args:
        one_time_code: The short code shown by rmfakecloud's pairing UI.

    Returns:
        The device JWT string.

    Raises:
        httpx.HTTPStatusError: If the server rejects the registration.
        RuntimeError: If the response cannot be parsed.
    """
    device_id = str(uuid.uuid4())
    payload = {
        "code": one_time_code,
        "deviceDesc": _DEVICE_DESC,
        "deviceID": device_id,
    }
    url = f"{config.rmfakecloud_url}/token/json/2/device/new"
    logger.info("Registering device with rmfakecloud at %s …", url)

    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()

    # rmfakecloud returns the JWT as a plain-text body (not JSON-wrapped).
    device_token = resp.text.strip()
    if not device_token:
        raise RuntimeError("Empty device token received from rmfakecloud")

    data = _load_auth()
    data["device_token"] = device_token
    data["device_id"] = device_id
    data["registered_at"] = datetime.now(timezone.utc).isoformat()
    _save_auth(data)

    logger.info("Device registered successfully (id=%s)", device_id)
    return device_token


# ──────────────────────────────────────────────────────────────────────────────
# User-token acquisition / refresh
# ──────────────────────────────────────────────────────────────────────────────


def _acquire_user_token(device_token: str) -> str:
    """
    Use the device JWT to obtain a short-lived user JWT.

    Args:
        device_token: A valid device JWT.

    Returns:
        A fresh user JWT.
    """
    url = f"{config.rmfakecloud_url}/token/json/2/user/new"
    logger.info("Acquiring user token from %s …", url)

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {device_token}"},
        )
        resp.raise_for_status()

    user_token = resp.text.strip()
    if not user_token:
        raise RuntimeError("Empty user token received from rmfakecloud")

    data = _load_auth()
    data["user_token"] = user_token
    data["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    _save_auth(data)

    logger.info("User token acquired/refreshed successfully")
    return user_token


def _decode_jwt_exp(token: str) -> datetime | None:
    """
    Decode the ``exp`` claim from a JWT **without** verifying the signature.

    Returns None if the claim cannot be parsed (e.g. opaque tokens).
    """
    try:
        import base64

        parts = token.split(".")
        if len(parts) != 3:
            return None
        # JWT base64 uses URL-safe alphabet without padding.
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except Exception:
        return None


def _token_is_expired(token: str, buffer_seconds: int = 120) -> bool:
    """Return True when *token* expires within *buffer_seconds* from now."""
    exp = _decode_jwt_exp(token)
    if exp is None:
        # Can't decode — assume expired so we always refresh.
        return True
    remaining = (exp - datetime.now(timezone.utc)).total_seconds()
    return remaining < buffer_seconds


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def get_user_token() -> str:
    """
    Return a valid user JWT, refreshing it if it is expired or absent.

    Raises:
        RuntimeError: If no device token is found (i.e. device not registered).
    """
    data = _load_auth()

    device_token = data.get("device_token", "")
    if not device_token:
        raise RuntimeError(
            "No device token found. Run `rm-register --code <code>` first."
        )

    user_token = data.get("user_token", "")
    if user_token and not _token_is_expired(user_token):
        logger.debug("Using cached user token (still valid)")
        return user_token

    logger.info("User token absent or expired — refreshing …")
    return _acquire_user_token(device_token)


def get_device_token() -> str:
    """
    Return the stored device token.

    Raises:
        RuntimeError: If no device token exists.
    """
    data = _load_auth()
    token = data.get("device_token", "")
    if not token:
        raise RuntimeError(
            "No device token found. Run `rm-register --code <code>` first."
        )
    return token
