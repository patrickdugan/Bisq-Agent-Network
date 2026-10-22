"""Optional Bisq 2 market-reference prices, with explicit unknown upstream age."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .models import Offer, Snapshot, currency_code
from .price_contract import ContractViolation, decode_price_quote


@dataclass(frozen=True)
class MarketReference:
    currency: str
    fiat_per_btc: Decimal
    source: str
    observed_at: str
    upstream_age_verified: bool = False

    def as_dict(self) -> dict:
        return {
            "currency": self.currency,
            "fiat_per_btc": str(self.fiat_per_btc),
            "source": self.source,
            "observed_at": self.observed_at,
            "upstream_age_verified": self.upstream_age_verified,
            "warning": "Timestamp is the local fetch time. Bisq 2's quotes endpoint does not expose the provider's price age.",
        }


def decode_reference(data: Any, currency: str, source: str) -> MarketReference:
    code = currency_code(currency)
    if not isinstance(data, dict) or not isinstance(data.get("quotes"), dict):
        raise ContractViolation("missing Bisq market-price quotes envelope")
    reference = data["quotes"].get(code)
    value = decode_price_quote(reference, code)
    return MarketReference(code, value, source, datetime.now(timezone.utc).isoformat())


def coherent_reference(snapshot: Snapshot, reference: MarketReference | None, max_seconds: int = 60) -> MarketReference | None:
    """Freshly fetched locally; underlying oracle's timestamp remains unverified."""
    if reference is None or reference.currency != snapshot.currency or reference.source != snapshot.source:
        return None
    try:
        now = datetime.now(timezone.utc)
        observed = datetime.fromisoformat(reference.observed_at)
        snap_time = datetime.fromisoformat(snapshot.fetched_at)
        delta = abs((observed - snap_time).total_seconds())
        if any(x.tzinfo is None for x in (observed, snap_time)):
            return None
        if delta > max_seconds or (now - observed).total_seconds() > max_seconds:
            return None
        if (observed - now).total_seconds() > 10 or (snap_time - now).total_seconds() > 10:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return reference


def resolved_price(offer: Offer, reference: MarketReference | None) -> tuple[Decimal | None, str]:
    if offer.price_fiat_per_btc is not None:
        return offer.price_fiat_per_btc, "offer_fixed_price"
    if reference is None:
        return None, "reference_unavailable"
    if offer.pricing_type == "MarketPriceSpec":
        return reference.fiat_per_btc, "market_derived_unverified_age"
    if offer.pricing_type == "FloatPriceSpec" and offer.markup_fraction is not None:
        price = (reference.fiat_per_btc * (Decimal("1") + offer.markup_fraction)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if price <= 0:
            return None, "invalid_market_markup"
        return price, "floating_derived_unverified_age"
    return None, "price_not_supported"
