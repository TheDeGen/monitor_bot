"""APScheduler setup — plugin discovery, job registration, enable/disable."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.plugin_base import Alert, MonitorPlugin

logger = logging.getLogger(__name__)


class MonitorScheduler:
    """Wraps APScheduler and manages MonitorPlugin lifecycle."""

    def __init__(self, plugins: list[MonitorPlugin], send_fn, app) -> None:
        """
        Args:
            plugins: Discovered MonitorPlugin instances.
            send_fn: Coroutine ``send_alert(app, alert)`` for dispatching alerts.
            app: The Telegram Application (passed to send_fn).
        """
        self._scheduler = AsyncIOScheduler()
        self._plugins: dict[str, MonitorPlugin] = {p.name: p for p in plugins}
        self._enabled: dict[str, bool] = {p.name: True for p in plugins}
        self._last_check: dict[str, str] = {}
        self._send_fn = send_fn
        self._app = app

    async def start(self) -> None:
        """Call setup() on each plugin, register scheduler jobs, and start."""
        from src import db

        for name, plugin in self._plugins.items():
            try:
                await plugin.setup()
                logger.info("Plugin %s setup complete", name)
            except Exception:
                logger.exception("Plugin %s setup failed — disabling", name)
                self._enabled[name] = False
                continue

            self._scheduler.add_job(
                self._run_check,
                "interval",
                seconds=plugin.interval,
                args=[name],
                id=f"monitor_{name}",
                replace_existing=True,
                max_instances=1,
            )
            logger.info("Scheduled %s every %ds", name, plugin.interval)

        # Hourly purge job
        self._scheduler.add_job(
            self._run_purge,
            "interval",
            hours=1,
            id="purge",
            replace_existing=True,
            max_instances=1,
        )

        self._scheduler.start()
        logger.info("Scheduler started with %d plugins", len(self._plugins))

    async def stop(self) -> None:
        """Teardown plugins and shut down the scheduler."""
        self._scheduler.shutdown(wait=False)
        for name, plugin in self._plugins.items():
            try:
                await plugin.teardown()
            except Exception:
                logger.exception("Plugin %s teardown error", name)
        logger.info("Scheduler stopped")

    def toggle(self, name: str) -> bool | None:
        """Toggle a monitor on/off. Returns new state, or None if not found."""
        name = name.upper()
        if name not in self._plugins:
            return None

        self._enabled[name] = not self._enabled[name]
        job_id = f"monitor_{name}"

        if self._enabled[name]:
            self._scheduler.resume_job(job_id)
            logger.info("Enabled monitor %s", name)
        else:
            self._scheduler.pause_job(job_id)
            logger.info("Disabled monitor %s", name)

        return self._enabled[name]

    def plugin_status(self) -> dict[str, dict[str, Any]]:
        """Return status info for every registered plugin."""
        result: dict[str, dict[str, Any]] = {}
        for name, plugin in self._plugins.items():
            result[name] = {
                "enabled": self._enabled.get(name, False),
                "interval": plugin.interval,
                "last_check": self._last_check.get(name, "never"),
            }
        return result

    # ── Internal ────────────────────────────────────────────────────────

    async def _run_check(self, name: str) -> None:
        """Execute a single plugin check cycle."""
        if not self._enabled.get(name, False):
            return

        plugin = self._plugins[name]
        try:
            alerts = await plugin.check()
            self._last_check[name] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            for alert in alerts:
                try:
                    from src import db

                    await db.insert_alert(
                        alert.monitor, alert.data.get("market", ""), alert.data
                    )
                    await self._send_fn(self._app, alert)
                except Exception:
                    logger.exception("Failed to process alert from %s", name)

            if alerts:
                logger.info("%s produced %d alerts", name, len(alerts))

        except Exception:
            logger.exception("Check failed for %s", name)

    async def _run_purge(self) -> None:
        """Periodic purge of old data."""
        try:
            from src import db

            deleted = await db.purge()
            if deleted:
                logger.info("Purge job removed %d rows", deleted)
        except Exception:
            logger.exception("Purge job failed")
