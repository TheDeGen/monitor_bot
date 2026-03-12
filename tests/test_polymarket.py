"""Tests for the Polymarket monitor."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set required env vars before importing our modules
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_IDS", "123456")

from src import config, db
from src.monitors.polymarket import PolymarketMonitor
from src.plugin_base import Alert


@pytest.fixture(autouse=True)
async def setup_db(tmp_path):
    """Initialise a temporary database for each test."""
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    await db.init()
    yield
    await db.close()


@pytest.mark.asyncio
async def test_polymarket_init():
    """Plugin should have correct name and interval."""
    monitor = PolymarketMonitor()
    assert monitor.name == "POLYMARKET"
    assert monitor.interval == 60


@pytest.mark.asyncio
async def test_polymarket_check_empty_trades():
    """check() should return empty list when API returns no trades."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[])
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_polymarket_check_filters_small_trades():
    """Trades below threshold should be filtered out."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    # Trade with size below default threshold of 10000
    trades = [
        {
            "size": 100,
            "price": 0.10,
            "slug": "test-market",
            "proxyWallet": "0xabc",
            "title": "Test Market?",
        }
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=trades)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_polymarket_check_filters_high_odds():
    """Trades with odds above max should be filtered out."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    # Trade with high odds (price > 0.20)
    trades = [
        {
            "size": 50000,
            "price": 0.90,
            "slug": "test-market",
            "proxyWallet": "0xabc",
            "title": "Test Market?",
        }
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=trades)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_polymarket_check_produces_alert():
    """A qualifying trade should produce an alert."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    # Trade meeting all criteria (no polygonscan key = wallet age check skipped)
    trades = [
        {
            "size": 50000,
            "price": 0.10,
            "slug": "big-event",
            "eventSlug": "big-event",
            "proxyWallet": "0xabc123",
            "title": "Will something happen?",
        }
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=trades)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert len(alerts) == 1
    assert alerts[0].monitor == "POLYMARKET"
    assert alerts[0].title == "Informed Flow Detected"
    assert "50,000" in alerts[0].body
    assert alerts[0].data["size"] == 50000


@pytest.mark.asyncio
async def test_polymarket_deduplication():
    """Same market+wallet should not alert twice within 6 hours."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    trades = [
        {
            "size": 50000,
            "price": 0.10,
            "slug": "dedup-market",
            "proxyWallet": "0xdedup",
            "title": "Dedup test?",
        }
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=trades)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    monitor._session.get = MagicMock(return_value=mock_resp)

    # First call should produce alert
    alerts1 = await monitor.check()
    assert len(alerts1) == 1

    # Insert the alert into DB (simulating what scheduler does)
    await db.insert_alert("POLYMARKET", "dedup-market:0xdedup", alerts1[0].data)

    # Second call should be deduplicated
    alerts2 = await monitor.check()
    assert len(alerts2) == 0


@pytest.mark.asyncio
async def test_polymarket_rate_limit_handling():
    """Monitor should handle 429 responses gracefully."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    mock_resp = AsyncMock()
    mock_resp.status = 429
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []
