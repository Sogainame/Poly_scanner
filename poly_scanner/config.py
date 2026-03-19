"""Configuration — all scanner settings, loaded from .env with sensible defaults."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


# ── Telegram ──────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── WebSocket ─────────────────────────────────────────────────────────

WS_URL: str = os.getenv(
    "SCANNER_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)

# ── Detection thresholds ──────────────────────────────────────────────

# Minimum single trade size (USD) to trigger whale alert
MIN_TRADE_USD: float = float(os.getenv("SCANNER_MIN_TRADE_USD", "1000"))

# Market daily volume ceiling — whale alerts only fire on "quiet" markets below this
WHALE_MARKET_MAX_VOLUME: float = float(os.getenv("SCANNER_WHALE_MARKET_MAX_VOLUME", "50000"))

# Hourly volume must exceed baseline * this multiplier to trigger volume spike
VOLUME_SPIKE_MULTIPLIER: float = float(os.getenv("SCANNER_VOLUME_SPIKE_MULTIPLIER", "3.0"))

# Wallet younger than this (hours) counts as "fresh"
FRESH_WALLET_HOURS: int = int(os.getenv("SCANNER_FRESH_WALLET_HOURS", "24"))

# Minimum trade size for a fresh-wallet alert
FRESH_WALLET_MIN_TRADE_USD: float = float(os.getenv("SCANNER_FRESH_WALLET_MIN_TRADE_USD", "500"))

# Number of unique whale wallets in 1h to trigger cluster alert
WHALE_CLUSTER_MIN_WALLETS: int = int(os.getenv("SCANNER_WHALE_CLUSTER_MIN_WALLETS", "2"))

# Minimum per-wallet volume to count as "whale" in cluster detection
WHALE_CLUSTER_WALLET_MIN_USD: float = float(os.getenv("SCANNER_WHALE_CLUSTER_WALLET_MIN_USD", "5000"))

# Directional bias threshold (0-1): fraction of buy volume on one side
DIRECTIONAL_BIAS_THRESHOLD: float = float(os.getenv("SCANNER_DIRECTIONAL_BIAS_THRESHOLD", "0.80"))

# Minimum 1h volume for directional bias alert to fire (skip tiny markets)
DIRECTIONAL_BIAS_MIN_VOLUME: float = float(os.getenv("SCANNER_DIRECTIONAL_BIAS_MIN_VOLUME", "5000"))

# ── Dedup & timing ───────────────────────────────────────────────────

# Don't re-alert same type + same market within this window (minutes)
DEDUP_MINUTES: int = int(os.getenv("SCANNER_DEDUP_MINUTES", "30"))

# How often to refresh market list from Gamma API (seconds)
MARKET_REFRESH_SECONDS: int = int(os.getenv("SCANNER_MARKET_REFRESH_SECONDS", "60"))

# How often to recalculate baseline volumes (seconds)
BASELINE_REFRESH_SECONDS: int = int(os.getenv("SCANNER_BASELINE_REFRESH_SECONDS", "900"))

# Heartbeat message to Telegram every N seconds (6 hours)
HEARTBEAT_SECONDS: int = int(os.getenv("SCANNER_HEARTBEAT_SECONDS", "21600"))

# ── Filters ───────────────────────────────────────────────────────────

# Markets with less total volume than this are skipped entirely
MIN_MARKET_VOLUME: float = float(os.getenv("SCANNER_MIN_MARKET_VOLUME", "1000"))

# Time window for market-maker detection: if wallet buys YES and NO
# on same market within this many seconds → flagged as MM
MM_DETECTION_WINDOW_SECONDS: int = int(os.getenv("SCANNER_MM_DETECTION_WINDOW_SECONDS", "300"))

# ── Storage ───────────────────────────────────────────────────────────

DB_PATH: str = os.getenv("SCANNER_DB_PATH", str(_PROJECT_ROOT / "data" / "scanner.db"))

# ── Excluded patterns (lowercase) ─────────────────────────────────────

EXCLUDED_TITLE_PATTERNS: list[str] = [
    "up or down",
    "up/down",
    "15-minute",
    "5-minute",
    "15min",
    "5min",
    "15 minute",
    "5 minute",
]

EXCLUDED_SPORT_PATTERNS: list[str] = [
    "nfl", "nba", "mlb", "nhl", "wnba", "ncaa",
    "ufc", "mma",
    "premier league", "epl",
    "champions league", "ucl",
    "la liga", "serie a", "bundesliga", "ligue 1",
    "mls",
    "super bowl",
    "world cup",
    "world series",
    "stanley cup",
    "grand prix", "formula 1", "f1",
    "nascar",
    "tennis", "wimbledon", "us open", "french open", "australian open",
    "pga", "masters tournament",
    " vs ",  # common in match-ups like "Lakers vs Celtics"
]
