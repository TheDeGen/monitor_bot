"""Configuration loader — env vars with runtime override support."""

from __future__ import annotations

import os
from typing import Any

# Required env vars (no defaults — bot must refuse to start without them)
# TELEGRAM_CHAT_ID kept for backward compat; prefer TELEGRAM_CHAT_IDS (comma-separated)
_REQUIRED = ("TELEGRAM_BOT_TOKEN",)

# Optional env vars with defaults
_DEFAULTS: dict[str, str] = {
    "POLY_THRESHOLD": "10000",
    "POLY_MAX_WALLET_AGE_DAYS": "90",
    "POLY_MAX_ODDS": "0.20",
    "PENDLE_CHAINS": "ethereum,arbitrum,bnb,optimism",
    "DB_PATH": "data/monitor.db",
    "PURGE_HOURS": "48",
    "POLYGONSCAN_API_KEY": "",
    "ADMIN_USER_IDS": "",
}

# Runtime override store (populated from the ``settings`` SQLite table)
_runtime_overrides: dict[str, str] = {}


def load_runtime_overrides(overrides: dict[str, str]) -> None:
    """Replace the in-memory runtime overrides (called at startup from DB)."""
    _runtime_overrides.clear()
    _runtime_overrides.update(overrides)


def set_override(key: str, value: str) -> None:
    """Set a single runtime override."""
    _runtime_overrides[key] = value


def remove_override(key: str) -> None:
    """Remove a single runtime override."""
    _runtime_overrides.pop(key, None)


def get(key: str, fallback: str | None = None) -> str:
    """Get a config value.  Priority: runtime override > env var > default > fallback."""
    if key in _runtime_overrides:
        return _runtime_overrides[key]
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val
    if key in _DEFAULTS:
        return _DEFAULTS[key]
    if fallback is not None:
        return fallback
    raise KeyError(f"Missing required config: {key}")


def get_int(key: str, fallback: int | None = None) -> int:
    """Get a config value as an integer."""
    fb = str(fallback) if fallback is not None else None
    return int(get(key, fb))


def get_float(key: str, fallback: float | None = None) -> float:
    """Get a config value as a float."""
    fb = str(fallback) if fallback is not None else None
    return float(get(key, fb))


def get_list(key: str, fallback: str | None = None) -> list[str]:
    """Get a comma-separated config value as a list of strings."""
    return [s.strip() for s in get(key, fallback).split(",") if s.strip()]


def validate() -> None:
    """Raise ``SystemExit`` if required variables are missing."""
    missing = [k for k in _REQUIRED if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    # At least one alert destination must be configured
    has_ids = os.environ.get("TELEGRAM_CHAT_IDS") or os.environ.get("TELEGRAM_CHAT_ID")
    if not has_ids:
        raise SystemExit(
            "Missing alert destination: set TELEGRAM_CHAT_IDS (preferred) or TELEGRAM_CHAT_ID"
        )


def all_config() -> dict[str, str]:
    """Return a dict of all known config keys and their current effective values."""
    result: dict[str, str] = {}
    for key in _REQUIRED:
        try:
            val = get(key)
            # Mask tokens
            if "TOKEN" in key or "KEY" in key:
                result[key] = val[:8] + "..." if len(val) > 8 else "***"
            else:
                result[key] = val
        except KeyError:
            result[key] = "<NOT SET>"

    # Show alert chat IDs
    chat_ids = get_alert_chat_ids()
    result["ALERT_CHAT_IDS"] = ", ".join(str(c) for c in chat_ids) if chat_ids else "<NONE>"

    # Show admin user IDs
    admin_ids = get_admin_user_ids()
    result["ADMIN_USER_IDS"] = ", ".join(str(a) for a in admin_ids) if admin_ids else "<unrestricted>"

    for key in _DEFAULTS:
        if key == "ADMIN_USER_IDS":
            continue  # already displayed above
        val = get(key)
        if "TOKEN" in key or "KEY" in key:
            result[key] = val[:8] + "..." if len(val) > 8 else "***"
        else:
            result[key] = val
    return result



def get_alert_chat_ids() -> list[int]:
    """Return the list of chat IDs that should receive scheduled alerts.

    Reads ``TELEGRAM_CHAT_IDS`` first (comma-separated).  Falls back to the
    legacy single ``TELEGRAM_CHAT_ID`` for backward compatibility.
    """
    try:
        raw = get("TELEGRAM_CHAT_IDS")
    except KeyError:
        raw = None

    if raw:
        return [int(cid.strip()) for cid in raw.split(",") if cid.strip()]

    # Fallback to legacy single-value key
    try:
        return [int(get("TELEGRAM_CHAT_ID"))]
    except KeyError:
        return []


def get_admin_user_ids() -> set[int]:
    """Return the set of Telegram user IDs allowed to run admin commands.

    If ``ADMIN_USER_IDS`` is empty or unset, admin commands are unrestricted
    (backward-compatible default for private-chat-only setups).
    """
    raw = get("ADMIN_USER_IDS", "")
    if not raw:
        return set()
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}