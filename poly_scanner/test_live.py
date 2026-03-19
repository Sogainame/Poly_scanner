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

    # Get a sample of token IDs to subscribe
    tokens = mm.get_all_token_ids()[:100]  # subscribe to first 100 for testing
    log.info("Subscribing to %d tokens for %d seconds...", len(tokens), duration)

    trade_count = 0

    async with websockets.connect(
        config.WS_URL, ping_interval=30, ping_timeout=10,
    ) as ws:
        for tid in tokens:
            await ws.send(json.dumps({
                "type": "subscribe",
                "channel": "market",
                "assets_id": tid,
            }))
        await asyncio.sleep(0.5)
        log.info("Subscribed. Listening...")

        try:
            async with asyncio.timeout(duration):
                async for raw in ws:
                    # Skip binary or empty messages
                    if isinstance(raw, bytes):
                        log.debug("Binary message (%d bytes), skipping", len(raw))
                        continue
                    if not raw or not raw.strip():
                        continue

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        log.debug("Non-JSON message: %s", raw[:100])
                        continue

                    # Skip non-trade messages
                    if isinstance(data, dict) and data.get("type") in (
                        "subscribed", "connected", "last_trade_price", "price_change"
                    ):
                        continue

                    trade_count += 1

                    # Try to extract trade info
                    if isinstance(data, dict) and "asset_id" in data:
                        token_id = data.get("asset_id", "")
                        market = mm.get_market_by_token(token_id)
                        title = market.title[:50] if market else "UNKNOWN"
                        price = data.get("price", "?")
                        size = data.get("size", "?")
                        side = data.get("side", "?")
                        log.info(
                            "Trade #%d: %s %s @ %s (%s shares) — %s",
                            trade_count, side, "Yes/No", price, size, title,
                        )
                    else:
                        log.info("Message #%d: %s", trade_count, str(data)[:200])

        except TimeoutError:
            pass

    log.info("Done. Received %d messages in %d seconds.", trade_count, duration)
    await mm.stop()


if __name__ == "__main__":
    asyncio.run(main())
