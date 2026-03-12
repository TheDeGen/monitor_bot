"""Pendle yield dislocation monitor.

Detects PT discount widening (sudden jumps) across multiple chains
for Pendle Finance markets.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiohttp

from src import config, db
from src.plugin_base import Alert, MonitorPlugin

logger = logging.getLogger(__name__)

# Pendle chain ID mapping
CHAIN_IDS: dict[str, int] = {
    "ethereum": 1,
    "arbitrum": 42161,
    "bnb": 56,
    "optimism": 10,
    "base": 8453,
    "sonic": 146,
    "hyperevm": 999,
    "plasma": 9745,
}

PENDLE_API = "https://api-v2.pendle.finance/core/v1"


class PendleMonitor(MonitorPlugin):
    name = "PENDLE"
    interval = 90  # every 90 seconds

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def setup(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
        )

    async def teardown(self) -> None:
        if self._session:
            await self._session.close()

    async def check(self) -> list[Alert]:
        if not self._session:
            return []

        chains = config.get_list("PENDLE_CHAINS")
        alerts: list[Alert] = []

        for chain_name in chains:
            chain_id = CHAIN_IDS.get(chain_name.lower())
            if chain_id is None:
                logger.warning("Unknown Pendle chain: %s", chain_name)
                continue

            try:
                chain_alerts = await self._check_chain(chain_name, chain_id)
                alerts.extend(chain_alerts)
            except Exception:
                logger.exception("Error checking Pendle chain %s", chain_name)

        return alerts

    async def _check_chain(self, chain_name: str, chain_id: int) -> list[Alert]:
        """Check all markets on a single chain."""
        assert self._session is not None
        alerts: list[Alert] = []

        markets = await self._fetch_markets(chain_id)
        if not markets:
            return []

        for market_summary in markets:
            try:
                address = market_summary.get("address", "")
                if not address:
                    continue

                market = await self._fetch_market_detail(chain_id, address)
                if not market:
                    continue

                # Store snapshot for historical comparison
                snapshot_data = {
                    "market_address": address,
                    "chain": chain_name,
                    "chain_id": chain_id,
                    "name": market.get("name", ""),
                    "pt_discount": market.get("ptDiscount", 0),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                await db.insert_snapshot(self.name, snapshot_data)

                # Check for PT discount widening over the last 4 hours
                market_alerts = await self._check_discount_widening(
                    chain_name, address, market, snapshot_data
                )
                alerts.extend(market_alerts)

            except Exception:
                logger.exception(
                    "Error processing Pendle market %s on %s",
                    market_summary.get("address", "?"),
                    chain_name,
                )

        return alerts

    async def _check_discount_widening(
        self,
        chain_name: str,
        address: str,
        market: dict,
        snapshot: dict,
    ) -> list[Alert]:
        """Alert when PT discount jumps more than 1% over the last 4 hours."""
        alerts: list[Alert] = []
        market_name = market.get("name", address[:16])
        market_link = f"https://app.pendle.finance/trade/markets/{address}"

        discount_threshold = config.get_float("PENDLE_DISCOUNT_THRESHOLD", 0.01)
        pt_discount = snapshot["pt_discount"]

        old_snapshots = await db.get_snapshots(
            self.name, hours=4, market_address=address
        )
        if not old_snapshots:
            return []

        try:
            oldest = json.loads(old_snapshots[0]["data_json"])
            old_discount = oldest.get("pt_discount", 0)
            discount_change = pt_discount - old_discount

            if discount_change > discount_threshold:
                dedup_key = f"{address}:discount"
                if not await db.alert_exists(self.name, dedup_key, hours=6):
                    alerts.append(
                        Alert(
                            monitor=self.name,
                            title="PT Discount Widening",
                            body=(
                                f"🏦 <b>Chain:</b> {chain_name}\n"
                                f"📊 <b>Market:</b> {market_name}\n"
                                f"📉 <b>Current Discount:</b> {pt_discount:.2%}\n"
                                f"📊 <b>4h Ago:</b> {old_discount:.2%}\n"
                                f"⚡ <b>Change:</b> +{discount_change:.2%}"
                            ),
                            link=market_link,
                            data={
                                "market": dedup_key,
                                "check": "discount_widening",
                                "current_discount": pt_discount,
                                "old_discount": old_discount,
                                "change": discount_change,
                            },
                        )
                    )
        except (json.JSONDecodeError, KeyError):
            pass

        return alerts

    # ── API helpers ─────────────────────────────────────────────────────

    async def _fetch_markets(self, chain_id: int) -> list[dict]:
        """Fetch all active markets for a chain."""
        assert self._session is not None
        url = f"{PENDLE_API}/{chain_id}/markets"
        try:
            async with self._session.get(
                url, params={"order_by": "name:1", "skip": 0, "limit": 100}
            ) as resp:
                if resp.status == 429:
                    logger.warning("Pendle API rate limited for chain %d", chain_id)
                    return []
                if resp.status >= 500:
                    logger.warning(
                        "Pendle API error %d for chain %d", resp.status, chain_id
                    )
                    return []
                resp.raise_for_status()
                data = await resp.json()
                # API may wrap in {"results": [...]} or return list directly
                if isinstance(data, list):
                    return data
                return data.get("results", data.get("markets", []))
        except aiohttp.ClientError:
            logger.exception("Failed to fetch Pendle markets for chain %d", chain_id)
            return []

    async def _fetch_market_detail(self, chain_id: int, address: str) -> dict | None:
        """Fetch detailed market data."""
        assert self._session is not None
        url = f"{PENDLE_API}/{chain_id}/markets/{address}"
        try:
            async with self._session.get(url) as resp:
                if resp.status == 429:
                    logger.warning("Pendle API rate limited")
                    return None
                if resp.status >= 400:
                    return None
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError:
            logger.exception("Failed to fetch Pendle market %s", address)
            return None
