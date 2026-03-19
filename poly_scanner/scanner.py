"""Main scanner — orchestrates all components."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

from poly_scanner import config
from poly_scanner.alert_manager import AlertManager
from poly_scanner.anomaly_detector import AnomalyDetector
from poly_scanner.db import ScannerDB
from poly_scanner.market_manager import MarketManager
from poly_scanner.models import Trade
from poly_scanner.ws_listener import WebSocketListener

log = logging.getLogger("poly_scanner")


class Scanner:
    """Top-level orchestrator."""

    def __init__(self, dry_run: bool = False) -> None:
        self._dry_run = dry_run
        self._db = ScannerDB()
        self._mm = MarketManager()
        self._alerts = AlertManager()
        self._detector = AnomalyDetector(self._db)
        self._ws = WebSocketListener(self._mm)
        self._running = False
        self._start_time = datetime.now(timezone.utc)

    async def run(self) -> None:
        """Main entry point — initialize everything and run forever."""
        self._running = True

        # Graceful shutdown on SIGINT / SIGTERM
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown()))

        # 1. Database
        log.info("Initializing database...")
        await self._db.init()

        # 2. Market manager — first fetch (blocking, need markets before WS)
        log.info("Fetching markets from Gamma API...")
        await self._mm.start()
        log.info(
            "Markets loaded: %d total, %d qualifying",
            self._mm.total_markets, self._mm.qualifying_markets,
        )

        # 3. Startup alert
        if not self._dry_run:
            await self._alerts.send_startup_message(self._mm.qualifying_markets)
        else:
            log.info("DRY RUN mode — Telegram alerts disabled")

        # 4. Start background tasks
        tasks = [
            asyncio.create_task(self._ws.start(self._on_trade), name="ws_listener"),
            asyncio.create_task(self._cleanup_loop(), name="cleanup"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]

        log.info("Scanner running. Press Ctrl+C to stop.")

        # Wait until shutdown
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    # ── trade callback ────────────────────────────────────────────────

    async def _on_trade(self, trade: Trade) -> None:
        """Called for every qualifying trade from WebSocket."""
        # Log large trades
        if trade.cash_amount >= 100:
            log.debug(
                "Trade: $%.0f %s %s on %s",
                trade.cash_amount, trade.side, trade.outcome,
                trade.market_title[:40] or trade.market_id[:16],
            )

        # Persist
        await self._db.add_trade_to_buffer(trade)
        await self._db.update_wallet(trade)

        # Get market for detector
        market = self._mm.get_market_by_condition(trade.market_id)
        if not market:
            market = self._mm.get_market_by_token(trade.token_id)
        if not market:
            return

        # Detect anomalies
        alerts = await self._detector.process_trade(trade, market)

        # Dispatch alerts
        for alert in alerts:
            if self._dry_run:
                log.info(
                    "🔔 [DRY] %s on %s — score %.1f\n%s",
                    alert.alert_type, alert.market.title[:50],
                    alert.score, alert.details,
                )
            else:
                await self._alerts.send_alert(alert)

    # ── background tasks ──────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Periodic DB cleanup."""
        while self._running:
            await asyncio.sleep(3600)  # every hour
            try:
                await self._db.cleanup_old_data()
            except Exception:
                log.exception("Cleanup failed")

    async def _heartbeat_loop(self) -> None:
        """Periodic heartbeat to Telegram."""
        while self._running:
            await asyncio.sleep(config.HEARTBEAT_SECONDS)
            if self._dry_run:
                continue
            try:
                ws_stats = self._ws.stats
                db_stats = await self._db.get_stats()
                await self._alerts.send_heartbeat(
                    self._mm.qualifying_markets, ws_stats, db_stats,
                )
            except Exception:
                log.exception("Heartbeat failed")

    # ── shutdown ──────────────────────────────────────────────────────

    async def _shutdown(self) -> None:
        log.info("Shutting down...")
        self._running = False
        await self._ws.stop()

        if not self._dry_run:
            try:
                await self._alerts.send_shutdown_message()
            except Exception:
                pass

        await self._alerts.close()
        await self._mm.stop()
        await self._db.close()
        log.info("Shutdown complete.")

        # Cancel all tasks
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Volume Anomaly Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Don't send Telegram alerts")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--min-trade", type=float, help="Override min trade USD threshold")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.min_trade:
        config.MIN_TRADE_USD = args.min_trade

    scanner = Scanner(dry_run=args.dry_run)
    asyncio.run(scanner.run())


if __name__ == "__main__":
    main()
