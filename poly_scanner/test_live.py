#!/usr/bin/env python3
"""Quick test: connect to Polymarket WS for 60 seconds and print trades.
Run: python -m poly_scanner.test_live
"""

import asyncio
import json
import logging
import sys

import websockets

from poly_scanner import config
from poly_scanner.market_manager import MarketManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


async def ping_loop(ws: websockets.WebSocketClientProtocol) -> None:
    """Send PING every 10 seconds as required by Polymarket WS."""
    try:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")
    except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
        pass


async def main() -> None:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60

    log.info("Fetching markets from Gamma API...")
    mm = MarketManager()
    await mm.start()
    log.info("Loaded %d markets (%d qualifying)", mm.total_markets, mm.qualifying_markets)

    # Top markets by volume — enough to see trades quickly
    tokens = mm.get_all_token_ids()[:100]
    log.info("Subscribing to %d tokens for %d seconds...", len(tokens), duration)

    trade_count = 0
    book_count = 0
    other_count = 0

    async with websockets.connect(
        config.WS_URL, ping_interval=None, ping_timeout=None,
    ) as ws:
        # Subscribe: ONE message with array of assets_ids
        sub_msg = json.dumps({
            "assets_ids": tokens,
            "type": "market",
        })
        await ws.send(sub_msg)
        log.info("Subscription sent.")

        # Start heartbeat task
        ping_task = asyncio.create_task(ping_loop(ws))

        log.info("Listening for events...")
        try:
            async with asyncio.timeout(duration):
                async for raw in ws:
                    # Handle binary messages
                    if isinstance(raw, bytes):
                        try:
                            raw = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            continue

                    # Handle PONG response
                    if raw == "PONG":
                        continue

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        log.debug("Non-JSON message: %s", raw[:100])
                        continue

                    # Handle arrays of events
                    events = data if isinstance(data, list) else [data]

                    for event in events:
                        if not isinstance(event, dict):
                            continue

                        event_type = event.get("event_type", "")

                        if event_type == "last_trade_price":
                            trade_count += 1
                            token_id = event.get("asset_id", "")
                            market = mm.get_market_by_token(token_id)
                            title = market.title[:60] if market else "UNKNOWN"
                            price = event.get("price", "?")
                            size = event.get("size", "?")
                            side = event.get("side", "?")

                            try:
                                usd = float(price) * float(size)
                                usd_str = f"${usd:,.2f}"
                            except (ValueError, TypeError):
                                usd_str = "?"

                            log.info(
                                "TRADE #%d: %s @ %s (%s shares, %s) — %s",
                                trade_count, side, price, size, usd_str, title,
                            )

                        elif event_type == "book":
                            book_count += 1
                            if book_count <= 3:
                                log.info("Book update for %s", event.get("asset_id", "?")[:20])

                        elif event_type == "price_change":
                            pass  # very noisy, skip

                        elif event_type == "tick_size_change":
                            pass

                        else:
                            other_count += 1
                            if other_count <= 10:
                                log.info("Event [%s]: %s", event_type, str(event)[:200])

        except TimeoutError:
            pass
        finally:
            ping_task.cancel()

    log.info(
        "Done in %ds. Trades: %d, Book updates: %d, Other: %d",
        duration, trade_count, book_count, other_count,
    )
    await mm.stop()


if __name__ == "__main__":
    asyncio.run(main())
