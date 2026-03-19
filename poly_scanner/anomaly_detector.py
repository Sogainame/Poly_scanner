"""Anomaly detector — the brain.  Receives every qualifying trade and decides
whether it is anomalous enough to fire an alert."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from poly_scanner import config
from poly_scanner.db import ScannerDB
from poly_scanner.models import Alert, Market, Trade

log = logging.getLogger(__name__)


class AnomalyDetector:
    """Runs all detection rules on each incoming trade."""

    def __init__(self, db: ScannerDB) -> None:
        self._db = db

        # In-memory rolling state (complements SQLite for speed)
        # condition_id → list of (timestamp, cash_amount, wallet, outcome, side)
        self._recent: dict[str, list[tuple[datetime, float, str, str, str]]] = defaultdict(list)

        # Track whether we already fired a volume-spike alert this hour per market
        self._spike_fired: dict[str, datetime] = {}

    # ── public entry point ────────────────────────────────────────────

    async def process_trade(self, trade: Trade, market: Market) -> list[Alert]:
        """Run all detectors and return non-duplicate alerts."""
        # Update in-memory buffer
        self._add_to_buffer(trade)

        alerts: list[Alert] = []

        for detect_fn in (
            self._detect_whale_trade,
            self._detect_volume_spike,
            self._detect_fresh_wallet,
            self._detect_whale_cluster,
            self._detect_directional_bias,
        ):
            alert = await detect_fn(trade, market)
            if alert is None:
                continue
            # Dedup check
            if await self._db.should_alert(alert.alert_type, market.condition_id):
                alerts.append(alert)
                await self._db.record_alert(alert)
            else:
                log.debug(
                    "Dedup suppressed %s on %s", alert.alert_type, market.title[:40]
                )

        return alerts

    # ── detectors ─────────────────────────────────────────────────────

    async def _detect_whale_trade(self, trade: Trade, market: Market) -> Alert | None:
        """Single large trade on a quiet market."""
        if trade.cash_amount < config.MIN_TRADE_USD:
            return None
        if market.volume_24h > config.WHALE_MARKET_MAX_VOLUME and market.volume_24h > 0:
            return None

        # Skip market makers
        if await self._db.is_market_maker(trade.wallet, trade.market_id):
            log.debug("Skipping whale alert — wallet is market maker")
            return None

        # Score: bigger trade relative to market → higher score
        ratio = trade.cash_amount / max(market.volume_24h, 100)
        score = min(ratio * 10, 10.0)

        is_fresh = await self._db.is_fresh_wallet(trade.wallet)
        fresh_tag = "\n🆕 Fresh wallet!" if is_fresh else ""

        return Alert(
            alert_type="whale_trade",
            market=market,
            trades=[trade],
            score=score,
            details=f"Score: {score:.1f}/10{fresh_tag}",
        )

    async def _detect_volume_spike(self, trade: Trade, market: Market) -> Alert | None:
        """Hourly volume exceeds baseline by configured multiplier."""
        # Only check every few trades, not on every single one
        buf = self._recent.get(trade.market_id, [])
        if len(buf) % 5 != 0:  # check every 5th trade
            return None

        # Check if we already fired for this market recently
        last_fired = self._spike_fired.get(trade.market_id)
        if last_fired and (datetime.now(timezone.utc) - last_fired).total_seconds() < 3600:
            return None

        hourly_vol = self._get_hourly_volume(trade.market_id)

        baseline = await self._db.get_baseline(trade.market_id)
        if not baseline:
            return None

        _, vol_7d_avg = baseline
        # Estimate hourly baseline: daily avg / 24
        baseline_hourly = vol_7d_avg / 24.0 if vol_7d_avg > 0 else 0

        if baseline_hourly <= 0:
            return None

        multiplier = hourly_vol / baseline_hourly
        if multiplier < config.VOLUME_SPIKE_MULTIPLIER:
            return None

        self._spike_fired[trade.market_id] = datetime.now(timezone.utc)

        score = min(multiplier * 2, 10.0)
        trade_count = self._get_hourly_trade_count(trade.market_id)

        return Alert(
            alert_type="volume_spike",
            market=market,
            trades=[trade],
            score=score,
            details=(
                f"🔥 {multiplier:.1f}x normal volume in last hour\n"
                f"💵 ${hourly_vol:,.0f} vs avg ${baseline_hourly:,.0f}/h\n"
                f"📊 {trade_count} trades in last hour\n"
                f"Score: {score:.1f}/10"
            ),
        )

    async def _detect_fresh_wallet(self, trade: Trade, market: Market) -> Alert | None:
        """Brand new wallet making a sizeable trade."""
        if trade.cash_amount < config.FRESH_WALLET_MIN_TRADE_USD:
            return None

        if not await self._db.is_fresh_wallet(trade.wallet):
            return None

        score = min(trade.cash_amount / config.FRESH_WALLET_MIN_TRADE_USD * 2, 10.0)

        return Alert(
            alert_type="fresh_wallet",
            market=market,
            trades=[trade],
            score=score,
            details=f"Score: {score:.1f}/10",
        )

    async def _detect_whale_cluster(self, trade: Trade, market: Market) -> Alert | None:
        """Multiple large wallets entering same market within 1 hour."""
        buf = self._recent.get(trade.market_id, [])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

        # Group by wallet, sum volume
        wallet_volumes: dict[str, float] = defaultdict(float)
        for ts, cash, wallet, _, _ in buf:
            if ts >= cutoff:
                wallet_volumes[wallet] += cash

        # Count wallets above threshold
        whale_wallets = {
            w: v for w, v in wallet_volumes.items()
            if v >= config.WHALE_CLUSTER_WALLET_MIN_USD
        }

        if len(whale_wallets) < config.WHALE_CLUSTER_MIN_WALLETS:
            return None

        total_vol = sum(whale_wallets.values())
        score = min(len(whale_wallets) * 3, 10.0)

        return Alert(
            alert_type="whale_cluster",
            market=market,
            trades=[trade],
            score=score,
            details=(
                f"👥 {len(whale_wallets)} large wallets in last hour\n"
                f"💵 Total: ${total_vol:,.0f}\n"
                f"Score: {score:.1f}/10"
            ),
        )

    async def _detect_directional_bias(self, trade: Trade, market: Market) -> Alert | None:
        """Overwhelming one-sided buying in last hour."""
        buf = self._recent.get(trade.market_id, [])

        # Only check periodically
        if len(buf) % 10 != 0:
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

        yes_buy_vol = 0.0
        no_buy_vol = 0.0
        total_buy_vol = 0.0

        for ts, cash, _, outcome, side in buf:
            if ts < cutoff or side != "BUY":
                continue
            total_buy_vol += cash
            if outcome == "Yes":
                yes_buy_vol += cash
            elif outcome == "No":
                no_buy_vol += cash

        if total_buy_vol < config.DIRECTIONAL_BIAS_MIN_VOLUME:
            return None

        yes_pct = yes_buy_vol / total_buy_vol if total_buy_vol > 0 else 0
        no_pct = no_buy_vol / total_buy_vol if total_buy_vol > 0 else 0

        if yes_pct >= config.DIRECTIONAL_BIAS_THRESHOLD:
            direction = "YES"
            bias_pct = yes_pct * 100
        elif no_pct >= config.DIRECTIONAL_BIAS_THRESHOLD:
            direction = "NO"
            bias_pct = no_pct * 100
        else:
            return None

        score = min((bias_pct - 50) / 5, 10.0)

        return Alert(
            alert_type="directional_bias",
            market=market,
            trades=[trade],
            score=score,
            details=(
                f"📊 {bias_pct:.0f}% of buy volume is {direction} in last hour\n"
                f"💵 ${total_buy_vol:,.0f} total buy volume\n"
                f"Score: {score:.1f}/10"
            ),
        )

    # ── in-memory buffer helpers ──────────────────────────────────────

    def _add_to_buffer(self, trade: Trade) -> None:
        """Add trade to rolling in-memory buffer, trim old entries."""
        entry = (trade.timestamp, trade.cash_amount, trade.wallet, trade.outcome, trade.side)
        self._recent[trade.market_id].append(entry)

        # Trim entries older than 2 hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        self._recent[trade.market_id] = [
            e for e in self._recent[trade.market_id] if e[0] >= cutoff
        ]

    def _get_hourly_volume(self, condition_id: str) -> float:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        return sum(
            cash for ts, cash, _, _, _ in self._recent.get(condition_id, [])
            if ts >= cutoff
        )

    def _get_hourly_trade_count(self, condition_id: str) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        return sum(
            1 for ts, _, _, _, _ in self._recent.get(condition_id, [])
            if ts >= cutoff
        )
