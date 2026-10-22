"""Read-only buyer shortlist with advertised amount bounds and exact prices."""
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from .models import Snapshot, positive_decimal


def shortlist(snapshot: Snapshot, amount: str, payment_method: str = "") -> dict:
    value = positive_decimal(amount)
    if value > Decimal("1000000000"):
        raise ValueError("requested amount exceeds display-only limit")
    preferred = payment_method.strip().casefold()
    if len(preferred) > 100:
        raise ValueError("payment method too long")
    candidates = [
        o for o in snapshot.offers
        if o.maker_direction == "SELL"
        and (not preferred or any(preferred == method.casefold() for method in o.payment_methods))
    ]
    candidates.sort(key=lambda o: (o.price_fiat_per_btc is None,
                                   o.price_fiat_per_btc or Decimal("Infinity"), o.offer_id))
    selections = []
    excluded_amount = 0
    for offer in candidates:
        status = offer.amount_bounds.contains(value, offer.price_fiat_per_btc)
        if status == "outside_advertised_bounds":
            excluded_amount += 1
            continue
        data = offer.as_dict()
        data["amount_eligibility"] = status
        if offer.price_fiat_per_btc is None:
            data["indicative_btc"] = None
            data["price_note"] = "Floating/market quote needs a verified local market reference"
        else:
            try:
                data["indicative_btc"] = str(
                    (value / offer.price_fiat_per_btc).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                )
            except (InvalidOperation, ArithmeticError):
                data["indicative_btc"] = None
            data["price_note"] = "Indicative fixed price; live availability and reputation restrictions unverified"
        selections.append(data)
    return {
        "currency": snapshot.currency,
        "requested_fiat": str(value),
        "payment_method": payment_method.strip(),
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at,
        "offers": selections[:10],
        "matched_count": len(selections),
        "excluded_outside_advertised_bounds": excluded_amount,
        "warning": "Read-only advertised bounds check, not an executable quote. Confirm availability, limits, price, fees and fiat settlement inside Bisq.",
        "requires_human_trade_confirmation": True,
    }
