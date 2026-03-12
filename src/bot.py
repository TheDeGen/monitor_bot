"""Telegram bot — command handlers and alert dispatch."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from src import config, db

if TYPE_CHECKING:
    from src.scheduler import MonitorScheduler

logger = logging.getLogger(__name__)

_start_time: float = 0.0
_scheduler: MonitorScheduler | None = None


def set_scheduler(scheduler: MonitorScheduler) -> None:
    """Inject the scheduler reference so commands can interact with it."""
    global _scheduler
    _scheduler = scheduler


# ── Alert dispatch ──────────────────────────────────────────────────────────


async def send_alert(app: Application, alert) -> None:  # type: ignore[type-arg]
    """Format and send an Alert to the configured Telegram chat."""
    from src.plugin_base import Alert

    chat_id = config.get("TELEGRAM_CHAT_ID")
    text = (
        f"🚨 <b>[{alert.monitor}]</b> {alert.title}\n\n"
        f"{alert.body}\n\n"
        f'🔗 <a href="{alert.link}">View</a>'
    )
    try:
        await app.bot.send_message(
            chat_id=int(chat_id),
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Alert sent: [%s] %s", alert.monitor, alert.title)
    except Exception as exc:
        logger.exception("Failed to send alert: [%s] %s — %s", alert.monitor, alert.title, exc)
        raise


# ── Command handlers ───────────────────────────────────────────────────────


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — uptime, active monitors, last check times."""
    uptime_s = int(time.time() - _start_time)
    h, remainder = divmod(uptime_s, 3600)
    m, s = divmod(remainder, 60)

    lines = [f"⏱ <b>Uptime:</b> {h}h {m}m {s}s"]

    if _scheduler:
        for name, info in _scheduler.plugin_status().items():
            status = "✅" if info["enabled"] else "⏸"
            last = info.get("last_check", "never")
            lines.append(f"{status} <b>{name}</b> — last check: {last}")
    else:
        lines.append("No scheduler attached.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recent [monitor] — last 10 alerts."""
    monitor = context.args[0].upper() if context.args else None
    rows = await db.recent_alerts(monitor=monitor, limit=10)

    if not rows:
        await update.message.reply_text("No recent alerts.")  # type: ignore[union-attr]
        return

    lines: list[str] = []
    for r in rows:
        lines.append(f"• <b>[{r['monitor']}]</b> {r['timestamp']}\n  {r['market']}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/config — show all current config values."""
    cfg = config.all_config()
    lines = [f"<code>{k}</code> = {v}" for k, v in sorted(cfg.items())]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]


async def cmd_set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_threshold <monitor> <key> <value>"""
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Usage: /set_threshold <monitor> <key> <value>"
        )
        return

    monitor = context.args[0].upper()
    key = context.args[1].upper()
    value = context.args[2]

    full_key = f"{monitor}_{key}" if not key.startswith(monitor) else key
    await db.set_setting(full_key, value)
    config.set_override(full_key, value)

    await update.message.reply_text(  # type: ignore[union-attr]
        f"✅ Set <code>{full_key}</code> = {value}", parse_mode="HTML"
    )
    logger.info("Config override: %s = %s", full_key, value)


async def cmd_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/toggle <monitor> — enable/disable a monitor."""
    if not context.args:
        await update.message.reply_text("Usage: /toggle <monitor>")  # type: ignore[union-attr]
        return

    name = context.args[0].upper()
    if not _scheduler:
        await update.message.reply_text("Scheduler not available.")  # type: ignore[union-attr]
        return

    new_state = _scheduler.toggle(name)
    if new_state is None:
        await update.message.reply_text(f"Unknown monitor: {name}")  # type: ignore[union-attr]
    else:
        emoji = "✅" if new_state else "⏸"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"{emoji} <b>{name}</b> {'enabled' if new_state else 'disabled'}",
            parse_mode="HTML",
        )


async def cmd_list_monitors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list_monitors — show all discovered plugins."""
    if not _scheduler:
        await update.message.reply_text("Scheduler not available.")  # type: ignore[union-attr]
        return

    lines: list[str] = []
    for name, info in _scheduler.plugin_status().items():
        status = "✅" if info["enabled"] else "⏸"
        lines.append(f"{status} <b>{name}</b> — interval {info['interval']}s")

    await update.message.reply_text(  # type: ignore[union-attr]
        "\n".join(lines) or "No monitors discovered.",
        parse_mode="HTML",
    )


async def cmd_test_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/test_alert — send a sample alert to the current chat."""
    from src.plugin_base import Alert

    test = Alert(
        monitor="TEST",
        title="Test Alert",
        body="This is a test alert to verify the channel is working.",
        link="https://example.com",
        data={"test": True},
    )
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    text = (
        f"🚨 <b>[{test.monitor}]</b> {test.title}\n\n"
        f"{test.body}\n\n"
        f'🔗 <a href="{test.link}">View</a>'
    )
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await update.message.reply_text("\u2705 Test alert sent.")  # type: ignore[union-attr]
    except Exception as exc:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"\u274c Failed to send alert: {exc}"
        )
# ── Application builder ────────────────────────────────────────────────────


def build_app() -> Application:  # type: ignore[type-arg]
    """Build and return the Telegram Application (do not start it yet)."""
    global _start_time
    _start_time = time.time()

    token = config.get("TELEGRAM_BOT_TOKEN")
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("set_threshold", cmd_set_threshold))
    app.add_handler(CommandHandler("toggle", cmd_toggle))
    app.add_handler(CommandHandler("list_monitors", cmd_list_monitors))
    app.add_handler(CommandHandler("test_alert", cmd_test_alert))

    logger.info("Telegram bot application built")
    return app
