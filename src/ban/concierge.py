"""Advisory buyer shortlist, price provenance and advertised amount bounds."""
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from .models import Snapshot, positive_decimal
from .references import MarketReference, coherent_reference, resolved_price


def shortlist(snapshot: Snapshot, amount: str, payment_method: str = "",
              reference: MarketReference | None = None) -> dict:
    value = positive_decimal(amount)
    if value > Decimal("1000000000"):
        raise ValueError("requested amount exceeds display-only limit")
    preferred = payment_method.strip().casefold()
    if len(preferred) > 100:
        raise ValueError("payment method too long")
    ref = coherent_reference(snapshot, reference)
    candidates = [
        o for o in snapshot.offers
        if o.maker_direction == "SELL"
        and (not preferred or any(preferred == method.casefold() for method in o.payment_methods))
    ]
    candidates.sort(key=lambda o: (
        resolved_price(o, ref)[0] is None,
        resolved_price(o, ref)[0] or Decimal("Infinity"),
        o.offer_id
    ))
    selections = []
    excluded_amount = 0
    for offer in candidates:
        price, provenance = resolved_price(offer, ref)
        status = offer.amount_bounds.contains(value, price)
        if status == "outside_advertised_bounds":
            excluded_amount += 1
            continue
        data = offer.as_dict()
        data["amount_eligibility"] = status
        data["price_provenance"] = provenance
        data["fiat_per_btc"] = str(price) if price is not None else None
        if price is None:
            data["indicative_btc"] = None
            data["price_note"] = "Market reference unavailable; verify current quote inside Bisq"
        else:
            try:
                data["indicative_btc"] = str(
                    (value / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                )
            except (InvalidOperation, ArithmeticError):
                data["indicative_btc"] = None
            data["price_note"] = (
                "Reference-derived estimate; upstream market quote age unverified"
                if provenance != "offer_fixed_price"
                else "Advertised fixed-price estimate; live availability unverified"
            )
        selections.append(data)
    return {
        "currency": snapshot.currency,
        "requested_fiat": str(value),
        "payment_method": payment_method.strip(),
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at,
        "reference_observed_at": ref.observed_at if ref else None,
        "reference_upstream_age_verified": False,
        "offers": selections[:10],
        "matched_count": len(selections),
        "excluded_outside_advertised_bounds": excluded_amount,
        "warning": "Read-only indicative comparison. No live liquidity or reputation eligibility verified. Verify price, limits, fees and fiat settlement in Bisq before trading.",
        "requires_human_trade_confirmation": True,
    }
