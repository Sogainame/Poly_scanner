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

    # —— public interface ——————————————————————————————————

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

    # —— internal ——————————————————————————————————————————

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

        Polymarket CLOB WS subscription format:
        ONE message with array of assets_ids:
        {"assets_ids": ["token1", "token2", ...], "type": "market"}

        Max 500 assets per connection (undocumented Polymarket limit).
        We split into multiple subscription messages if needed.
        """
        all_token_ids = self._mm.get_all_token_ids()
        log.info("Subscribing to %d token IDs...", len(all_token_ids))

        # Split into chunks of 500 (Polymarket limit per connection)
        chunk_size = 500
        for i in range(0, len(all_token_ids), chunk_size):
            chunk = all_token_ids[i : i + chunk_size]
            msg = json.dumps({
                "assets_ids": chunk,
                "type": "market",
            })
            await ws.send(msg)
            if i + chunk_size < len(all_token_ids):
                await asyncio.sleep(0.2)  # small delay between chunks

        log.info("Subscribed to %d tokens across %d qualifying markets",
                 len(all_token_ids), self._mm.qualifying_markets)

    async def _handle_message(self, raw: str | bytes) -> None:
        """Parse a WS message and emit enriched trades."""
        # Handle binary messages
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # WS can send a single event dict or an array of events
        events = data if isinstance(data, list) else [data]

        for event in events:
            if not isinstance(event, dict):
                continue

            event_type = event.get("event_type", "")

            # last_trade_price is the trade event we care about
            # Format: {"event_type": "last_trade_price", "asset_id": "...",
            #          "market": "0x...", "price": "0.52", "size": "100",
            #          "side": "BUY", "timestamp": 1234567890000}
            if event_type == "last_trade_price":
                trade = self._parse_trade(event)
                if trade and not self._mm.is_excluded(trade.token_id):
                    self._trades_received += 1
                    await self._on_trade(trade)

            # book and price_change events are skipped — we only want trades
            # Other event types logged at debug level
            elif event_type not in ("book", "price_change", "tick_size_change"):
                log.debug("Unhandled WS event type: %s", event_type)

    def _parse_trade(self, data: dict) -> Trade | None:
        """Parse a last_trade_price event into a Trade model."""
        try:
            token_id = str(data.get("asset_id") or "")
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

            # Wallet — last_trade_price does NOT include wallet address
            # We'll use "unknown" for now; wallet tracking requires
            # either the user channel (authenticated) or the Data API
            wallet = "unknown"

            # Timestamp — can be milliseconds
            ts_raw = data.get("timestamp")
            if ts_raw:
                try:
                    ts_int = int(ts_raw)
                    # If timestamp > 1e12, it's in milliseconds
                    if ts_int > 1e12:
                        ts_int = ts_int // 1000
                    ts = datetime.fromtimestamp(ts_int, tz=timezone.utc)
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
