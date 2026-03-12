
# PRD: Monitor Bot

## 1. Product Overview

**Name:** Monitor Bot
**Type:** Python Telegram bot for real-time crypto market monitoring
**Deployment:** Docker container on a Linux VPS, auto-deployed via GitHub Actions on push to `main`

## 2. Technical Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.12+ |
| Telegram | `python-telegram-bot` (async) |
| Scheduler | `APScheduler` |
| HTTP client | `aiohttp` |
| Database | `aiosqlite` (async SQLite) |
| Container | Docker + `docker-compose` |
| CI/CD | GitHub Actions |

## 3. File Structure

```
monitor-bot/
├── .github/
│   └── workflows/
│       └── deploy.yml
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point: start bot + scheduler
│   ├── config.py            # Load env vars, provide defaults
│   ├── bot.py               # Telegram bot, command handlers, send_alert()
│   ├── db.py                # SQLite init, insert, query, purge helpers
│   ├── scheduler.py         # APScheduler setup, plugin discovery, job registration
│   ├── plugin_base.py       # Alert dataclass + MonitorPlugin ABC
│   └── monitors/
│       ├── __init__.py      # Auto-discovery: scan for MonitorPlugin subclasses
│       ├── polymarket.py
│       └── pendle.py
├── tests/
│   ├── test_polymarket.py
│   └── test_pendle.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## 4. Build Order

Build and verify each layer before moving to the next:

1. `src/plugin_base.py`
2. `src/config.py`
3. `src/db.py`
4. `src/bot.py`
5. `src/scheduler.py`
6. `src/monitors/__init__.py`
7. `src/monitors/polymarket.py`
8. `src/monitors/pendle.py`
9. `src/main.py`
10. `Dockerfile` + `docker-compose.yml`
11. `.github/workflows/deploy.yml`
12. `tests/`

## 5. Functional Requirements

### 5.1 Core System

- **FR-01:** The bot shall run as a long-lived async Python process inside a Docker container.
- **FR-02:** The bot shall use a plugin architecture. Each monitor is a Python module in `src/monitors/` that inherits from `MonitorPlugin` (defined in `src/plugin_base.py`). The scheduler auto-discovers plugins at startup by scanning the `src/monitors/` directory for `MonitorPlugin` subclasses.
- **FR-03:** The bot shall use APScheduler to run each plugin's `check()` method at its configured `interval` (in seconds).
- **FR-04:** The bot shall send alerts to the configured Telegram chat (personal DM or group), prefixed with the monitor's tag (e.g. `[POLYMARKET]`, `[PENDLE]`). The bot shall respond to commands in any chat where it receives them (personal or group).
- **FR-05:** The bot shall store alerts, time-series snapshots, and runtime settings in a SQLite database at `data/monitor.db`.
- **FR-06:** A purge job shall run every hour, deleting rows older than `PURGE_HOURS` (default 48) from the `alerts` and `snapshots` tables.

### 5.2 Plugin Base Class (`src/plugin_base.py`)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class Alert:
    monitor: str        # e.g. "POLYMARKET"
    title: str          # e.g. "Informed Flow Detected"
    body: str           # formatted alert message body
    link: str           # URL to the relevant market
    data: dict          # raw data for storage in SQLite

class MonitorPlugin(ABC):
    name: str           # tag used in alerts
    interval: int       # seconds between check() calls

    @abstractmethod
    async def check(self) -> list[Alert]:
        """Run one monitoring cycle. Return a list of Alert objects to send."""
        ...

    async def setup(self) -> None:
        """Called once at startup. Override for initialisation."""
        pass

    async def teardown(self) -> None:
        """Called on shutdown. Override for cleanup."""
        pass
```

### 5.3 Polymarket Monitor (`src/monitors/polymarket.py`)

- **FR-10:** Poll `GET https://gamma-api.polymarket.com/trades` every 60 seconds.
- **FR-11:** Filter trades where `size >= POLY_THRESHOLD` (default $10,000) AND market odds <= `POLY_MAX_ODDS` (default 0.20).
- **FR-12:** Look up the wallet's on-chain creation timestamp via Polygonscan's free API. Cache wallet ages in a dedicated SQLite table (`wallet_cache`: `address`, `created_at`, `fetched_at`) to avoid repeated lookups.
- **FR-13:** Filter where wallet age <= `POLY_MAX_WALLET_AGE_DAYS` (default 90).
- **FR-14:** Additionally flag markets where price moved significantly with fewer than 5 unique participants in the past hour.
- **FR-15:** Deduplicate: do not re-alert on the same `market + wallet` combination within 6 hours (check against `alerts` table).
- **FR-16:** Each alert must include: market question, current odds, trade size, wallet age, participant count, and a direct link to the Polymarket event page.

### 5.4 Pendle Monitor (`src/monitors/pendle.py`)

- **FR-20:** For each chain in `PENDLE_CHAINS`, fetch all markets from `GET https://api-v2.pendle.finance/core/v1/{chainId}/markets` every 5 minutes.
- **FR-21:** For each active market, fetch details from `GET https://api-v2.pendle.finance/core/v1/{chainId}/markets/{address}`.
- **FR-22:** Store yield snapshots in the `snapshots` table (keyed by `timestamp + monitor + market_address`) for trend detection.
- **FR-23:** Implement these detection checks, each with a configurable threshold:

| ID | Check | Logic | Default Threshold |
|----|-------|-------|-------------------|
| FR-23a | PT implied vs realised yield | Compare PT implied APY with underlying protocol's trailing 7d realised APY | Spread > 2% |
| FR-23b | PT discount widening | Compare current PT discount to the snapshot from 1h ago | Widens > 1% in 1h |
| FR-23c | YT pricing inconsistency | Compare YT price with `(1 - PT price)` | Deviation > 0.5% |
| FR-23d | Basis vs lending rates | Compare Pendle yield with Aave/Compound supply rates from DeFi Llama (`https://yields.llama.fi/pools`) | Basis > 3% |
| FR-23e | Smart money entries | Detect unusually large transactions into specific maturities | Top 1% by size |

- **FR-24:** Each alert must include: chain name, market name, the specific dislocation type, the relevant numbers (e.g. "PT yield 8.2% vs realised 5.1%, spread 3.1%"), and a direct link to the Pendle market page.

### 5.5 Telegram Commands

- **FR-30:** `/status` — respond with bot uptime, active monitors, and last check time for each.
- **FR-31:** `/recent [monitor]` — list the last 10 alerts, optionally filtered by monitor name.
- **FR-32:** `/config` — display all current configuration values (env defaults + any runtime overrides).
- **FR-33:** `/set_threshold <monitor> <key> <value>` — update a runtime setting; persist in the `settings` table. This overrides the env default until removed.
- **FR-34:** `/toggle <monitor>` — enable or disable a monitor without redeploying.
- **FR-35:** `/list_monitors` — show all discovered plugins with their enabled/disabled status and polling interval.
- **FR-36:** `/test_alert` — send a sample alert to verify the channel is working.

### 5.6 Configuration (`src/config.py`)

- **FR-40:** All configuration shall be loaded from environment variables with sensible defaults.
- **FR-41:** Runtime overrides from the `settings` SQLite table take precedence over env values.
- **FR-42:** Required variables (no default, bot must refuse to start without them): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- **FR-43:** Optional variables with defaults:

| Variable | Default |
|----------|---------|
| `POLY_THRESHOLD` | `10000` |
| `POLY_MAX_WALLET_AGE_DAYS` | `90` |
| `POLY_MAX_ODDS` | `0.20` |
| `PENDLE_CHAINS` | `ethereum,arbitrum,bnb,optimism` |
| `DB_PATH` | `data/monitor.db` |
| `PURGE_HOURS` | `48` |

### 5.7 Database Schema (`src/db.py`)

Create these tables on first run if they do not exist:

```sql
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    monitor TEXT NOT NULL,
    market TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    monitor TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_cache (
    address TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### 5.8 Deployment (`Dockerfile`, `docker-compose.yml`, `.github/workflows/deploy.yml`)

- **FR-50:** `Dockerfile`: use `python:3.12-slim` base, install from `requirements.txt`, copy `src/`, set `CMD ["python", "-m", "src.main"]`.
- **FR-51:** `docker-compose.yml`: single service `monitor-bot`, volume `./data:/app/data`, env file `.env`, restart policy `unless-stopped`.
- **FR-52:** `.github/workflows/deploy.yml`: trigger on push to `main`. SSH into VPS using secrets (`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`), run `git pull && docker compose up -d --build`.
- **FR-53:** `.env.example`: list all env vars with placeholder values and comments. `.gitignore` must exclude `.env` and `data/`.

> [!important] Pre-existing files
> A `.env` file already exists in the project root with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` populated. Do not overwrite it. When creating `.env.example`, mirror its structure but use placeholder values. When creating `.gitignore`, ensure `.env` is excluded.

## 6. Non-Functional Requirements

- **NFR-01:** Handle API rate limits with exponential backoff. Never crash on a rate-limited response.
- **NFR-02:** Log all errors, warnings, and alert dispatches to stdout (captured by `docker logs`).
- **NFR-03:** Recover from transient API failures (timeouts, 5xx responses) automatically; retry after backoff.
- **NFR-04:** Docker container restarts automatically on crash (`unless-stopped` policy).
- **NFR-05:** No secrets committed to the repository. Use `.env` (gitignored) locally and GitHub Secrets for CI/CD.
- **NFR-06:** All async; do not block the event loop with synchronous I/O.

## 7. Acceptance Criteria

- [ ] Bot starts and responds to `/status` within 5 seconds
- [ ] `/test_alert` delivers a formatted alert to the configured Telegram channel
- [ ] Polymarket monitor detects and alerts on trades matching all filter criteria
- [ ] Pendle monitor detects and alerts on yield dislocations across all configured chains
- [ ] `/set_threshold` persists changes; they survive a container restart
- [ ] `/toggle` disables a monitor; its scheduler job stops until re-enabled
- [ ] Data older than 48h is automatically purged from `alerts` and `snapshots`
- [ ] `docker compose up` starts the bot with no manual steps beyond providing `.env`
- [ ] Push to `main` triggers GitHub Actions and deploys to VPS automatically
- [ ] Bot recovers gracefully from API downtime without manual intervention

