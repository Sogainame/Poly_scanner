"""Data models for the scanner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from poly_scanner import config


# ── Trade ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """A single trade received from the WebSocket stream."""

    market_id: str  # condition_id
    token_id: str  # asset_id
    wallet: str
    side: str  # "BUY" or "SELL"
    outcome: str  # "Yes" or "No"
    price: float
    size: float  # number of shares
    timestamp: datetime
    market_title: str = ""
    market_slug: str = ""
    event_slug: str = ""

    @property
    def cash_amount(self) -> float:
        """USD value of the trade."""
        return self.size * self.price


# ── Market ────────────────────────────────────────────────────────────

@dataclass
class Market:
    """A Polymarket market with metadata and classification."""

    condition_id: str
    token_id_yes: str = ""
    token_id_no: str = ""
    title: str = ""
    slug: str = ""
    event_slug: str = ""
    category: str = "other"  # politics, geopolitics, crypto_dip, crypto_price, other
    is_excluded: bool = False
    volume_24h: float = 0.0
    volume_7d_avg: float = 0.0
    end_date: Optional[datetime] = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def classify(self) -> None:
        """Set category and is_excluded based on title/slug."""
        t = (self.title + " " + self.slug).lower()

        # Check exclusions first
        for pat in config.EXCLUDED_TITLE_PATTERNS:
            if pat in t:
                self.is_excluded = True
                self.category = "crypto_updown"
                return

        for pat in config.EXCLUDED_SPORT_PATTERNS:
            if pat in t:
                self.is_excluded = True
                self.category = "sports"
                return

        # Classify non-excluded markets
        if any(kw in t for kw in ("dip to", "dip below", "drop to", "drop below",
                                   "fall to", "fall below", "crash below")):
            self.category = "crypto_dip"
        elif any(kw in t for kw in ("bitcoin", "btc", "ethereum", "eth", "solana",
                                     "sol", "xrp", "crypto")) and \
             any(kw in t for kw in ("price", "above", "below", "hit", "reach", "$")):
            self.category = "crypto_price"
        elif any(kw in t for kw in ("president", "election", "trump", "biden",
                                     "congress", "senate", "governor", "gop",
                                     "democrat", "republican", "primary")):
            self.category = "politics"
        elif any(kw in t for kw in ("iran", "war", "strike", "invasion", "military",
                                     "tariff", "sanctions", "ceasefire", "ukraine",
                                     "russia", "china", "nato", "missile")):
            self.category = "geopolitics"
        else:
            self.category = "other"


# ── Alert ─────────────────────────────────────────────────────────────

ALERT_EMOJIS = {
    "whale_trade": "🐋",
    "volume_spike": "📈",
    "fresh_wallet": "🆕",
    "whale_cluster": "🦈",
    "directional_bias": "🎯",
}


@dataclass
class Alert:
    """A generated alert ready for dispatch."""

    alert_type: str  # whale_trade, volume_spike, fresh_wallet, whale_cluster, directional_bias
    market: Market
    trades: list[Trade] = field(default_factory=list)
    score: float = 0.0  # 0-10 severity
    details: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def format_telegram(self) -> str:
        """Format alert as a Telegram message."""
        emoji = ALERT_EMOJIS.get(self.alert_type, "⚠️")
        link = f"https://polymarket.com/event/{self.market.event_slug}" if self.market.event_slug else ""

        if self.alert_type == "whale_trade" and self.trades:
            t = self.trades[0]
            wallet_tag = f"{t.wallet[:10]}..."
            return (
                f"{emoji} *WHALE TRADE* on {self.market.title}\n"
                f"💰 ${t.cash_amount:,.0f} {t.side} {t.outcome} @ ${t.price:.2f}\n"
                f"👛 Wallet: `{wallet_tag}`\n"
                f"📊 Market 24h vol: ${self.market.volume_24h:,.0f}\n"
                f"{self.details}\n"
                f"🔗 {link}"
            )

        if self.alert_type == "volume_spike":
            return (
                f"{emoji} *VOLUME SPIKE* on {self.market.title}\n"
                f"{self.details}\n"
                f"🔗 {link}"
            )

        if self.alert_type == "fresh_wallet" and self.trades:
            t = self.trades[0]
            return (
                f"{emoji} *FRESH WALLET* on {self.market.title}\n"
                f"💰 ${t.cash_amount:,.0f} {t.side} {t.outcome} @ ${t.price:.2f}\n"
                f"👛 Wallet: `{t.wallet[:10]}...`\n"
                f"⚠️ New wallet on Polymarket!\n"
                f"{self.details}\n"
                f"🔗 {link}"
            )

        if self.alert_type == "whale_cluster":
            return (
                f"{emoji} *WHALE CLUSTER* on {self.market.title}\n"
                f"{self.details}\n"
                f"🔗 {link}"
            )

        if self.alert_type == "directional_bias":
            return (
                f"{emoji} *DIRECTIONAL BIAS* on {self.market.title}\n"
                f"{self.details}\n"
                f"🔗 {link}"
            )

        # Fallback
        return (
            f"{emoji} *{self.alert_type.upper()}* on {self.market.title}\n"
            f"{self.details}\n"
            f"🔗 {link}"
        )


# ── WalletProfile ────────────────────────────────────────────────────

@dataclass
class WalletProfile:
    """Cached wallet behaviour info."""

    address: str
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trade_count: int = 0
    total_volume_usd: float = 0.0
    is_market_maker: bool = False
    markets_traded: set[str] = field(default_factory=set)
