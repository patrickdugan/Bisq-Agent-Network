"""Normalize Bisq 2's offer DTOs without guessing their financial semantics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any

CURRENCY = re.compile(r"^[A-Z]{3,5}$")


class InvalidData(ValueError):
    pass


def currency_code(value: str) -> str:
    code = str(value).upper()
    if not CURRENCY.fullmatch(code):
        raise InvalidData("currency must be 3 to 5 ASCII letters")
    return code


def positive_decimal(value: str | Decimal) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise InvalidData("invalid decimal amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise InvalidData("amount must be finite and positive")
    return amount


def fixed_price(spec: dict[str, Any], currency: str) -> Decimal | None:
    # Floating and market-price offers require additional live quote state.
    if not isinstance(spec, dict) or spec.get("type") != "FixPriceSpec":
        return None
    quote = spec.get("priceQuote")
    if not isinstance(quote, dict):
        raise InvalidData("fixed-price quote missing")
    value, precision = quote.get("value"), quote.get("precision")
    if type(value) is not int or type(precision) is not int or not (0 <= precision <= 12):
        raise InvalidData("invalid fixed-price encoding")
    market = quote.get("market", {})
    if not isinstance(market, dict):
        raise InvalidData("invalid fixed-price market")
    if market.get("baseCurrencyCode") != "BTC" or market.get("quoteCurrencyCode") != currency:
        raise InvalidData("price quote market mismatch")
    price = Decimal(value).scaleb(-precision)
    if price <= 0 or not price.is_finite():
        raise InvalidData("nonpositive fixed price")
    return price


@dataclass(frozen=True)
class Offer:
    offer_id: str
    currency: str
    maker_direction: str
    nickname: str
    payment_methods: tuple[str, ...]
    price_fiat_per_btc: Decimal | None
    amount_display: str
    reputation_score: int | None
    pricing_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "currency": self.currency,
            "maker_direction": self.maker_direction,
            "nickname": self.nickname,
            "payment_methods": list(self.payment_methods),
            "fiat_per_btc": str(self.price_fiat_per_btc) if self.price_fiat_per_btc else None,
            "amount_display": self.amount_display,
            "amount_eligibility": "unverified",
            "reputation_score": self.reputation_score,
            "pricing_type": self.pricing_type,
        }


@dataclass(frozen=True)
class Snapshot:
    currency: str
    source: str
    fetched_at: str
    offers: tuple[Offer, ...]
    rejected_rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "offer_count": len(self.offers),
            "rejected_rows": self.rejected_rows,
            "offers": [offer.as_dict() for offer in self.offers],
        }


def normalize_offer(row: dict[str, Any], currency: str) -> Offer:
    if not isinstance(row, dict):
        raise InvalidData("offer entry must be an object")
    raw = row.get("bisqEasyOffer")
    if not isinstance(raw, dict):
        raise InvalidData("missing bisqEasyOffer")
    market = raw.get("market")
    if not isinstance(market, dict) or market.get("baseCurrencyCode") != "BTC" or market.get("quoteCurrencyCode") != currency:
        raise InvalidData("offer currency mismatch")
    offer_id = raw.get("id")
    direction = raw.get("direction")
    if not isinstance(offer_id, str) or not (1 <= len(offer_id) <= 256):
        raise InvalidData("invalid offer ID")
    if direction not in ("BUY", "SELL"):
        raise InvalidData("invalid maker direction")
    spec = raw.get("priceSpec", {})
    if not isinstance(spec, dict):
        raise InvalidData("invalid price spec")
    methods = row.get("quoteSidePaymentMethods") or []
    if not isinstance(methods, list) or any(not isinstance(m, str) for m in methods):
        raise InvalidData("invalid payment methods")
    profile = row.get("userProfile") or {}
    if not isinstance(profile, dict):
        raise InvalidData("invalid user profile")
    nickname = profile.get("nickName") or "Pseudonymous merchant"
    if not isinstance(nickname, str):
        nickname = "Pseudonymous merchant"
    reputation = row.get("reputationScore") or {}
    score = reputation.get("totalScore") if isinstance(reputation, dict) else None
    if type(score) is not int:
        score = None
    return Offer(
        offer_id=offer_id,
        currency=currency,
        maker_direction=direction,
        nickname=nickname[:100],
        payment_methods=tuple(m[:100] for m in methods[:30]),
        price_fiat_per_btc=fixed_price(spec, currency),
        amount_display=str(row.get("formattedQuoteAmount") or "See Bisq app")[:120],
        reputation_score=score,
        pricing_type=str(spec.get("type") or "unknown")[:50],
    )


def normalize_snapshot(data: Any, currency: str, source: str) -> Snapshot:
    code = currency_code(currency)
    if not isinstance(data, list) or len(data) > 10000:
        raise InvalidData("offerbook must be an array of at most 10000 entries")
    offers: list[Offer] = []
    rejected = 0
    for row in data:
        try:
            offers.append(normalize_offer(row, code))
        except (InvalidData, TypeError, ValueError, OverflowError):
            rejected += 1
    return Snapshot(
        currency=code,
        source=source,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        offers=tuple(offers),
        rejected_rows=rejected,
    )
