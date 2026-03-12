"""Pendle yield dislocation monitor.

FR-20 through FR-24 implementation — detects yield anomalies across
multiple chains for Pendle Finance markets.
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
}

PENDLE_API = "https://api-v2.pendle.finance/core/v1"
DEFILLAMA_YIELDS = "https://yields.llama.fi/pools"


class PendleMonitor(MonitorPlugin):
    name = "PENDLE"
    interval = 300  # every 5 minutes

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._lending_rates: dict[str, float] = {}  # pool -> apy

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

        # Refresh lending rates for basis comparison (FR-23d)
        await self._refresh_lending_rates()

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

        # FR-20: Fetch all markets
        markets = await self._fetch_markets(chain_id)
        if not markets:
            return []

        for market_summary in markets:
            try:
                address = market_summary.get("address", "")
                if not address:
                    continue

                # FR-21: Fetch detailed market data
                market = await self._fetch_market_detail(chain_id, address)
                if not market:
                    continue

                # Store snapshot (FR-22)
                snapshot_data = {
                    "market_address": address,
                    "chain": chain_name,
                    "chain_id": chain_id,
                    "name": market.get("name", ""),
                    "pt_price": market.get("pt", {}).get("price", {}).get("usd", 0),
                    "yt_price": market.get("yt", {}).get("price", {}).get("usd", 0),
                    "implied_apy": market.get("impliedApy", 0),
                    "underlying_apy": market.get("underlyingApy", 0),
                    "pt_discount": market.get("ptDiscount", 0),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                await db.insert_snapshot(self.name, snapshot_data)

                # Run detection checks (FR-23)
                market_alerts = await self._run_checks(
                    chain_name, chain_id, address, market, snapshot_data
                )
                alerts.extend(market_alerts)

            except Exception:
                logger.exception(
                    "Error processing Pendle market %s on %s",
                    market_summary.get("address", "?"),
                    chain_name,
                )

        return alerts

    async def _run_checks(
        self,
        chain_name: str,
        chain_id: int,
        address: str,
        market: dict,
        snapshot: dict,
    ) -> list[Alert]:
        """Run all FR-23 detection checks on a single market."""
        alerts: list[Alert] = []
        market_name = market.get("name", address[:16])
        market_link = f"https://app.pendle.finance/trade/markets/{address}"

        # Thresholds (configurable via /set_threshold)
        spread_threshold = config.get_float("PENDLE_SPREAD_THRESHOLD", 0.02)
        discount_threshold = config.get_float("PENDLE_DISCOUNT_THRESHOLD", 0.01)
        yt_deviation_threshold = config.get_float(
            "PENDLE_YT_DEVIATION_THRESHOLD", 0.005
        )
        basis_threshold = config.get_float("PENDLE_BASIS_THRESHOLD", 0.03)

        implied_apy = snapshot["implied_apy"]
        underlying_apy = snapshot["underlying_apy"]
        pt_discount = snapshot["pt_discount"]
        pt_price = snapshot["pt_price"]
        yt_price = snapshot["yt_price"]

        # ── FR-23a: PT implied vs realised yield ──────────────────────────
        if underlying_apy > 0:
            spread = abs(implied_apy - underlying_apy)
            if spread > spread_threshold:
                dedup_key = f"{address}:spread"
                if not await db.alert_exists(self.name, dedup_key, hours=6):
                    alerts.append(
                        Alert(
                            monitor=self.name,
                            title="PT Yield Spread",
                            body=(
                                f"🏦 <b>Chain:</b> {chain_name}\n"
                                f"📊 <b>Market:</b> {market_name}\n"
                                f"📈 <b>PT Implied APY:</b> {implied_apy:.2%}\n"
                                f"📉 <b>Realised APY (7d):</b> {underlying_apy:.2%}\n"
                                f"⚡ <b>Spread:</b> {spread:.2%}"
                            ),
                            link=market_link,
                            data={
                                "market": dedup_key,
                                "check": "spread",
                                "implied_apy": implied_apy,
                                "underlying_apy": underlying_apy,
                                "spread": spread,
                            },
                        )
                    )

        # ── FR-23b: PT discount widening ──────────────────────────────────
        old_snapshots = await db.get_snapshots(
            self.name, hours=1, market_address=address
        )
        if old_snapshots:
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
                                    f"📊 <b>1h Ago:</b> {old_discount:.2%}\n"
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

        # ── FR-23c: YT pricing inconsistency ─────────────────────────────
        if pt_price > 0:
            expected_yt = 1.0 - pt_price
            if expected_yt > 0:
                deviation = abs(yt_price - expected_yt) / expected_yt
                if deviation > yt_deviation_threshold:
                    dedup_key = f"{address}:yt_pricing"
                    if not await db.alert_exists(self.name, dedup_key, hours=6):
                        alerts.append(
                            Alert(
                                monitor=self.name,
                                title="YT Pricing Inconsistency",
                                body=(
                                    f"🏦 <b>Chain:</b> {chain_name}\n"
                                    f"📊 <b>Market:</b> {market_name}\n"
                                    f"💲 <b>YT Price:</b> ${yt_price:.4f}\n"
                                    f"💲 <b>Expected (1-PT):</b> ${expected_yt:.4f}\n"
                                    f"⚡ <b>Deviation:</b> {deviation:.2%}"
                                ),
                                link=market_link,
                                data={
                                    "market": dedup_key,
                                    "check": "yt_pricing",
                                    "yt_price": yt_price,
                                    "expected_yt": expected_yt,
                                    "deviation": deviation,
                                },
                            )
                        )

        # ── FR-23d: Basis vs lending rates ────────────────────────────────
        underlying_symbol = market.get("underlyingAsset", {}).get("symbol", "").lower()
        lending_apy = self._lending_rates.get(underlying_symbol, 0)
        if lending_apy > 0 and implied_apy > 0:
            basis = implied_apy - lending_apy
            if basis > basis_threshold:
                dedup_key = f"{address}:basis"
                if not await db.alert_exists(self.name, dedup_key, hours=6):
                    alerts.append(
                        Alert(
                            monitor=self.name,
                            title="Yield Basis vs Lending",
                            body=(
                                f"🏦 <b>Chain:</b> {chain_name}\n"
                                f"📊 <b>Market:</b> {market_name}\n"
                                f"📈 <b>Pendle Yield:</b> {implied_apy:.2%}\n"
                                f"🏪 <b>Lending Rate:</b> {lending_apy:.2%}\n"
                                f"⚡ <b>Basis:</b> {basis:.2%}"
                            ),
                            link=market_link,
                            data={
                                "market": dedup_key,
                                "check": "basis",
                                "pendle_yield": implied_apy,
                                "lending_rate": lending_apy,
                                "basis": basis,
                            },
                        )
                    )

        # ── FR-23e: Smart money entries (large transactions) ──────────────
        # Detected via unusually large liquidity or volume relative to market
        liquidity = market.get("liquidity", {}).get("usd", 0)
        volume_24h = market.get("tradingVolume", {}).get("usd", 0)
        if liquidity > 0 and volume_24h > 0:
            # Flag if 24h volume > 50% of liquidity (unusual concentration)
            vol_ratio = volume_24h / liquidity
            if vol_ratio > 0.5:
                dedup_key = f"{address}:smart_money"
                if not await db.alert_exists(self.name, dedup_key, hours=6):
                    alerts.append(
                        Alert(
                            monitor=self.name,
                            title="Smart Money Entry Detected",
                            body=(
                                f"🏦 <b>Chain:</b> {chain_name}\n"
                                f"📊 <b>Market:</b> {market_name}\n"
                                f"💰 <b>24h Volume:</b> ${volume_24h:,.0f}\n"
                                f"🏊 <b>Liquidity:</b> ${liquidity:,.0f}\n"
                                f"⚡ <b>Vol/Liq Ratio:</b> {vol_ratio:.1%}"
                            ),
                            link=market_link,
                            data={
                                "market": dedup_key,
                                "check": "smart_money",
                                "volume_24h": volume_24h,
                                "liquidity": liquidity,
                                "vol_ratio": vol_ratio,
                            },
                        )
                    )

        return alerts

    # ── API helpers ─────────────────────────────────────────────────────

    async def _fetch_markets(self, chain_id: int) -> list[dict]:
        """FR-20: Fetch all active markets for a chain."""
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
        """FR-21: Fetch detailed market data."""
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

    async def _refresh_lending_rates(self) -> None:
        """FR-23d: Fetch current lending rates from DeFi Llama."""
        assert self._session is not None
        try:
            async with self._session.get(DEFILLAMA_YIELDS) as resp:
                if resp.status != 200:
                    logger.warning("DeFi Llama yields API returned %d", resp.status)
                    return
                data = await resp.json()

            pools = data.get("data", [])
            rates: dict[str, float] = {}

            for pool in pools:
                project = pool.get("project", "").lower()
                symbol = pool.get("symbol", "").lower()
                apy = pool.get("apy", 0)

                # Only Aave and Compound supply rates
                if project in ("aave-v3", "aave-v2", "compound-v3", "compound-v2"):
                    # Keep the highest rate per symbol
                    if symbol not in rates or apy > rates[symbol]:
                        rates[symbol] = apy / 100.0  # Convert percentage to decimal

            self._lending_rates = rates
            logger.debug("Refreshed %d lending rates from DeFi Llama", len(rates))

        except aiohttp.ClientError:
            logger.exception("Failed to refresh lending rates")
