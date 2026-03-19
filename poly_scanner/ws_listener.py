"""WebSocket listener — real-time trade stream from Polymarket CLOB."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import websockets
import websockets.exceptions

from poly_scanner import config
from poly_scanner.market_manager import MarketManager
from poly_scanner.models import Trade

log = logging.getLogger(__name__)


class WebSocketListener:
    """Connects to Polymarket WS and streams enriched trades."""

    def __init__(self, market_manager: MarketManager) -> None:
        self._mm = market_manager
        self._running = False
        self._reconnect_count = 0
        self._last_message: datetime | None = None
        self._trades_received = 0

    # ── public interface ──────────────────────────────────────────────

    async def start(self, on_trade: Callable[[Trade], Awaitable[None]]) -> None:
        """Connect and start listening. Reconnects automatically."""
        self._running = True
        self._on_trade = on_trade

        while self._running:
            try:
                await self._connect_and_listen()
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.ConnectionClosedError,
                ConnectionError,
                OSError,
            ) as e:
                if not self._running:
                    break
                self._reconnect_count += 1
                delay = min(2 ** min(self._reconnect_count, 5), 30)
                log.warning(
                    "WS disconnected (%s), reconnect #%d in %ds",
                    e, self._reconnect_count, delay,
                )
                await asyncio.sleep(delay)
            except Exception:
                if not self._running:
                    break
                log.exception("Unexpected WS error, reconnecting in 10s")
                await asyncio.sleep(10)

    async def stop(self) -> None:
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "trades_received": self._trades_received,
            "reconnects": self._reconnect_count,
            "last_message": self._last_message.isoformat() if self._last_message else None,
        }

    # ── internal ──────────────────────────────────────────────────────

    async def _connect_and_listen(self) -> None:
        """Single connection lifecycle."""
        log.info("Connecting to WS: %s", config.WS_URL)

        async with websockets.connect(
            config.WS_URL,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            log.info("WS connected, subscribing to markets...")
            self._reconnect_count = 0  # reset on successful connection

            # Subscribe to all qualifying market asset IDs
            await self._subscribe(ws)

            log.info("WS listening for trades...")
            async for raw_msg in ws:
                if not self._running:
                    break
                self._last_message = datetime.now(timezone.utc)

                try:
                    await self._handle_message(raw_msg)
                except Exception:
                    log.exception("Error handling WS message: %s", str(raw_msg)[:200])

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Subscribe to trade channels for qualifying markets.

        Polymarket WS supports subscribing to specific asset IDs.
        Format: {"type": "subscribe", "channel": "market", "assets_id": "TOKEN_ID"}
        
        We subscribe in batches to avoid overwhelming the WS.
        """
        token_ids = self._mm.get_all_token_ids()
        log.info("Subscribing to %d token IDs...", len(token_ids))

        # Subscribe in batches of 50
        batch_size = 50
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i : i + batch_size]
            for tid in batch:
                msg = json.dumps({
                    "type": "subscribe",
                    "channel": "market",
                    "assets_id": tid,
                })
                await ws.send(msg)
            await asyncio.sleep(0.1)  # small delay between batches

        log.info("Subscribed to %d tokens across %d qualifying markets",
                 len(token_ids), self._mm.qualifying_markets)

    async def _handle_message(self, raw: str) -> None:
        """Parse a WS message and emit enriched trades."""
        data = json.loads(raw)

        # Polymarket WS can send different message types
        msg_type = data.get("type", "")

        # Trade/match messages
        if msg_type in ("last_trade_price", "price_change"):
            # These are price updates, not individual trades — skip
            return

        # The actual trade stream format varies. Common patterns:
        # 1. {"asset_id": "...", "price": "0.55", "size": "100", "side": "BUY", ...}
        # 2. {"type": "trade", "data": {...}}
        # 3. Array of trades

        trades_data = []

        if isinstance(data, list):
            trades_data = data
        elif "data" in data and isinstance(data["data"], list):
            trades_data = data["data"]
        elif "data" in data and isinstance(data["data"], dict):
            trades_data = [data["data"]]
        elif "asset_id" in data:
            trades_data = [data]
        elif "market" in data and isinstance(data["market"], str):
            # Some messages are just subscription confirmations
            return
        else:
            # Unknown format — log at debug level
            log.debug("Unknown WS message format: %s", str(data)[:300])
            return

        for td in trades_data:
            trade = self._parse_trade(td)
            if trade and not self._mm.is_excluded(trade.token_id):
                self._trades_received += 1
                await self._on_trade(trade)

    def _parse_trade(self, data: dict) -> Trade | None:
        """Parse a single trade dict into a Trade model."""
        try:
            token_id = str(data.get("asset_id") or data.get("token_id") or "")
            if not token_id:
                return None

            # Enrich from market manager
            market = self._mm.get_market_by_token(token_id)

            # Determine outcome from token ID
            outcome = "Unknown"
            market_id = ""
            market_title = ""
            market_slug = ""
            event_slug = ""

            if market:
                market_id = market.condition_id
                market_title = market.title
                market_slug = market.slug
                event_slug = market.event_slug
                if token_id == market.token_id_yes:
                    outcome = "Yes"
                elif token_id == market.token_id_no:
                    outcome = "No"

            # Parse price and size — can be string or number
            price = float(data.get("price", 0) or 0)
            size = float(data.get("size", 0) or 0)

            if price <= 0 or size <= 0:
                return None

            # Side
            side = str(data.get("side", "BUY")).upper()
            if side not in ("BUY", "SELL"):
                side = "BUY"

            # Wallet — could be taker_address, maker_address, or just address
            wallet = str(
                data.get("taker_address")
                or data.get("maker_address")
                or data.get("owner")
                or data.get("trader")
                or data.get("address")
                or "unknown"
            )

            # Timestamp
            ts_raw = data.get("timestamp") or data.get("match_time")
            if ts_raw:
                try:
                    ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
                except (ValueError, TypeError, OSError):
                    ts = datetime.now(timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            return Trade(
                market_id=market_id,
                token_id=token_id,
                wallet=wallet,
                side=side,
                outcome=outcome,
                price=price,
                size=size,
                timestamp=ts,
                market_title=market_title,
                market_slug=market_slug,
                event_slug=event_slug,
            )

        except Exception:
            log.debug("Failed to parse trade: %s", str(data)[:200], exc_info=True)
            return None
