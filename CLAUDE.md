# Poly Scanner — Polymarket Volume Anomaly Scanner

## What this project does
Real-time scanner that monitors ALL non-sport, non-crypto-updown Polymarket markets via WebSocket for volume anomalies and whale activity. Sends instant Telegram alerts when suspicious trading patterns are detected.

## Architecture
- WebSocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) for real-time trade stream — PRIMARY data source, ~1 second latency
- REST API (gamma-api.polymarket.com, data-api.polymarket.com) for background metadata refresh only
- SQLite for persisting baseline volumes, wallet profiles, and alert dedup history
- Telegram bot for alerts (via notifier module)

## Data flow
```
[WebSocket: live trades] ──→ [ws_listener.py] ──→ [anomaly_detector.py] ──→ [alert_manager.py] ──→ [Telegram]
                                    │                      │
                              [market_manager.py]    [db.py: SQLite]
                              (REST refresh 60s)     - baselines
                                                     - wallet profiles
                                                     - alert dedup
                                                     - trade buffer
```

## Key detection rules
1. **Whale trade**: single trade >= $1,000 on a market with < $50K daily volume
2. **Volume spike**: hourly volume > 3x rolling average
3. **Fresh wallet**: wallet age < 24h making trade >= $500
4. **Whale cluster**: 2+ wallets with > $5K entering same market within 1 hour
5. **Directional bias**: >80% of buy volume is one direction (YES or NO) in last hour

## Filters (reduce noise)
- EXCLUDE markets matching: "up or down", "up/down", "15-minute", "5-minute", "15min", "5min"
- EXCLUDE sports (NFL, NBA, MLB, NHL, UFC, Premier League, Champions League, etc.)
- EXCLUDE known market-maker wallets (buy BOTH yes AND no on same market within 5 min)
- Dedup: no re-alert on same market + same anomaly type within 30 minutes
- Skip markets with < $1,000 total volume

## Tech stack
- Python 3.11+
- asyncio + aiohttp/websockets for async I/O
- httpx for REST API calls
- aiosqlite for async SQLite
- python-dotenv for config

## Config (.env)
```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
SCANNER_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
SCANNER_MIN_TRADE_USD=1000
SCANNER_VOLUME_SPIKE_MULTIPLIER=3.0
SCANNER_FRESH_WALLET_HOURS=24
SCANNER_DEDUP_MINUTES=30
SCANNER_MARKET_REFRESH_SECONDS=60
SCANNER_BASELINE_REFRESH_SECONDS=900
SCANNER_DB_PATH=data/scanner.db
```

## Running
```bash
# Install deps
pip install -r requirements.txt

# Dry run (no Telegram, just logs)
python -m poly_scanner.scanner --dry-run --verbose

# Live
python -m poly_scanner.scanner
```

## Code style
- Type hints everywhere
- Dataclasses for all data structures
- Async/await (asyncio) for everything
- Logging via standard logging module, not print()
- All thresholds in config.py, never hardcoded in logic
