"""Market manager — fetches & caches active markets from Polymarket REST APIs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from poly_scanner import config
from poly_scanner.models import Market

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


class MarketManager:
    """Maintains an in-memory cache of active Polymarket markets."""

    def __init__(self) -> None:
        self._by_condition: dict[str, Market] = {}
        self._by_token: dict[str, Market] = {}  # token_id → Market
        self._client = httpx.AsyncClient(timeout=20)
        self._running = False

    # ── public lookups (O(1)) ─────────────────────────────────────────

    def get_market_by_token(self, token_id: str) -> Market | None:
        return self._by_token.get(token_id)

    def get_market_by_condition(self, condition_id: str) -> Market | None:
        return self._by_condition.get(condition_id)

    def is_excluded(self, token_id: str) -> bool:
        m = self._by_token.get(token_id)
        return m.is_excluded if m else True  # unknown tokens are excluded by default

    def get_all_qualifying(self) -> list[Market]:
        return [m for m in self._by_condition.values() if not m.is_excluded]

    def get_all_token_ids(self) -> list[str]:
        """All token IDs for qualifying (non-excluded) markets."""
        ids: list[str] = []
        for m in self._by_condition.values():
            if m.is_excluded:
                continue
            if m.token_id_yes:
                ids.append(m.token_id_yes)
            if m.token_id_no:
                ids.append(m.token_id_no)
        return ids

    @property
    def total_markets(self) -> int:
        return len(self._by_condition)

    @property
    def qualifying_markets(self) -> int:
        return len(self.get_all_qualifying())

    # ── background refresh ────────────────────────────────────────────

    async def start(self) -> None:
        """Run the first fetch then start background loop."""
        await self._refresh()
        self._running = True
        asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        await self._client.aclose()

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(config.MARKET_REFRESH_SECONDS)
            try:
                await self._refresh()
            except Exception:
                log.exception("Market refresh failed, keeping stale cache")

    # ── fetch from Gamma API ──────────────────────────────────────────

    async def _refresh(self) -> None:
        markets: list[dict] = []
        offset = 0
        limit = 100

        # Only fetch markets with real activity (volume > 0) to avoid
        # pulling 30K+ dead markets.  Gamma supports order=volume&ascending=false.
        while True:
            try:
                resp = await self._client.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "closed": "false",
                        "active": "true",
                        "limit": limit,
                        "offset": offset,
                        "order": "volume",
                        "ascending": "false",
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    log.warning("Gamma API rate limited, retrying in 5s")
                    await asyncio.sleep(5)
                    continue
                raise

            batch = resp.json()
            if not batch:
                break

            # Stop if we've hit markets with negligible volume
            # (they're sorted by volume desc)
            last_vol = float(batch[-1].get("volume", 0) or 0)
            markets.extend(batch)

            if len(batch) < limit:
                break
            # Stop after we pass the min volume threshold — no point
            # loading thousands of zero-volume markets
            if last_vol < config.MIN_MARKET_VOLUME and len(markets) >= 500:
                break
            offset += limit
            await asyncio.sleep(0.15)

        # Build lookup dicts
        new_by_condition: dict[str, Market] = {}
        new_by_token: dict[str, Market] = {}

        for raw in markets:
            m = self._parse_market(raw)
            m.classify()

            new_by_condition[m.condition_id] = m
            if m.token_id_yes:
                new_by_token[m.token_id_yes] = m
            if m.token_id_no:
                new_by_token[m.token_id_no] = m

        self._by_condition = new_by_condition
        self._by_token = new_by_token

        qualifying = len([m for m in new_by_condition.values() if not m.is_excluded])
        excluded = len(new_by_condition) - qualifying
        log.info(
            "Market refresh: %d total, %d qualifying, %d excluded",
            len(new_by_condition), qualifying, excluded,
        )

    @staticmethod
    def _parse_market(raw: dict) -> Market:
        """Parse a Gamma API market response into our Market model."""
        # Extract token IDs from the outcomes/tokens structure
        tokens = raw.get("clobTokenIds") or raw.get("tokens") or []
        outcomes = raw.get("outcomes") or raw.get("outcomePrices") or []

        token_yes = ""
        token_no = ""

        if isinstance(tokens, str):
            # Sometimes it's a JSON string like '["id1","id2"]'
            import json
            try:
                tokens = json.loads(tokens)
            except (json.JSONDecodeError, TypeError):
                tokens = []

        if isinstance(tokens, list) and len(tokens) >= 2:
            token_yes = str(tokens[0])
            token_no = str(tokens[1])
        elif isinstance(tokens, list) and len(tokens) == 1:
            token_yes = str(tokens[0])

        # Parse end date
        end_date = None
        raw_end = raw.get("endDate") or raw.get("end_date_iso")
        if raw_end:
            try:
                end_date = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        volume_str = raw.get("volume", "0") or "0"
        try:
            volume = float(volume_str)
        except (ValueError, TypeError):
            volume = 0.0

        return Market(
            condition_id=raw.get("conditionId") or raw.get("condition_id") or "",
            token_id_yes=token_yes,
            token_id_no=token_no,
            title=raw.get("question") or raw.get("title") or "",
            slug=raw.get("slug") or "",
            event_slug=raw.get("eventSlug") or raw.get("event_slug") or "",
            volume_24h=volume,
            end_date=end_date,
            last_updated=datetime.now(timezone.utc),
        )
