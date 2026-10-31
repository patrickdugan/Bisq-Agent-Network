"""Offline qualification of an ILS/JawwalPay cashout corridor.

This is a diagnostic sitting *after* BAN's signed BUY-side shortlist.  It is
never a trade executor, a rail adapter, or an authority to settle fiat.  An
attested Bisq offer is indicative until a funded reservation and signed quote
exist.  JawwalPay's production API and the legality of any proposed crypto-
funded payout are external approval gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

REQUIRED_CAPABILITIES = (
    "licensed_payout_provider", "third_party_recipient_allowed",
    "merchant_payout_access", "kyc_and_sanctions_workflow",
    "idempotency_reference", "status_lookup", "rail_receipt_evidence",
    "local_wallet_float",
)
MAX_INVENTORY_AGE_SECONDS = 300


@dataclass(frozen=True)
class RailCapabilities:
    """Capability assertions, NEVER evidence of authorization by themselves.

    SYNTHETIC_FIXTURE allows a demonstration only. An operator's checklist,
    even with all booleans true, cannot enable real payments in BAN.
    """

    source: str = "UNVERIFIED"
    licensed_payout_provider: bool | None = None
    third_party_recipient_allowed: bool | None = None
    merchant_payout_access: bool | None = None
    kyc_and_sanctions_workflow: bool | None = None
    idempotency_reference: bool | None = None
    status_lookup: bool | None = None
    rail_receipt_evidence: bool | None = None
    local_wallet_float: bool | None = None

    @classmethod
    def synthetic(cls) -> "RailCapabilities":
        return cls(source="SYNTHETIC_FIXTURE", **{name: True for name in REQUIRED_CAPABILITIES})

    def missing(self) -> list[str]:
        return ["rail_" + name + "_unconfirmed" for name in REQUIRED_CAPABILITIES
                if getattr(self, name) is not True]


def illustrative_ils_minor(usd: str, ils_per_usd: str) -> int:
    """Illustrative FX only; caller MUST label rate as synthetic.

    ILS has two minor-unit digits. Never use binary floats for money and never
    infer an FX rate from a live market in this offline module.
    """
    if type(usd) is not str or type(ils_per_usd) is not str:
        raise ValueError("amount and rate must be decimal strings")
    try:
        amount, rate = Decimal(usd), Decimal(ils_per_usd)
        if not all(v.is_finite() and v > 0 for v in (amount, rate)):
            raise ValueError("positive finite decimal amounts required")
        minor = (amount * rate * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid illustrative amount or FX rate") from exc
    if not 0 < minor < 2**63:
        raise ValueError("illustrative amount outside supported range")
    return int(minor)


def _eligible_offer_ids(shortlist: Mapping[str, Any], now: int, target_minor: int) -> list[str]:
    """Defensive diagnostics over advisory shortlist rows, not authorization."""
    offers = shortlist.get("offers")
    if not isinstance(offers, list):
        return []
    eligible: set[str] = set()
    for row in offers:
        if not isinstance(row, dict) or row.get("maker_direction") != "BUY":
            continue
        if row.get("currency") != "ILS":
            continue
        offer_id = row.get("offer_id")
        if not isinstance(offer_id, str) or not offer_id or len(offer_id) > 256:
            continue
        if not isinstance(row.get("provider_pubkey"), str) or len(row["provider_pubkey"]) != 64:
            continue
        inventory = row.get("inventory")
        if not isinstance(inventory, dict) or inventory.get("source") != "provider_self_asserted":
            continue
        a = inventory.get("available_minor")
        m = inventory.get("max_per_payout_minor")
        observed = inventory.get("observed_at")
        expiry = inventory.get("valid_until")
        if any(type(v) is not int for v in (a, m, observed, expiry)):
            continue
        if min(a, m) < target_minor or observed > now + 30 or now - observed > MAX_INVENTORY_AGE_SECONDS:
            continue
        if expiry <= now:
            continue
        indicative = row.get("indicative_net_payout_minor")
        if type(indicative) is not int or indicative < target_minor:
            continue
        if row.get("amount_eligibility") == "outside_advertised_bounds":
            continue
        eligible.add(offer_id)
    return sorted(eligible)


def qualify_corridor(shortlist: Mapping[str, Any], intent: Any, rail: RailCapabilities, *, now: int) -> dict:
    """Return simulation/blocker facts. This function can never approve execution.

    Only a recipient commitment is accepted through intent; never put phone
    numbers or actual wallet destinations in quote/diagnostic output.
    """
    if type(now) is not int or now < 0:
        raise ValueError("now must be an unsigned Unix timestamp")
    if not isinstance(shortlist, Mapping) or not isinstance(rail, RailCapabilities):
        raise ValueError("invalid diagnostic input")
    target = getattr(intent, "amount", None)
    if type(target) is not int or not 0 < target < 2**63:
        raise ValueError("unsupported target minor units")
    blockers = []
    if (getattr(intent, "currency", None), getattr(intent, "rail", None),
            getattr(intent, "exponent", None), getattr(intent, "amount_mode", None)) != (
                "ILS", "jawwal_pay", 2, "receive_exact"):
        blockers.append("requires_ils_jawwal_receive_exact_intent")
    commitment = getattr(intent, "recipient_commitment", None)
    if not isinstance(commitment, str) or len(commitment) != 64 or any(c not in "0123456789abcdef" for c in commitment):
        blockers.append("recipient_commitment_not_canonical")
    if type(getattr(intent, "expiry_unix", None)) is not int or intent.expiry_unix <= now:
        blockers.append("intent_expired")
    if (shortlist.get("direction") != "cashout_maker_buys_btc" or
            shortlist.get("currency") != "ILS" or shortlist.get("rail") != "jawwal_pay" or
            shortlist.get("amount_mode") != "receive_exact" or shortlist.get("amount") != target):
        blockers.append("shortlist_intent_mismatch")
    if shortlist.get("requires_human_trade_confirmation") is not True:
        blockers.append("human_trade_confirmation_required")
    source = shortlist.get("source")
    if source != "SYNTHETIC_FIXTURE":
        blockers.append("no_executable_live_buy_bid_verified")
    if rail.source != "SYNTHETIC_FIXTURE":
        blockers.append("rail_authorization_requires_independent_review")
    blockers.extend(rail.missing())
    offers = _eligible_offer_ids(shortlist, now, target)
    if not offers:
        blockers.append("no_qualified_provider_self_attested_inventory")
    blockers = sorted(set(blockers))
    return {
        "status": "SIMULATION_PASS" if not blockers else "BLOCKED",
        "source": source if isinstance(source, str) else "UNKNOWN",
        "rail": "jawwal_pay", "currency": "ILS", "net_payout_minor": target,
        "indicative_offer_ids": offers, "indicative_offer_count": len(offers),
        "blockers": blockers, "executable_liquidity_verified": False,
        "live_rail_access_verified": False, "payment_execution_enabled": False,
        "funding_path": "BTC_only_mock; USDT_TRC20_requires_separate_authorized_conversion",
        "receipt_status": "unverified_external_rail",
        "warning": "Simulation/advisory only. A Bisq BUY offer and provider-signed float claim do not prove "
                   "executable liquidity; neither authorizes third-party JawwalPay settlement.",
    }


