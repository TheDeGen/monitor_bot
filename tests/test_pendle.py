"""Tests for the Pendle monitor."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

# Set required env vars before importing our modules
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")

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


@pytest.mark.asyncio
async def test_pendle_init():
    """Plugin should have correct name and interval."""
    monitor = PendleMonitor()
    assert monitor.name == "PENDLE"
    assert monitor.interval == 300


@pytest.mark.asyncio
async def test_chain_ids():
    """Verify chain ID mapping."""
    assert CHAIN_IDS["ethereum"] == 1
    assert CHAIN_IDS["arbitrum"] == 42161
    assert CHAIN_IDS["bnb"] == 56
    assert CHAIN_IDS["optimism"] == 10


@pytest.mark.asyncio
async def test_pendle_check_empty_markets():
    """check() should return empty list when no markets found."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    # Mock DeFi Llama (lending rates)
    defillama_resp = _mock_json_response({"data": []})
    # Mock Pendle markets (empty)
    markets_resp = _mock_json_response({"results": []})

    def route_get(url, **kwargs):
        if "llama" in url:
            return defillama_resp
        return markets_resp

    monitor._session.get = MagicMock(side_effect=route_get)

    # Only check one chain
    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()
    assert alerts == []


@pytest.mark.asyncio
async def test_pendle_spread_alert():
    """Should alert when PT implied APY vs realised APY spread exceeds threshold."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    market_data = {
        "address": "0xtest123",
        "name": "Test Market",
        "chainId": 1,
        "impliedApy": 0.08,  # 8%
        "underlyingApy": 0.05,  # 5% → spread = 3% > 2% threshold
        "ptDiscount": 0.02,
        "pt": {"address": "0xpt", "symbol": "PT-TEST", "price": {"usd": 0.95}},
        "yt": {"address": "0xyt", "symbol": "YT-TEST", "price": {"usd": 0.05}},
        "underlyingAsset": {"symbol": "WETH"},
        "liquidity": {"usd": 1000000},
        "tradingVolume": {"usd": 100000},
    }

    defillama_resp = _mock_json_response({"data": []})
    markets_resp = _mock_json_response({"results": [{"address": "0xtest123"}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if "llama" in url:
            return defillama_resp
        if "/markets/" in url and "0xtest123" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()

    # Should get a spread alert (3% > 2% threshold)
    spread_alerts = [a for a in alerts if a.data.get("check") == "spread"]
    assert len(spread_alerts) == 1
    assert spread_alerts[0].monitor == "PENDLE"
    assert "Spread" in spread_alerts[0].title
    assert spread_alerts[0].data["spread"] == pytest.approx(0.03, abs=0.001)


@pytest.mark.asyncio
async def test_pendle_yt_pricing_alert():
    """Should alert when YT price deviates from (1 - PT price)."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    # PT price 0.90, so expected YT = 0.10, but actual YT = 0.15 → deviation = 50%
    market_data = {
        "address": "0xyt_test",
        "name": "YT Test Market",
        "chainId": 1,
        "impliedApy": 0.04,
        "underlyingApy": 0.04,  # No spread
        "ptDiscount": 0.01,
        "pt": {"address": "0xpt", "symbol": "PT-TEST", "price": {"usd": 0.90}},
        "yt": {"address": "0xyt", "symbol": "YT-TEST", "price": {"usd": 0.15}},
        "underlyingAsset": {"symbol": "USDC"},
        "liquidity": {"usd": 500000},
        "tradingVolume": {"usd": 10000},
    }

    defillama_resp = _mock_json_response({"data": []})
    markets_resp = _mock_json_response({"results": [{"address": "0xyt_test"}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if "llama" in url:
            return defillama_resp
        if "0xyt_test" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()

    yt_alerts = [a for a in alerts if a.data.get("check") == "yt_pricing"]
    assert len(yt_alerts) == 1
    assert yt_alerts[0].data["deviation"] > 0.005  # Above threshold


@pytest.mark.asyncio
async def test_pendle_basis_alert():
    """Should alert when Pendle yield vs lending rate basis exceeds threshold."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    market_data = {
        "address": "0xbasis_test",
        "name": "Basis Test",
        "chainId": 1,
        "impliedApy": 0.10,  # 10%
        "underlyingApy": 0.10,
        "ptDiscount": 0.01,
        "pt": {"address": "0xpt", "symbol": "PT-TEST", "price": {"usd": 0.95}},
        "yt": {"address": "0xyt", "symbol": "YT-TEST", "price": {"usd": 0.05}},
        "underlyingAsset": {"symbol": "WETH"},
        "liquidity": {"usd": 2000000},
        "tradingVolume": {"usd": 50000},
    }

    # Lending rate for WETH at 5% → basis = 10% - 5% = 5% > 3% threshold
    defillama_resp = _mock_json_response(
        {
            "data": [
                {
                    "project": "aave-v3",
                    "symbol": "weth",
                    "apy": 5.0,  # 5% (will be divided by 100)
                }
            ]
        }
    )
    markets_resp = _mock_json_response({"results": [{"address": "0xbasis_test"}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if "llama" in url:
            return defillama_resp
        if "0xbasis_test" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()

    basis_alerts = [a for a in alerts if a.data.get("check") == "basis"]
    assert len(basis_alerts) == 1
    assert basis_alerts[0].data["basis"] == pytest.approx(0.05, abs=0.001)


@pytest.mark.asyncio
async def test_pendle_deduplication():
    """Same alert type + market should not fire again within 6 hours."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    market_data = {
        "address": "0xdedup",
        "name": "Dedup Market",
        "chainId": 1,
        "impliedApy": 0.08,
        "underlyingApy": 0.05,
        "ptDiscount": 0.02,
        "pt": {"address": "0xpt", "symbol": "PT-TEST", "price": {"usd": 0.95}},
        "yt": {"address": "0xyt", "symbol": "YT-TEST", "price": {"usd": 0.05}},
        "underlyingAsset": {"symbol": "DAI"},
        "liquidity": {"usd": 1000000},
        "tradingVolume": {"usd": 50000},
    }

    defillama_resp = _mock_json_response({"data": []})
    markets_resp = _mock_json_response({"results": [{"address": "0xdedup"}]})
    detail_resp = _mock_json_response(market_data)

    def route_get(url, **kwargs):
        if "llama" in url:
            return defillama_resp
        if "0xdedup" in url:
            return detail_resp
        if "/markets" in url:
            return markets_resp
        return _mock_json_response({})

    monitor._session.get = MagicMock(side_effect=route_get)
    os.environ["PENDLE_CHAINS"] = "ethereum"

    # First check
    alerts1 = await monitor.check()
    spread_alerts1 = [a for a in alerts1 if a.data.get("check") == "spread"]
    assert len(spread_alerts1) == 1

    # Insert into DB
    for a in alerts1:
        await db.insert_alert(a.monitor, a.data.get("market", ""), a.data)

    # Second check — should be deduplicated
    alerts2 = await monitor.check()
    spread_alerts2 = [a for a in alerts2 if a.data.get("check") == "spread"]
    assert len(spread_alerts2) == 0


@pytest.mark.asyncio
async def test_pendle_rate_limit_handling():
    """Monitor should handle rate limits gracefully."""
    monitor = PendleMonitor()
    monitor._session = AsyncMock()

    defillama_resp = _mock_json_response({"data": []})
    rate_limited = _mock_json_response({}, status=429)

    def route_get(url, **kwargs):
        if "llama" in url:
            return defillama_resp
        return rate_limited

    monitor._session.get = MagicMock(side_effect=route_get)

    os.environ["PENDLE_CHAINS"] = "ethereum"
    alerts = await monitor.check()
    assert alerts == []
