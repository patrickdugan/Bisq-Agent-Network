"""Explain and shortlist offers. Output is indicative, never executable."""
from decimal import Decimal, ROUND_DOWN
from .models import Snapshot, currency_code, positive_decimal


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
    for offer in candidates[:10]:
        data = offer.as_dict()
        if offer.price_fiat_per_btc is None:
            data["indicative_btc"] = None
            data["price_note"] = "Floating/market quote needs fresh in-app price"
        else:
            data["indicative_btc"] = str(
                (value / offer.price_fiat_per_btc).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            )
            data["price_note"] = "Indicative fixed price; offer size and live availability unverified"
        selections.append(data)
    return {
        "currency": snapshot.currency,
        "requested_fiat": str(value),
        "payment_method": payment_method.strip(),
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at,
        "offers": selections,
        "matched_count": len(candidates),
        "warning": "Educational, read-only comparison. Verify availability, seller reputation, limits, fees and fiat settlement inside Bisq before any trade.",
        "requires_human_trade_confirmation": True,
    }
