"""Advisory BUY-side (cashout) shortlist.

``concierge.shortlist`` serves BTC buyers and keeps only makers SELLING BTC.
Cashout is the other direction: it needs makers BUYING BTC that hold local
payout float. This is a separate code path on purpose; neither function ever
returns the other's direction. Inventory is self-asserted by the provider
(spec E) and only narrows the list; it is not executable liquidity.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Collection, Mapping

from .bip340 import verify_hex
from .intent import ContractError, PayoutIntentV1, SignedInventory, price, rate_from_price
from .models import Snapshot
from .references import MarketReference, coherent_reference, resolved_price

INVENTORY_MAX_AGE_SECONDS = 300
INVENTORY_FUTURE_SKEW_SECONDS = 30


def _attestation_problem(signed: SignedInventory | None, offer_id: str, intent: PayoutIntentV1,
                         providers: frozenset[str], now: int, max_age: int) -> str | None:
    if signed is None:
        return "missing_inventory"
    att = signed.attestation
    try:
        digest = att.digest()
    except ContractError:
        return "malformed_inventory"
    if (att.offer_id != offer_id or att.provider_pubkey not in providers
            or not verify_hex(att.provider_pubkey, digest, signed.sig)):
        return "wrong_key_inventory"
    if (att.rail, att.currency, att.exponent) != (intent.rail, intent.currency, intent.exponent):
        return "rail_mismatch"
    if now >= att.valid_until or now - att.observed_at > max_age or att.observed_at > now + INVENTORY_FUTURE_SKEW_SECONDS:
        return "stale_inventory"
    return None


def cashout_shortlist(snapshot: Snapshot, intent: PayoutIntentV1, inventory: Mapping[str, SignedInventory], *,
                      providers: Collection[str], now: int, reference: MarketReference | None = None,
                      max_inventory_age: int = INVENTORY_MAX_AGE_SECONDS) -> dict:
    """Rank makers BUYING BTC for ``intent``.

    ``inventory`` maps offer ID to a provider-signed attestation. ``providers`` is
    the provider list from the recipient's resolved record for the intent's
    commitment: an attestation signed by any other key is excluded.
    """
    if snapshot.currency != intent.currency:
        raise ValueError("offerbook currency does not match the intent")
    allowed = frozenset(providers)
    ref = coherent_reference(snapshot, reference)
    excluded: Counter[str] = Counter()
    selections = []
    for offer in snapshot.offers:
        if offer.maker_direction != "BUY":
            excluded["maker_sells_btc"] += 1
            continue
        signed = inventory.get(offer.offer_id)
        problem = _attestation_problem(signed, offer.offer_id, intent, allowed, now, max_inventory_age)
        if problem:
            excluded[problem] += 1
            continue
        fiat_per_btc, provenance = resolved_price(offer, ref)
        if fiat_per_btc is None:
            excluded["price_unavailable"] += 1
            continue
        try:
            rate = rate_from_price(fiat_per_btc, intent.exponent)
            # Indicative only: the provider's own fee is fixed later, in its signed quote.
            pricing = price(intent.amount_mode, intent.amount, rate, 0)
        except ContractError:
            excluded["price_unavailable"] += 1
            continue
        att = signed.attestation
        if pricing.net_payout_minor > min(att.available_minor, att.max_per_payout_minor):
            excluded["insufficient_inventory"] += 1
            continue
        gross_major = Decimal(pricing.gross_minor).scaleb(-intent.exponent)
        eligibility = offer.amount_bounds.contains(gross_major, fiat_per_btc)
        if eligibility == "outside_advertised_bounds":
            excluded["outside_advertised_bounds"] += 1
            continue
        data = offer.as_dict()
        data.update({
            "amount_eligibility": eligibility,
            "price_provenance": provenance,
            "fiat_per_btc": str(fiat_per_btc),
            "provider_pubkey": att.provider_pubkey,
            "rate_minor_per_btc": rate,
            "indicative_sats_required": pricing.sats_required,
            "indicative_net_payout_minor": pricing.net_payout_minor,
            "inventory": {"available_minor": att.available_minor, "max_per_payout_minor": att.max_per_payout_minor,
                          "observed_at": att.observed_at, "valid_until": att.valid_until,
                          "source": "provider_self_asserted"},
        })
        selections.append(data)
    if intent.amount_mode == "receive_exact":
        selections.sort(key=lambda d: (d["indicative_sats_required"], d["offer_id"]))
    else:
        selections.sort(key=lambda d: (-d["indicative_net_payout_minor"], d["offer_id"]))
    return {
        "direction": "cashout_maker_buys_btc",
        "currency": snapshot.currency,
        "rail": intent.rail,
        "amount_mode": intent.amount_mode,
        "amount": intent.amount,
        "source": snapshot.source,
        "offers": selections[:10],
        "matched_count": len(selections),
        "excluded": dict(sorted(excluded.items())),
        "warning": "Advisory. Inventory is provider self-asserted; pricing excludes the provider fee, "
                   "which is fixed only in its signed quote. No payout happens here.",
        "requires_human_trade_confirmation": True,
    }
