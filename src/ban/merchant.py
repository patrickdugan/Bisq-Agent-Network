"""Merchant-side quote advisor without any publication/execution capability."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from .models import positive_decimal
from .references import MarketReference


def draft_merchant_quote(reference: MarketReference, *, margin_bps: str, fiat_amount: str) -> dict:
    # The upstream quote's age is unknown; require a recent local observation.
    try:
        observed = datetime.fromisoformat(reference.observed_at)
        now = datetime.now(timezone.utc)
        if observed.tzinfo is None or not -10 <= (now - observed).total_seconds() <= 60:
            raise ValueError("local market reference observation is stale")
    except (TypeError, OverflowError):
        raise ValueError("invalid market reference timestamp") from None
    ticket = positive_decimal(fiat_amount)
    if ticket > Decimal("1000000000"):
        raise ValueError("display amount too large")
    try:
        margin = Decimal(str(margin_bps))
    except (ValueError, ArithmeticError):
        raise ValueError("invalid spread in basis points") from None
    if not margin.is_finite() or not 0 <= margin <= 2500 or margin.as_tuple().exponent < -2:
        raise ValueError("margin_bps must be 0 to 2500 with at most two decimal places")
    ask = (reference.fiat_per_btc * (Decimal("1") + margin / Decimal("10000"))).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    if ask <= 0:
        raise ValueError("invalid calculated merchant price")
    try:
        quantity = (ticket / ask).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    except (InvalidOperation, ArithmeticError):
        raise ValueError("unable to calculate indicative BTC amount") from None
    return {
        "approval_state": "draft_only",
        "action": "merchant_quote_advice",
        "currency": reference.currency,
        "reference_fiat_per_btc": str(reference.fiat_per_btc),
        "reference_observed_at": reference.observed_at,
        "upstream_age_verified": reference.upstream_age_verified,
        "margin_bps": str(margin),
        "suggested_fiat_per_btc": str(ask),
        "example_fiat_amount": str(ticket),
        "indicative_btc": str(quantity),
        "route": "none",
        "warning": "Local drafting only. Source market price age, payment reversals, merchant reputation, inventory, executable size and local rules require independent verification. Nothing has been published to Bisq.",
    }
