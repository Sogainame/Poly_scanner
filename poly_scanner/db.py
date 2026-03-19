"""SQLite persistence — baselines, wallet profiles, dedup, trade buffer."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from poly_scanner import config
from poly_scanner.models import Alert, Trade

log = logging.getLogger(__name__)


class ScannerDB:
    """Async SQLite wrapper for scanner state."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or config.DB_PATH
        self._conn: aiosqlite.Connection | None = None

    # ── lifecycle ─────────────────────────────────────────────────────

    async def init(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        log.info("ScannerDB initialized at %s", self._path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn, "call init() first"
        return self._conn

    # ── wallet profiles ───────────────────────────────────────────────

    async def update_wallet(self, trade: Trade) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            """INSERT INTO wallet_profiles (address, first_seen, trade_count, total_volume_usd, last_seen)
               VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(address) DO UPDATE SET
                 trade_count = trade_count + 1,
                 total_volume_usd = total_volume_usd + ?,
                 last_seen = ?""",
            (trade.wallet, now, trade.cash_amount, now, trade.cash_amount, now),
        )
        # record side for market-maker detection
        side_key = f"{trade.side}_{trade.outcome.upper()}"
        await self.conn.execute(
            """INSERT OR REPLACE INTO wallet_market_sides
               (address, condition_id, side, timestamp)
               VALUES (?, ?, ?, ?)""",
            (trade.wallet, trade.market_id, side_key, now),
        )
        await self.conn.commit()

    async def is_market_maker(self, wallet: str, condition_id: str) -> bool:
        """True if wallet traded both YES and NO on this market within MM window."""
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=config.MM_DETECTION_WINDOW_SECONDS)
        ).isoformat()
        cursor = await self.conn.execute(
            """SELECT DISTINCT side FROM wallet_market_sides
               WHERE address = ? AND condition_id = ? AND timestamp >= ?""",
            (wallet, condition_id, cutoff),
        )
        sides = {row["side"] for row in await cursor.fetchall()}
        has_yes = any("YES" in s for s in sides)
        has_no = any("NO" in s for s in sides)
        return has_yes and has_no

    async def is_fresh_wallet(self, wallet: str) -> bool:
        """True if wallet was first seen < FRESH_WALLET_HOURS ago or not in DB."""
        cursor = await self.conn.execute(
            "SELECT first_seen FROM wallet_profiles WHERE address = ?",
            (wallet,),
        )
        row = await cursor.fetchone()
        if not row:
            return True
        first = datetime.fromisoformat(row["first_seen"])
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - first
        return age < timedelta(hours=config.FRESH_WALLET_HOURS)

    # ── alert dedup ───────────────────────────────────────────────────

    async def should_alert(self, alert_type: str, condition_id: str) -> bool:
        """True if we have NOT alerted this type+market within DEDUP_MINUTES."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=config.DEDUP_MINUTES)
        ).isoformat()
        cursor = await self.conn.execute(
            """SELECT COUNT(*) as cnt FROM alert_history
               WHERE alert_type = ? AND condition_id = ? AND timestamp >= ?""",
            (alert_type, condition_id, cutoff),
        )
        row = await cursor.fetchone()
        return row["cnt"] == 0

    async def record_alert(self, alert: Alert) -> None:
        await self.conn.execute(
            """INSERT INTO alert_history (alert_type, condition_id, timestamp, details)
               VALUES (?, ?, ?, ?)""",
            (
                alert.alert_type,
                alert.market.condition_id,
                alert.timestamp.isoformat(),
                alert.details[:500],
            ),
        )
        await self.conn.commit()

    # ── baselines ─────────────────────────────────────────────────────

    async def update_baseline(
        self, condition_id: str, title: str, slug: str,
        volume_24h: float, volume_7d_avg: float,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute(
            """INSERT INTO market_baselines (condition_id, title, slug, volume_24h, volume_7d_avg, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(condition_id) DO UPDATE SET
                 title = ?, slug = ?, volume_24h = ?, volume_7d_avg = ?, updated_at = ?""",
            (condition_id, title, slug, volume_24h, volume_7d_avg, now,
             title, slug, volume_24h, volume_7d_avg, now),
        )
        await self.conn.commit()

    async def get_baseline(self, condition_id: str) -> tuple[float, float] | None:
        cursor = await self.conn.execute(
            "SELECT volume_24h, volume_7d_avg FROM market_baselines WHERE condition_id = ?",
            (condition_id,),
        )
        row = await cursor.fetchone()
        if row:
            return (row["volume_24h"], row["volume_7d_avg"])
        return None

    # ── trade buffer ──────────────────────────────────────────────────

    async def add_trade_to_buffer(self, trade: Trade) -> None:
        await self.conn.execute(
            """INSERT INTO trades_buffer (condition_id, wallet, side, outcome, cash_amount, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                trade.market_id,
                trade.wallet,
                trade.side,
                trade.outcome,
                trade.cash_amount,
                trade.timestamp.isoformat(),
            ),
        )
        await self.conn.commit()

    async def get_recent_trades(
        self, condition_id: str, minutes: int = 60
    ) -> list[dict]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes)
        ).isoformat()
        cursor = await self.conn.execute(
            """SELECT * FROM trades_buffer
               WHERE condition_id = ? AND timestamp >= ?
               ORDER BY timestamp DESC""",
            (condition_id, cutoff),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_hourly_volume(self, condition_id: str) -> float:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(cash_amount), 0) as vol FROM trades_buffer WHERE condition_id = ? AND timestamp >= ?",
            (condition_id, cutoff),
        )
        row = await cursor.fetchone()
        return float(row["vol"])

    async def get_hourly_trade_count(self, condition_id: str) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        cursor = await self.conn.execute(
            "SELECT COUNT(*) as cnt FROM trades_buffer WHERE condition_id = ? AND timestamp >= ?",
            (condition_id, cutoff),
        )
        row = await cursor.fetchone()
        return int(row["cnt"])

    # ── cleanup ───────────────────────────────────────────────────────

    async def cleanup_old_data(self) -> None:
        trade_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).isoformat()
        alert_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).isoformat()
        sides_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).isoformat()

        await self.conn.execute(
            "DELETE FROM trades_buffer WHERE timestamp < ?", (trade_cutoff,)
        )
        await self.conn.execute(
            "DELETE FROM alert_history WHERE timestamp < ?", (alert_cutoff,)
        )
        await self.conn.execute(
            "DELETE FROM wallet_market_sides WHERE timestamp < ?", (sides_cutoff,)
        )
        await self.conn.commit()
        log.info("DB cleanup complete")

    # ── stats ─────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        trades = await self.conn.execute("SELECT COUNT(*) as c FROM trades_buffer")
        alerts = await self.conn.execute("SELECT COUNT(*) as c FROM alert_history")
        wallets = await self.conn.execute("SELECT COUNT(*) as c FROM wallet_profiles")
        t = await trades.fetchone()
        a = await alerts.fetchone()
        w = await wallets.fetchone()
        return {
            "trades_buffered": t["c"],
            "alerts_total": a["c"],
            "wallets_tracked": w["c"],
        }


# ── Schema ────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_baselines (
    condition_id TEXT PRIMARY KEY,
    title TEXT,
    slug TEXT,
    volume_24h REAL DEFAULT 0,
    volume_7d_avg REAL DEFAULT 0,
    last_trade_count_1h INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS wallet_profiles (
    address TEXT PRIMARY KEY,
    first_seen TEXT,
    trade_count INTEGER DEFAULT 0,
    total_volume_usd REAL DEFAULT 0,
    is_market_maker INTEGER DEFAULT 0,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS wallet_market_sides (
    address TEXT,
    condition_id TEXT,
    side TEXT,
    timestamp TEXT,
    PRIMARY KEY (address, condition_id, side)
);

CREATE TABLE IF NOT EXISTS alert_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT,
    condition_id TEXT,
    timestamp TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS trades_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT,
    wallet TEXT,
    side TEXT,
    outcome TEXT,
    cash_amount REAL,
    timestamp TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_buffer_market_ts
    ON trades_buffer (condition_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_buffer_ts
    ON trades_buffer (timestamp);
CREATE INDEX IF NOT EXISTS idx_alert_history_dedup
    ON alert_history (alert_type, condition_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_wallet_sides_lookup
    ON wallet_market_sides (address, condition_id, timestamp);
"""
