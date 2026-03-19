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


async def main() -> None:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60

    log.info("Fetching markets from Gamma API...")
    mm = MarketManager()
    await mm.start()
    log.info("Loaded %d markets (%d qualifying)", mm.total_markets, mm.qualifying_markets)

    # Get token IDs to subscribe (max 500 per WS connection)
    tokens = mm.get_all_token_ids()[:500]
    log.info("Subscribing to %d tokens for %d seconds...", len(tokens), duration)

    trade_count = 0
    book_count = 0
    other_count = 0

    async with websockets.connect(
        config.WS_URL, ping_interval=30, ping_timeout=10,
    ) as ws:
        # CORRECT subscription format: ONE message with array of assets_ids
        sub_msg = json.dumps({
            "assets_ids": tokens,
            "type": "market",
        })
        await ws.send(sub_msg)
        log.info("Subscription sent. Listening...")

        try:
            async with asyncio.timeout(duration):
                async for raw in ws:
                    # Handle binary messages
                    if isinstance(raw, bytes):
                        try:
                            raw = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            continue

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
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

                            # Calculate USD value
                            try:
                                usd = float(price) * float(size)
                                usd_str = f"${usd:,.2f}"
                            except (ValueError, TypeError):
                                usd_str = "?"

                            log.info(
                                "TRADE #%d: %s %s @ %s (%s shares, %s) — %s",
                                trade_count, side, "Yes/No", price, size, usd_str, title,
                            )

                        elif event_type == "book":
                            book_count += 1

                        elif event_type == "price_change":
                            pass  # ignore silently

                        else:
                            other_count += 1
                            if other_count <= 5:
                                log.info("Other event: %s — %s", event_type, str(event)[:200])

        except TimeoutError:
            pass

    log.info(
        "Done in %ds. Trades: %d, Book updates: %d, Other: %d",
        duration, trade_count, book_count, other_count,
    )
    await mm.stop()


if __name__ == "__main__":
    asyncio.run(main())
