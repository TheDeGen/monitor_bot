"""Entry point — start bot, database, scheduler, and run until interrupted."""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

# Load .env before anything reads config
load_dotenv()

from src import config, db
from src.bot import build_app, send_alert, set_scheduler
from src.monitors import discover_plugins
from src.scheduler import MonitorScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    # Validate required env vars
    config.validate()

    # Initialise database
    await db.init()

    # Load runtime overrides from settings table
    overrides = await db.all_settings()
    config.load_runtime_overrides(overrides)
    logger.info("Loaded %d runtime overrides from DB", len(overrides))

    # Build Telegram app
    app = build_app()

    # Discover and start monitors
    plugins = discover_plugins()
    logger.info("Discovered %d monitor plugins", len(plugins))

    scheduler = MonitorScheduler(plugins, send_alert, app)
    set_scheduler(scheduler)

    # Initialise the Telegram application
    await app.initialize()
    await app.start()
    await app.updater.start_polling()  # type: ignore[union-attr]

    # Start the scheduler
    await scheduler.start()

    logger.info("Monitor Bot is running. Press Ctrl+C to stop.")

    # Run until cancelled
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down…")
        await scheduler.stop()
        await app.updater.stop()  # type: ignore[union-attr]
        await app.stop()
        await app.shutdown()
        await db.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
