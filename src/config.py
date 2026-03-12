"""Configuration loader — env vars with runtime override support."""

from __future__ import annotations

import os
from typing import Any

# Required env vars (no defaults — bot must refuse to start without them)
_REQUIRED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

# Optional env vars with defaults
_DEFAULTS: dict[str, str] = {
    "POLY_THRESHOLD": "10000",
    "POLY_MAX_WALLET_AGE_DAYS": "90",
    "POLY_MAX_ODDS": "0.20",
    "PENDLE_CHAINS": "ethereum,arbitrum,bnb,optimism",
    "DB_PATH": "data/monitor.db",
    "PURGE_HOURS": "48",
    "POLYGONSCAN_API_KEY": "",
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
    for key in _DEFAULTS:
        val = get(key)
        if "TOKEN" in key or "KEY" in key:
            result[key] = val[:8] + "..." if len(val) > 8 else "***"
        else:
            result[key] = val
    return result
