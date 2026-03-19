# Poly Scanner — Polymarket Volume Anomaly Scanner

Real-time scanner that monitors Polymarket prediction markets for unusual trading activity and sends instant Telegram alerts.

## What it detects

| Alert | Description |
|-------|-------------|
| 🐋 Whale Trade | Large trade ($1K+) on a quiet market (<$50K daily volume) |
| 📈 Volume Spike | Hourly volume exceeds 3x the rolling average |
| 🆕 Fresh Wallet | Brand new wallet making a $500+ trade |
| 🦈 Whale Cluster | 2+ large wallets entering the same market within 1 hour |
| 🎯 Directional Bias | >80% of buy volume is one-sided (YES or NO) in the last hour |

## Filters

Automatically excludes noise:
- Crypto Up/Down 15-minute & 5-minute markets
- All sports markets
- Known market-maker wallets (buy both YES and NO)
- Deduplication: same alert type + market won't re-fire within 30 minutes

## Setup

```bash
git clone https://github.com/Sogainame/Poly_scanner.git
cd Poly_scanner
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Telegram bot token and chat ID
```

## Usage

```bash
# Test connectivity (listens for 60 seconds, prints trades)
python -m poly_scanner.test_live

# Dry run (logs anomalies, no Telegram)
python -m poly_scanner.scanner --dry-run --verbose

# Live (sends alerts to Telegram)
python -m poly_scanner.scanner

# With custom minimum trade threshold
python -m poly_scanner.scanner --min-trade 2000
```

## Architecture

```
WebSocket (real-time)  ──→  Anomaly Detector  ──→  Telegram
       │                         │
  Market Manager            SQLite DB
  (REST, every 60s)         - trade buffer
                            - wallet profiles
                            - alert dedup
                            - volume baselines
```

## Configuration

All settings are in `.env` (see `.env.example`). Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `SCANNER_MIN_TRADE_USD` | 1000 | Whale trade alert threshold |
| `SCANNER_VOLUME_SPIKE_MULTIPLIER` | 3.0 | Volume spike sensitivity |
| `SCANNER_FRESH_WALLET_HOURS` | 24 | How old is "fresh" |
| `SCANNER_DEDUP_MINUTES` | 30 | Alert dedup window |

## Project structure

```
poly_scanner/
├── scanner.py          # Main orchestrator
├── ws_listener.py      # WebSocket real-time trade stream
├── market_manager.py   # REST API market cache
├── anomaly_detector.py # Detection logic (5 rules)
├── alert_manager.py    # Telegram alerts
├── db.py               # SQLite persistence
├── models.py           # Trade, Market, Alert dataclasses
├── config.py           # Settings from .env
├── test_live.py        # Connectivity test
└── data/               # SQLite DB (auto-created)
```
