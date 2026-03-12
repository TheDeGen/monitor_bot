"""Tests for the Pendle monitor."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

# Set required env vars before importing our modules
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_IDS", "123456")

from src import config, db
from src.monitors.pendle import CHAIN_IDS, PendleMonitor
from src.plugin_base import Alert


@pytest.fixture(autouse=True)
async def setup_db(tmp_path):
    """Initialise a temporary database for each test."""
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    await db.init()
    yield
    await db.close()


def _mock_json_response(data, status=200):
    """Create a mock aiohttp response."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=data)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# Basic tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pendle_init():
    """Plugin should have correct name and interval (90s)."""
    monitor = PendleMonitor()
    assert monitor.name == "PENDLE"
    assert monitor.interval == 90


@pytest.mark.asyncio
async def test_chain_ids():
    """Verify chain ID mapping includes all 8 chains."""
    assert CHAIN_IDS["ethereum"] == 1
    assert CHAIN_IDS["arbitrum"] == 42161
    assert CHAIN_IDS["bnb"] == 56
    assert CHAIN_IDS["optimism"] == 10
    assert CHAIN_IDS["base"] == 8453
    assert CHAIN_IDS["sonic"] == 146
    assert CHAIN_IDS["hyperevm"] == 999
    assert CHAIN_IDS["plasma"] == 9745
    assert len(CHAIN_IDS) == 8


# ---------------------------------------------------------------------------
# check() with empty / no markets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pendle_check_empty_markets():
    """check() should return empty list when no markets found."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    markets_resp = _mock_json_response({"results": []})
    monitor._session.get = MagicMock(return_value=markets_resp)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_pendle_check_no_session():
    """check() should return empty list when session is None."""
    monitor = PendleMonitor()
    monitor._session = None
    alerts = await monitor.check()
    assert alerts == []


# ---------------------------------------------------------------------------
# Discount widening alert tests
# ---------------------------------------------------------------------------


def _make_market_data(
    address="0xtest123", name="Test Market", chain_id=1, pt_discount=0.05
):
    """Helper to build a market detail response."""
    return {
        "address": address,
        "name": name,
        "chainId": chain_id,
        "ptDiscount": pt_discount,
        "pt": {"address": "0xpt", "symbol": "PT-TEST", "price": {"usd": 0.95}},
        "yt": {"address": "0xyt", "symbol": "YT-TEST", "price": {"usd": 0.05}},
        "underlyingAsset": {"symbol": "WETH"},
        "liquidity": {"usd": 1000000},
        "tradingVolume": {"usd": 100000},
    }


async def _seed_old_snapshot(address: str, pt_discount: float, hours_ago: float = 4.0):
    """Insert a snapshot with a timestamp in the past."""
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    snapshot = {
        "market_address": address,
        "chain": "ethereum",
        "chain_id": 1,
        "name": "Test Market",
        "pt_discount": pt_discount,
        "timestamp": ts.isoformat(),
    }
    await db.insert_snapshot("PENDLE", snapshot)
    # Backdate the row so get_snapshots (which filters by SQLite datetime('now', ...)) picks it up
    conn = db._conn()
    await conn.execute(
        "UPDATE snapshots SET timestamp = ? WHERE id = (SELECT MAX(id) FROM snapshots)",
        (ts.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_pendle_discount_widening_alert():
    """Should alert when PT discount widens more than 1% over 4h."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    address = "0xdiscount_test"
    # Seed old snapshot with 2% discount (4h ago)
    await _seed_old_snapshot(address, pt_discount=0.02, hours_ago=3.5)

    # Current discount is 5% → change = 3% > 1% threshold
    market_data = _make_market_data(address=address, pt_discount=0.05)
    markets_resp = _mock_json_response({"results": [{"address": address}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if f"/markets/{address}" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()

    discount_alerts = [a for a in alerts if a.data.get("check") == "discount_widening"]
    assert len(discount_alerts) == 1
    assert discount_alerts[0].monitor == "PENDLE"
    assert discount_alerts[0].title == "PT Discount Widening"
    assert discount_alerts[0].data["change"] == pytest.approx(0.03, abs=0.005)
    assert discount_alerts[0].data["current_discount"] == pytest.approx(0.05, abs=0.001)
    assert discount_alerts[0].data["old_discount"] == pytest.approx(0.02, abs=0.001)


@pytest.mark.asyncio
async def test_pendle_discount_widening_below_threshold():
    """Should NOT alert when PT discount change is below 1%."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    address = "0xsmall_change"
    # Seed old snapshot with 2% discount
    await _seed_old_snapshot(address, pt_discount=0.02, hours_ago=3.5)

    # Current discount is 2.5% → change = 0.5% < 1% threshold
    market_data = _make_market_data(address=address, pt_discount=0.025)
    markets_resp = _mock_json_response({"results": [{"address": address}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if f"/markets/{address}" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()

    discount_alerts = [a for a in alerts if a.data.get("check") == "discount_widening"]
    assert len(discount_alerts) == 0


@pytest.mark.asyncio
async def test_pendle_discount_widening_no_history():
    """Should NOT alert when no historical snapshots exist (first run)."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    address = "0xno_history"
    # No seeded snapshot — first time seeing this market

    market_data = _make_market_data(address=address, pt_discount=0.05)
    markets_resp = _mock_json_response({"results": [{"address": address}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if f"/markets/{address}" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()

    # The snapshot just inserted is the only one; no old snapshot to compare against
    # on the very first call. The monitor inserts then queries — the freshly inserted
    # snapshot IS the "oldest" so change will be 0.
    discount_alerts = [a for a in alerts if a.data.get("check") == "discount_widening"]
    assert len(discount_alerts) == 0


@pytest.mark.asyncio
async def test_pendle_discount_narrowing_no_alert():
    """Should NOT alert when discount narrows (decreases) — only widening matters."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    address = "0xnarrowing"
    # Old discount was 5%, now it's 2% → change = -3% (negative, not widening)
    await _seed_old_snapshot(address, pt_discount=0.05, hours_ago=3.5)

    market_data = _make_market_data(address=address, pt_discount=0.02)
    markets_resp = _mock_json_response({"results": [{"address": address}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if f"/markets/{address}" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()

    discount_alerts = [a for a in alerts if a.data.get("check") == "discount_widening"]
    assert len(discount_alerts) == 0


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pendle_deduplication():
    """Same alert type + market should not fire again within 6 hours."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    address = "0xdedup"
    await _seed_old_snapshot(address, pt_discount=0.01, hours_ago=3.5)

    # Current discount 5% → change 4% > 1% threshold
    market_data = _make_market_data(address=address, pt_discount=0.05)
    markets_resp = _mock_json_response({"results": [{"address": address}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if f"/markets/{address}" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)
    os.environ["PENDLE_CHAINS"] = "ethereum"

    # First check — should fire
    alerts1 = await monitor.check()
    discount_alerts1 = [
        a for a in alerts1 if a.data.get("check") == "discount_widening"
    ]
    assert len(discount_alerts1) == 1

    # Insert into DB (simulating what scheduler does)
    for a in alerts1:
        await db.insert_alert(a.monitor, a.data.get("market", ""), a.data)

    # Second check — should be deduplicated
    alerts2 = await monitor.check()
    discount_alerts2 = [
        a for a in alerts2 if a.data.get("check") == "discount_widening"
    ]
    assert len(discount_alerts2) == 0


# ---------------------------------------------------------------------------
# Rate limit / error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pendle_rate_limit_handling():
    """Monitor should handle rate limits gracefully."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    rate_limited = _mock_json_response({}, status=429)
    monitor._session.get = MagicMock(return_value=rate_limited)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_pendle_server_error_handling():
    """Monitor should handle 5xx errors gracefully."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    error_resp = _mock_json_response({}, status=500)
    monitor._session.get = MagicMock(return_value=error_resp)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_pendle_unknown_chain():
    """Unknown chain name should be skipped with no crash."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    os.environ["PENDLE_CHAINS"] = "unknownchain"
    alerts = await monitor.check()
    assert alerts == []
    # Session.get should NOT have been called for an unknown chain
    monitor._session.get.assert_not_called()
