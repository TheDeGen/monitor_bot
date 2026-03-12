# Monitor Bot

Real-time crypto market monitoring bot for Telegram. Tracks Polymarket trades and Pendle yield dislocations, delivering alerts to your chat.

## Features

**Polymarket Monitor**
- Detects large trades on low-odds markets (configurable thresholds)
- Filters by wallet age via Polygonscan (caches lookups in SQLite)
- Flags thin markets with fewer than 5 unique participants
- Deduplicates alerts per market+wallet within 6 hours

**Pendle Monitor**
- Scans markets across Ethereum, Arbitrum, BNB, and Optimism
- Detects 5 types of yield dislocations:
  - PT implied vs realised yield spread
  - PT discount widening (1h comparison)
  - YT pricing inconsistency vs `(1 - PT price)`
  - Basis vs Aave/Compound lending rates (via DeFi Llama)
  - Smart money entries (top 1% by size)

**Telegram Commands**
| Command | Description | Access |
|---------|-------------|--------|
| `/status` | Uptime, active monitors, last check times | Public |
| `/recent [monitor]` | Last 10 alerts, optionally filtered | Public |
| `/list_monitors` | Show all plugins with status and interval | Public |
| `/test_alert` | Send a sample alert to verify the channel | Public |
| `/chatid` | Show the current chat's numeric ID | Public |
| `/config` | Current configuration values | Admin |
| `/set_threshold <monitor> <key> <value>` | Update a threshold at runtime | Admin |
| `/toggle <monitor>` | Enable/disable a monitor | Admin |

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Compose
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))

### Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/TheDeGen/monitor_bot.git
   cd monitor_bot
   ```

2. Create `.env` from the example:
   ```bash
   cp .env.example .env
   ```

3. Fill in your credentials in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_CHAT_IDS=your-chat-id,-100your-group-id
   ADMIN_USER_IDS=your-telegram-user-id
   ```

4. Run with Docker Compose:
   ```bash
   docker compose up -d --build
   ```

The bot starts polling Telegram and running monitors immediately. Data is persisted in `./data/monitor.db`.

### Run Locally (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.main
```

## Configuration

All settings are loaded from environment variables with sensible defaults. Runtime overrides via `/set_threshold` are persisted in SQLite and take precedence.

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *required* | Telegram bot API token |
| `TELEGRAM_CHAT_IDS` | *required* | Comma-separated chat IDs for alert delivery (DMs and/or groups) |
| `TELEGRAM_CHAT_ID` | — | Legacy single chat ID (fallback if `TELEGRAM_CHAT_IDS` is not set) |
| `ADMIN_USER_IDS` | *(empty)* | Comma-separated Telegram user IDs allowed to run admin commands. Empty = unrestricted |
| `POLY_THRESHOLD` | `10000` | Minimum trade size ($) |
| `POLY_MAX_WALLET_AGE_DAYS` | `90` | Max wallet age filter (days) |
| `POLY_MAX_ODDS` | `0.20` | Max market odds filter |
| `PENDLE_CHAINS` | `ethereum,arbitrum,bnb,optimism` | Chains to scan |
| `DB_PATH` | `data/monitor.db` | SQLite database path |
| `PURGE_HOURS` | `48` | Auto-purge data older than N hours |
| `POLYGONSCAN_API_KEY` | *optional* | For wallet age lookups |

## Project Structure

```
monitor_bot/
├── src/
│   ├── main.py              # Entry point
│   ├── config.py            # Env var loading + runtime overrides
│   ├── bot.py               # Telegram handlers + alert dispatch
│   ├── db.py                # Async SQLite (alerts, snapshots, settings, wallet_cache)
│   ├── scheduler.py         # APScheduler wrapper + plugin lifecycle
│   ├── plugin_base.py       # Alert dataclass + MonitorPlugin ABC
│   └── monitors/
│       ├── __init__.py      # Auto-discovery of MonitorPlugin subclasses
│       ├── polymarket.py    # Polymarket trade monitor
│       └── pendle.py        # Pendle yield dislocation monitor
├── tests/
│   ├── test_polymarket.py
│   └── test_pendle.py
├── Dockerfile
├── docker-compose.yml
├── .github/workflows/deploy.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

## Plugin Architecture

Monitors are auto-discovered at startup. To add a new monitor, create a file in `src/monitors/` that subclasses `MonitorPlugin`:

```python
from src.plugin_base import Alert, MonitorPlugin

class MyMonitor(MonitorPlugin):
    name = "MYMONITOR"
    interval = 120  # seconds

    async def check(self) -> list[Alert]:
        # Your monitoring logic here
        return [Alert(
            monitor=self.name,
            title="Something happened",
            body="Details...",
            link="https://example.com",
            data={"key": "value"},
        )]
```

The scheduler picks it up automatically — no registration needed.

## Deployment

Pushes to `main` trigger GitHub Actions, which SSHs into the VPS and runs:

```bash
git pull && docker compose up -d --build
```

Required GitHub Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`.

## Tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

## License

Private repository.
