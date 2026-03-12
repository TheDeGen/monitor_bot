"""Async SQLite helpers — init, insert, query, purge."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from src import config

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    monitor TEXT NOT NULL,
    market TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    monitor TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_cache (
    address TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def init() -> None:
    """Open the database and create tables if needed."""
    global _db
    db_path = config.get("DB_PATH")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(db_path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    await _db.commit()
    logger.info("Database initialised at %s", db_path)


async def close() -> None:
    """Close the database connection."""
    global _db
    if _db:
        await _db.close()
        _db = None


def _conn() -> aiosqlite.Connection:
    """Return the active connection or raise."""
    if _db is None:
        raise RuntimeError("Database not initialised — call db.init() first")
    return _db


# ── Alerts ──────────────────────────────────────────────────────────────────


async def insert_alert(monitor: str, market: str, data: dict) -> int:
    """Insert an alert row and return its id."""
    conn = _conn()
    cur = await conn.execute(
        "INSERT INTO alerts (monitor, market, data_json) VALUES (?, ?, ?)",
        (monitor, market, json.dumps(data)),
    )
    await conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def recent_alerts(monitor: str | None = None, limit: int = 10) -> list[dict]:
    """Fetch the most recent alerts, optionally filtered by monitor."""
    conn = _conn()
    if monitor:
        cur = await conn.execute(
            "SELECT * FROM alerts WHERE monitor = ? ORDER BY id DESC LIMIT ?",
            (monitor.upper(), limit),
        )
    else:
        cur = await conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
        )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def alert_exists(monitor: str, market: str, hours: int = 6) -> bool:
    """Check if an alert for this monitor+market was sent within ``hours``."""
    conn = _conn()
    cur = await conn.execute(
        """SELECT 1 FROM alerts
           WHERE monitor = ? AND market = ?
             AND timestamp >= datetime('now', ?)
           LIMIT 1""",
        (monitor, market, f"-{hours} hours"),
    )
    return (await cur.fetchone()) is not None


# ── Snapshots ───────────────────────────────────────────────────────────────


async def insert_snapshot(monitor: str, data: dict) -> int:
    """Insert a time-series snapshot and return its id."""
    conn = _conn()
    cur = await conn.execute(
        "INSERT INTO snapshots (monitor, data_json) VALUES (?, ?)",
        (monitor, json.dumps(data)),
    )
    await conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def get_snapshots(
    monitor: str,
    hours: int = 1,
    market_address: str | None = None,
) -> list[dict]:
    """Fetch snapshots for a monitor within the last ``hours``."""
    conn = _conn()
    query = """SELECT * FROM snapshots
               WHERE monitor = ? AND timestamp >= datetime('now', ?)
               ORDER BY timestamp ASC"""
    params: list[Any] = [monitor, f"-{hours} hours"]

    cur = await conn.execute(query, params)
    rows = await cur.fetchall()
    results = []
    for r in rows:
        row_dict = dict(r)
        if market_address:
            data = json.loads(row_dict["data_json"])
            if data.get("market_address") == market_address:
                results.append(row_dict)
        else:
            results.append(row_dict)
    return results


# ── Settings ────────────────────────────────────────────────────────────────


async def get_setting(key: str) -> str | None:
    """Get a runtime setting value."""
    conn = _conn()
    cur = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    """Upsert a runtime setting."""
    conn = _conn()
    await conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await conn.commit()


async def delete_setting(key: str) -> None:
    """Delete a runtime setting."""
    conn = _conn()
    await conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    await conn.commit()


async def all_settings() -> dict[str, str]:
    """Return all runtime settings as a dict."""
    conn = _conn()
    cur = await conn.execute("SELECT key, value FROM settings")
    rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── Wallet Cache ────────────────────────────────────────────────────────────


async def get_wallet_age(address: str) -> str | None:
    """Get cached wallet creation timestamp, or None if not cached."""
    conn = _conn()
    cur = await conn.execute(
        "SELECT created_at FROM wallet_cache WHERE address = ?", (address.lower(),)
    )
    row = await cur.fetchone()
    return row["created_at"] if row else None


async def set_wallet_age(address: str, created_at: str) -> None:
    """Cache a wallet creation timestamp."""
    conn = _conn()
    await conn.execute(
        "INSERT INTO wallet_cache (address, created_at) VALUES (?, ?) "
        "ON CONFLICT(address) DO UPDATE SET created_at = excluded.created_at, "
        "fetched_at = datetime('now')",
        (address.lower(), created_at),
    )
    await conn.commit()


# ── Purge ───────────────────────────────────────────────────────────────────


async def purge(hours: int | None = None) -> int:
    """Delete alerts and snapshots older than ``hours`` (default from config).

    Returns the total number of deleted rows.
    """
    if hours is None:
        hours = config.get_int("PURGE_HOURS")
    conn = _conn()
    threshold = f"-{hours} hours"
    c1 = await conn.execute(
        "DELETE FROM alerts WHERE timestamp < datetime('now', ?)", (threshold,)
    )
    c2 = await conn.execute(
        "DELETE FROM snapshots WHERE timestamp < datetime('now', ?)", (threshold,)
    )
    await conn.commit()
    total = (c1.rowcount or 0) + (c2.rowcount or 0)
    if total:
        logger.info("Purged %d rows older than %dh", total, hours)
    return total
