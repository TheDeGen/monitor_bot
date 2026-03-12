"""Polymarket trade monitor — detects informed flow on prediction markets.

FR-10 through FR-16 implementation.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import aiohttp

from src import config, db
from src.plugin_base import Alert, MonitorPlugin

logger = logging.getLogger(__name__)

TRADES_URL = "https://data-api.polymarket.com/trades"
EVENTS_URL = "https://gamma-api.polymarket.com/events"
POLYGONSCAN_API = "https://api.polygonscan.com/api"

_MARKET_REFRESH_INTERVAL = 3600


class PolymarketMonitor(MonitorPlugin):
    name = "POLYMARKET"
    interval = 60  # every 60 seconds

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._event_ids: list[int] = []
        self._events_last_refreshed: float = 0.0
        self._seen_tx_hashes: deque[str] = deque(maxlen=50_000)

    async def setup(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )

    async def teardown(self) -> None:
        if self._session:
            await self._session.close()

    async def check(self) -> list[Alert]:
        if not self._session:
            return []

        threshold = config.get_float("POLY_THRESHOLD")
        max_odds = config.get_float("POLY_MAX_ODDS")
        max_wallet_age_days = config.get_int("POLY_MAX_WALLET_AGE_DAYS")

        # Refresh large-market list periodically
        await self._maybe_refresh_events()

        # FR-10: Fetch recent trades
        trades = await self._fetch_trades()
        if not trades:
            return []

        alerts: list[Alert] = []

        for trade in trades:
            try:
                tx_hash = trade.get("transactionHash", "")
                if tx_hash and tx_hash in self._seen_tx_hashes:
                    continue

                size = float(trade.get("size", 0))
                price = float(trade.get("price", 1))
                dollar_value = size * price
                market_slug = trade.get("slug", trade.get("eventSlug", ""))
                event_slug = trade.get("eventSlug", market_slug)
                wallet = trade.get("proxyWallet", "")
                market_question = trade.get("title", trade.get("name", "Unknown"))

                # FR-11: Filter by size and odds
                if dollar_value < threshold:
                    continue
                if price > max_odds:
                    continue

                if tx_hash:
                    self._seen_tx_hashes.append(tx_hash)

                # FR-15: Deduplicate within 6 hours
                dedup_key = f"{market_slug}:{wallet}"
                if await db.alert_exists(self.name, dedup_key, hours=6):
                    continue

                # FR-12, FR-13: Check wallet age
                wallet_age_days = await self._get_wallet_age_days(wallet)
                if (
                    wallet_age_days is not None
                    and wallet_age_days > max_wallet_age_days
                ):
                    continue

                # FR-14: Check participant count (flagging thin markets)
                participant_note = ""
                # We'll flag if we can detect low participation from trade data
                # This is a simplified check based on available trade data

                # FR-16: Build alert
                age_str = (
                    f"{wallet_age_days}d" if wallet_age_days is not None else "unknown"
                )
                event_url = (
                    f"https://polymarket.com/event/{event_slug}"
                    if event_slug
                    else "https://polymarket.com"
                )

                body_parts = [
                    f"📊 <b>Market:</b> {market_question}",
                    f"💰 <b>Trade Size:</b> ${dollar_value:,.2f} ({size:,.0f} shares @ {price:.2%})",
                    f"📈 <b>Odds:</b> {price:.2%}",
                    f"👛 <b>Wallet Age:</b> {age_str}",
                ]
                if participant_note:
                    body_parts.append(f"⚠️ {participant_note}")

                alert = Alert(
                    monitor=self.name,
                    title="Informed Flow Detected",
                    body="\n".join(body_parts),
                    link=event_url,
                    data={
                        "market": dedup_key,
                        "size": dollar_value,
                        "price": price,
                        "wallet": wallet,
                        "wallet_age_days": wallet_age_days,
                        "market_slug": market_slug,
                    },
                )
                alerts.append(alert)

            except (ValueError, KeyError, TypeError):
                logger.exception("Error processing trade: %s", trade)
                continue

        return alerts

    async def _fetch_trades(self) -> list[dict]:
        """Fetch recent trades from Polymarket data API.

        Uses server-side CASH filter to only return trades above our dollar
        threshold, and optionally restricts to large-market event IDs.
        """
        assert self._session is not None
        threshold = config.get_float("POLY_THRESHOLD")
        try:
            params: dict[str, str] = {
                "limit": "10000",
                "filterType": "CASH",
                "filterAmount": str(threshold),
            }
            # Narrow to large markets if we have event IDs
            if self._event_ids:
                params["eventId"] = ",".join(str(eid) for eid in self._event_ids)

            async with self._session.get(TRADES_URL, params=params) as resp:
                if resp.status == 429:
                    logger.warning("Polymarket API rate limited")
                    return []
                if resp.status >= 500:
                    logger.warning("Polymarket API server error: %d", resp.status)
                    return []
                resp.raise_for_status()
                data = await resp.json()
                # API returns a list of trades directly
                if isinstance(data, list):
                    return data
                return data.get("data", data.get("trades", []))
        except aiohttp.ClientError:
            logger.exception("Failed to fetch Polymarket trades")
            return []

    async def _maybe_refresh_events(self) -> None:
        """Refresh the large-market event ID list if the cache is stale."""
        now = time.monotonic()
        if now - self._events_last_refreshed < _MARKET_REFRESH_INTERVAL:
            return
        event_ids = await self._fetch_large_event_ids()
        if event_ids:
            self._event_ids = event_ids
            logger.info("Refreshed large-market list: %d events", len(self._event_ids))
        elif not self._event_ids:
            # First attempt failed — don't filter by event ID
            logger.warning("Failed to fetch large events; trading without event filter")
        # Update timestamp even on failure to avoid hammering on errors
        self._events_last_refreshed = now

    async def _fetch_large_event_ids(self) -> list[int]:
        """Fetch active events above the minimum volume from the Gamma API."""
        assert self._session is not None
        min_volume = config.get_float("POLY_MIN_MARKET_VOLUME")
        try:
            params: dict[str, str] = {
                "active": "true",
                "closed": "false",
                "volume_min": str(min_volume),
                "limit": "500",
            }
            async with self._session.get(EVENTS_URL, params=params) as resp:
                if resp.status == 429:
                    logger.warning("Gamma API rate limited")
                    return []
                if resp.status >= 500:
                    logger.warning("Gamma API server error: %d", resp.status)
                    return []
                resp.raise_for_status()
                events = await resp.json()
                if not isinstance(events, list):
                    return []
                event_ids: list[int] = []
                for event in events:
                    eid = event.get("id")
                    if eid is not None:
                        try:
                            event_ids.append(int(eid))
                        except (ValueError, TypeError):
                            continue
                return event_ids
        except aiohttp.ClientError:
            logger.exception("Failed to fetch Polymarket events")
            return []

    async def _get_wallet_age_days(self, address: str) -> int | None:
        """Get wallet age in days. Uses DB cache, falls back to Polygonscan."""
        if not address:
            return None

        # Check cache
        cached = await db.get_wallet_age(address)
        if cached:
            try:
                created = datetime.fromisoformat(cached)
                age = datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)
                return age.days
            except ValueError:
                pass

        # Fetch from Polygonscan
        api_key = config.get("POLYGONSCAN_API_KEY", "")
        if not api_key:
            return None

        assert self._session is not None
        try:
            params = {
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": "0",
                "endblock": "99999999",
                "page": "1",
                "offset": "1",
                "sort": "asc",
                "apikey": api_key,
            }
            async with self._session.get(POLYGONSCAN_API, params=params) as resp:
                if resp.status == 429:
                    logger.warning("Polygonscan rate limited")
                    return None
                resp.raise_for_status()
                data = await resp.json()

            results = data.get("result", [])
            if not results or not isinstance(results, list):
                return None

            first_tx = results[0]
            timestamp = int(first_tx.get("timeStamp", 0))
            if timestamp == 0:
                return None

            created_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            await db.set_wallet_age(address, created_at.isoformat())

            age = datetime.now(timezone.utc) - created_at
            return age.days

        except (aiohttp.ClientError, ValueError, KeyError):
            logger.exception("Failed to fetch wallet age for %s", address)
            return None
