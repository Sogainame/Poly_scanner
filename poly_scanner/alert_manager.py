"""Alert manager — formats alerts and sends them to Telegram."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from poly_scanner import config
from poly_scanner.models import Alert

log = logging.getLogger(__name__)


class AlertManager:
    """Sends formatted alerts to Telegram with rate limiting."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=10)
        self._base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
        self._alerts_sent_today = 0
        self._last_send: float = 0  # monotonic time of last send
        self._trades_processed = 0

    # ── public ────────────────────────────────────────────────────────

    async def send_alert(self, alert: Alert) -> None:
        """Format and send an alert to Telegram."""
        text = alert.format_telegram()
        await self._send_message(text)
        self._alerts_sent_today += 1
        log.info(
            "Alert sent: %s on %s (score %.1f)",
            alert.alert_type, alert.market.title[:40], alert.score,
        )

    async def send_startup_message(self, market_count: int) -> None:
        text = (
            f"🟢 *Poly Scanner started*\n"
            f"📊 Monitoring {market_count} markets\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        await self._send_message(text)

    async def send_shutdown_message(self) -> None:
        text = "🔴 *Poly Scanner stopped*"
        await self._send_message(text)

    async def send_heartbeat(self, markets: int, ws_stats: dict, db_stats: dict) -> None:
        text = (
            f"💓 *Poly Scanner heartbeat*\n"
            f"📊 Markets: {markets}\n"
            f"📈 Trades received: {ws_stats.get('trades_received', 0):,}\n"
            f"🚨 Alerts sent today: {self._alerts_sent_today}\n"
            f"💾 Trades buffered: {db_stats.get('trades_buffered', 0):,}\n"
            f"👛 Wallets tracked: {db_stats.get('wallets_tracked', 0):,}\n"
            f"🔄 WS reconnects: {ws_stats.get('reconnects', 0)}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        await self._send_message(text)

    def increment_trades(self) -> None:
        self._trades_processed += 1

    async def close(self) -> None:
        await self._client.aclose()

    # ── internal ──────────────────────────────────────────────────────

    async def _send_message(self, text: str) -> None:
        """Send a message to Telegram with rate limiting (max 1/sec)."""
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            log.warning("Telegram not configured, skipping alert")
            return

        # Rate limit: at least 1 second between messages
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_send
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)

        try:
            resp = await self._client.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            self._last_send = asyncio.get_event_loop().time()

            if resp.status_code != 200:
                log.error("Telegram error %d: %s", resp.status_code, resp.text[:200])
        except Exception:
            log.exception("Failed to send Telegram message")
