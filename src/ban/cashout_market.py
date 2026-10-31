"""Read-only BTC/ILS bid discovery from a Bisq offer snapshot.

An advertised BUY offer is not executable settlement, local wallet float,
licensed remittance capacity, or permission to pay a third-party beneficiary.
Only the previously existing loopback-only Bisq adapter may fetch a snapshot.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .models import Snapshot
from .references import MarketReference, coherent_reference, resolved_price


def _major_ils(value: str) -> Decimal:
    if type(value) is not str:
        raise ValueError("ILS amount must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid ILS amount") from exc
    if not amount.is_finite() or not (Decimal("0.01") <= amount <= Decimal("100000000")):
        raise ValueError("ILS amount outside supported range")
    if amount.as_tuple().exponent < -2:
        raise ValueError("ILS uses two minor-unit digits")
    return amount


def inspect_ils_buy_orders(snapshot: Snapshot, target_ils: str,
                           reference: MarketReference | None = None) -> dict[str, Any]:
    """Describe BTC buyers and any JawwalPay payment-method mentions.

    This is an *advisory* read, even if the source is BISQ2_LOCAL. No
    self-asserted orderbook row is used as payout authorization. No recipient
    address, phone, external URL, trade message, signature or key is processed.
    """
    if not isinstance(snapshot, Snapshot) or snapshot.currency != "ILS":
        raise ValueError("BTC/ILS Snapshot required")
    amount = _major_ils(target_ils)
    ref = coherent_reference(snapshot, reference)
    rows = []
    selling = 0
    for offer in snapshot.offers:
        if offer.maker_direction != "BUY":
            selling += 1
            continue
        rate, provenance = resolved_price(offer, ref)
        bounds = offer.amount_bounds.contains(amount, rate)
        # Mentions in untrusted maker-supplied payment-method descriptions
        # are hints, not certification that the rail is supported.
        mentions = any("jawwal" in method.casefold().replace(" ", "").replace("-", "")
                       or "جوال" in method for method in offer.payment_methods)
        rows.append({
            "offer_id": offer.offer_id,
            "fiat_per_btc": str(rate) if rate is not None else None,
            "price_provenance": provenance,
            "advertised_payment_methods": list(offer.payment_methods),
            "advertised_size_eligibility": bounds,
            "jawwalpay_text_mention": mentions,
            "counterparty_acceptance_confirmed": False,
            "reserved_ils_float_verified": False,
            "third_party_beneficiary_permitted": False,
        })
    # An offer that merely mentions Jawwal is shown first. Fixed-price offers
    # with declared bounds follow. No synthetic profitability score is added.
    rows.sort(key=lambda x: (
        not x["jawwalpay_text_mention"],
        x["advertised_size_eligibility"] == "outside_advertised_bounds",
        x["fiat_per_btc"] is None,
        x["offer_id"],
    ))
    compatible = sum(1 for row in rows if row["jawwalpay_text_mention"]
                     and row["advertised_size_eligibility"] == "within_advertised_bounds")
    return {
        "source": snapshot.source,
        "snapshot_local_fetch_time": snapshot.fetched_at,
        "upstream_offer_age_verified": False,
        "market": "BTC/ILS",
        "target_ils": str(amount),
        "buy_side_advertised_offers": len(rows),
        "sell_side_excluded": selling,
        "invalid_offer_rows_excluded": snapshot.rejected_rows,
        "jawwalpay_text_mentions_with_in_range_size": compatible,
        "advertised_offers": rows[:50],
        "offers_truncated": len(rows) > 50,
        "executable_bids_verified": 0,
        "authorized_third_party_payout_providers_verified": 0,
        "can_send_money": False,
        "next_gate": "Maker acceptance, signed executable quote, independently confirmed NIS float, "
                     "and licensed third-party JawwalPay payout approval",
        "warning": "Read-only; Bisq BUY offerbook entries and method text are unaudited, non-executable "
                   "advertisements. The sender's beneficiary is outside the Bisq trade contract.",
    }
