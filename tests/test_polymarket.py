"""Tests for the Polymarket monitor."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

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


def _make_monitor() -> PolymarketMonitor:
    """Create a PolymarketMonitor with event refresh bypassed."""
    monitor = PolymarketMonitor()
    # Set events_last_refreshed far in the future so _maybe_refresh_events is a no-op
    monitor._events_last_refreshed = time.monotonic() + 999_999
    return monitor


def _mock_response(*, status: int = 200, json_data=None):
    """Create a mock aiohttp response usable as async context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else [])
    resp.raise_for_status = MagicMock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# Basic tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_polymarket_init():
    """Plugin should have correct name and interval."""
    monitor = PolymarketMonitor()
    assert monitor.name == "POLYMARKET"
    assert monitor.interval == 15


@pytest.mark.asyncio
async def test_polymarket_check_empty_trades():
    """check() should return empty list when API returns no trades."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    mock_resp = _mock_response(json_data=[])
    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_polymarket_check_filters_small_trades():
    """Trades below threshold should be filtered out."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    # 100 shares at $0.10 = $10 dollar value, well below $10,000 threshold
    trades = [
        {
            "size": 100,
            "price": 0.10,
            "slug": "test-market",
            "proxyWallet": "0xabc",
            "title": "Test Market?",
        }
    ]

    mock_resp = _mock_response(json_data=trades)
    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_polymarket_check_filters_high_odds():
    """Trades with odds above max should be filtered out."""
    monitor = _make_monitor()
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

    mock_resp = _mock_response(json_data=trades)
    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_polymarket_check_produces_alert():
    """A qualifying trade should produce an alert."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    # 200,000 shares at $0.10 = $20,000 dollar value (above $10,000 threshold)
    trades = [
        {
            "size": 200000,
            "price": 0.10,
            "slug": "big-event-option-yes",
            "eventSlug": "big-event",
            "proxyWallet": "0xabc123",
            "title": "Will something happen?",
        }
    ]

    mock_resp = _mock_response(json_data=trades)
    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert len(alerts) == 1
    assert alerts[0].monitor == "POLYMARKET"
    assert alerts[0].title == "Informed Flow Detected"
    assert "20,000" in alerts[0].body
    assert alerts[0].data["size"] == pytest.approx(20000.0)
    # Link should use eventSlug (event-level), not slug (option-level)
    assert alerts[0].link == "https://polymarket.com/event/big-event"


@pytest.mark.asyncio
async def test_polymarket_deduplication():
    """Same market+wallet should not alert twice within 6 hours."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    trades = [
        {
            "size": 200000,
            "price": 0.10,
            "slug": "dedup-market",
            "proxyWallet": "0xdedup",
            "title": "Dedup test?",
        }
    ]

    mock_resp = _mock_response(json_data=trades)
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
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    mock_resp = _mock_response(status=429)
    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []


# ---------------------------------------------------------------------------
# _fetch_trades parameter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_trades_sends_cash_filter_params():
    """_fetch_trades should send filterType=CASH and filterAmount params."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    mock_resp = _mock_response(json_data=[])
    monitor._session.get = MagicMock(return_value=mock_resp)

    await monitor._fetch_trades()

    # Verify the GET call was made with correct params
    monitor._session.get.assert_called_once()
    call_args = monitor._session.get.call_args
    params = call_args[1].get("params") or call_args.kwargs.get("params")

    assert params["limit"] == "10000"
    assert params["filterType"] == "CASH"
    assert params["filterAmount"] == str(config.get_float("POLY_THRESHOLD"))


@pytest.mark.asyncio
async def test_fetch_trades_includes_event_ids_when_populated():
    """_fetch_trades should include eventId param when event IDs are cached."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()
    monitor._event_ids = [111, 222, 333]

    mock_resp = _mock_response(json_data=[])
    monitor._session.get = MagicMock(return_value=mock_resp)

    await monitor._fetch_trades()

    call_args = monitor._session.get.call_args
    params = call_args[1].get("params") or call_args.kwargs.get("params")

    assert params["eventId"] == "111,222,333"


@pytest.mark.asyncio
async def test_fetch_trades_omits_event_ids_when_empty():
    """_fetch_trades should NOT include eventId param when no event IDs cached."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()
    monitor._event_ids = []

    mock_resp = _mock_response(json_data=[])
    monitor._session.get = MagicMock(return_value=mock_resp)

    await monitor._fetch_trades()

    call_args = monitor._session.get.call_args
    params = call_args[1].get("params") or call_args.kwargs.get("params")

    assert "eventId" not in params


@pytest.mark.asyncio
async def test_fetch_trades_handles_server_error():
    """_fetch_trades should return empty list on 5xx errors."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    mock_resp = _mock_response(status=500)
    monitor._session.get = MagicMock(return_value=mock_resp)

    result = await monitor._fetch_trades()
    assert result == []


# ---------------------------------------------------------------------------
# Transaction hash deduplication tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tx_hash_dedup_skips_duplicate_trade():
    """A trade with a previously seen transactionHash should be skipped."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    trades = [
        {
            "size": 200000,
            "price": 0.10,
            "slug": "tx-dedup-market",
            "eventSlug": "tx-dedup-event",
            "proxyWallet": "0xwallet1",
            "title": "TX Dedup test?",
            "transactionHash": "0xabc123hash",
        }
    ]

    mock_resp = _mock_response(json_data=trades)
    monitor._session.get = MagicMock(return_value=mock_resp)

    # First call: should produce alert and record the tx hash
    alerts1 = await monitor.check()
    assert len(alerts1) == 1
    assert "0xabc123hash" in monitor._seen_tx_hashes

    # Insert alert to DB so db-level dedup doesn't interfere
    await db.insert_alert("POLYMARKET", "tx-dedup-market:0xwallet1", alerts1[0].data)

    # Second call with same trades: tx hash dedup should skip it
    # (even though db dedup would also catch it, tx hash fires first)
    alerts2 = await monitor.check()
    assert len(alerts2) == 0


@pytest.mark.asyncio
async def test_tx_hash_dedup_allows_different_hashes():
    """Trades with different transactionHashes should both produce alerts."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    trade1 = [
        {
            "size": 200000,
            "price": 0.10,
            "slug": "market-a",
            "eventSlug": "event-a",
            "proxyWallet": "0xwalletA",
            "title": "Market A?",
            "transactionHash": "0xhash_one",
        }
    ]
    trade2 = [
        {
            "size": 300000,
            "price": 0.15,
            "slug": "market-b",
            "eventSlug": "event-b",
            "proxyWallet": "0xwalletB",
            "title": "Market B?",
            "transactionHash": "0xhash_two",
        }
    ]

    # First call with trade1
    mock_resp1 = _mock_response(json_data=trade1)
    monitor._session.get = MagicMock(return_value=mock_resp1)
    alerts1 = await monitor.check()
    assert len(alerts1) == 1

    # Second call with trade2 (different hash, different market)
    mock_resp2 = _mock_response(json_data=trade2)
    monitor._session.get = MagicMock(return_value=mock_resp2)
    alerts2 = await monitor.check()
    assert len(alerts2) == 1


@pytest.mark.asyncio
async def test_tx_hash_only_recorded_after_passing_filters():
    """A tx hash should NOT be recorded if the trade is filtered out by size/odds."""
    monitor = _make_monitor()
    monitor._session = AsyncMock()

    # Small trade that won't pass the dollar threshold
    trades = [
        {
            "size": 10,
            "price": 0.05,
            "slug": "small-market",
            "proxyWallet": "0xsmall",
            "title": "Small trade?",
            "transactionHash": "0xsmall_hash",
        }
    ]

    mock_resp = _mock_response(json_data=trades)
    monitor._session.get = MagicMock(return_value=mock_resp)

    alerts = await monitor.check()
    assert alerts == []
    # The hash should NOT be in _seen_tx_hashes since the trade was filtered
    assert "0xsmall_hash" not in monitor._seen_tx_hashes


# ---------------------------------------------------------------------------
# Event discovery tests (_fetch_large_event_ids, _maybe_refresh_events)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_large_event_ids_returns_ids():
    """_fetch_large_event_ids should parse event IDs from Gamma API response."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    events = [
        {"id": "101", "title": "Event A", "volume": "500000"},
        {"id": "202", "title": "Event B", "volume": "1000000"},
        {"id": "303", "title": "Event C", "volume": "200000"},
    ]

    mock_resp = _mock_response(json_data=events)
    monitor._session.get = MagicMock(return_value=mock_resp)

    result = await monitor._fetch_large_event_ids()
    assert result == [101, 202, 303]


@pytest.mark.asyncio
async def test_fetch_large_event_ids_sends_correct_params():
    """_fetch_large_event_ids should send volume_min and active filters."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    mock_resp = _mock_response(json_data=[])
    monitor._session.get = MagicMock(return_value=mock_resp)

    await monitor._fetch_large_event_ids()

    call_args = monitor._session.get.call_args
    url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
    params = call_args[1].get("params") or call_args.kwargs.get("params")

    assert "gamma-api.polymarket.com" in str(url) or "gamma-api" in str(call_args)
    assert params["active"] == "true"
    assert params["closed"] == "false"
    assert params["volume_min"] == str(config.get_float("POLY_MIN_MARKET_VOLUME"))
    assert params["limit"] == "500"


@pytest.mark.asyncio
async def test_fetch_large_event_ids_handles_rate_limit():
    """_fetch_large_event_ids should return empty list on 429."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    mock_resp = _mock_response(status=429)
    monitor._session.get = MagicMock(return_value=mock_resp)

    result = await monitor._fetch_large_event_ids()
    assert result == []


@pytest.mark.asyncio
async def test_fetch_large_event_ids_handles_server_error():
    """_fetch_large_event_ids should return empty list on 5xx."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    mock_resp = _mock_response(status=502)
    monitor._session.get = MagicMock(return_value=mock_resp)

    result = await monitor._fetch_large_event_ids()
    assert result == []


@pytest.mark.asyncio
async def test_fetch_large_event_ids_skips_invalid_ids():
    """Events with non-numeric IDs should be silently skipped."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()

    events = [
        {"id": "101", "title": "Good"},
        {"id": "not-a-number", "title": "Bad"},
        {"id": None, "title": "Also Bad"},
        {"id": "303", "title": "Also Good"},
    ]

    mock_resp = _mock_response(json_data=events)
    monitor._session.get = MagicMock(return_value=mock_resp)

    result = await monitor._fetch_large_event_ids()
    assert result == [101, 303]


@pytest.mark.asyncio
async def test_maybe_refresh_events_skips_when_fresh():
    """_maybe_refresh_events should not call API if cache is fresh."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()
    monitor._events_last_refreshed = time.monotonic()  # just refreshed
    monitor._event_ids = [1, 2, 3]

    await monitor._maybe_refresh_events()

    # Session.get should NOT have been called
    monitor._session.get.assert_not_called()
    # Event IDs unchanged
    assert monitor._event_ids == [1, 2, 3]


@pytest.mark.asyncio
async def test_maybe_refresh_events_refreshes_when_stale():
    """_maybe_refresh_events should call Gamma API when cache is stale."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()
    monitor._events_last_refreshed = 0.0  # never refreshed (epoch)

    events = [{"id": "10"}, {"id": "20"}]
    mock_resp = _mock_response(json_data=events)
    monitor._session.get = MagicMock(return_value=mock_resp)

    await monitor._maybe_refresh_events()

    assert monitor._event_ids == [10, 20]
    assert monitor._events_last_refreshed > 0


@pytest.mark.asyncio
async def test_maybe_refresh_events_keeps_old_ids_on_failure():
    """On API failure, existing event IDs should be preserved."""
    monitor = PolymarketMonitor()
    monitor._session = AsyncMock()
    monitor._events_last_refreshed = 0.0
    monitor._event_ids = [99, 88]  # existing cache

    # Return empty (simulating failure path)
    mock_resp = _mock_response(status=500)
    monitor._session.get = MagicMock(return_value=mock_resp)

    await monitor._maybe_refresh_events()

    # Old event IDs should be preserved
    assert monitor._event_ids == [99, 88]
    # But timestamp should be updated to avoid hammering
    assert monitor._events_last_refreshed > 0
