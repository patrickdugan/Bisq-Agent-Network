"""Versioned cashout contracts (spec D.2) and the sender-side quote check.

Each object is a fixed-width, unsigned big-endian encoding (length-prefixed only
where a field is inherently variable) in the style of UTXO-Ref ``claim_v1.py``.
Its ID or digest is a domain-separated SHA-256 of that encoding, and BIP340
signatures are over that 32-byte digest. Money is integer minor units or sats;
conversion uses Decimal with explicit rounding. No floats anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from hashlib import sha256
import re
from typing import Any, Collection

from . import canon
from .bip340 import verify_hex

NETWORKS = {"mainnet": 0, "testnet4": 1, "signet": 2, "regtest": 3}  # claim_v1.py:8
AMOUNT_MODES = {"receive_exact": 0, "send_exact": 1}
FUNDING_SOURCES = {"unknown": 0, "held_btc": 1, "bought_for_payment": 2}  # unverified hint (spec E)
OVERPAY_POLICIES = {"refund_excess": 0, "tip": 1}
UNDERPAY_POLICIES = {"refund_minus_fee": 0, "topup_within_expiry": 1}
REFUND_FEE_POLICIES = {"sender_pays_network_fee": 0}
EVIDENCE_PROFILES = {"own_core_v1": 0, "sp1_btc_payment_v1": 1, "nova_spv_payment_v1": 2,
                     "cashu_bitvm_claim_v1": 3}
RECEIPT_STATUSES = {"delivered": 0, "failed": 1, "unknown": 2}
RAIL_EVIDENCE_KINDS = {"signed_webhook": 0, "api_status": 1, "manual": 2, "provider_attested_only": 3}
ENROLLMENT_STATUSES = {"accepted": 0, "rejected": 1, "name_check_unavailable": 2}
SATS_PER_BTC = 100_000_000
_RAIL = re.compile(r"[a-z0-9_]{1,32}")
_CCY = re.compile(r"[A-Z]{3}")
_REFERENCE = re.compile(r"[A-Za-z0-9._:-]{0,64}")
ZERO32 = "00" * 32


class ContractError(ValueError):
    pass


class QuoteRejected(ValueError):
    pass


def _bytes32(value: str) -> bytes:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ContractError("expected canonical lowercase 32-byte hex")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise ContractError("expected canonical lowercase 32-byte hex") from exc


def _uint(value: int, width: int, *, positive: bool = False) -> bytes:
    if type(value) is not int or value < int(positive) or value >= 1 << (8 * width):
        raise ContractError("integer outside canonical range")
    return value.to_bytes(width, "big")


def _enum(table: dict[str, int], value: str) -> bytes:
    if value not in table:
        raise ContractError("unknown enum value: " + str(value))
    return bytes([table[value]])


def _currency(code: str, exponent: int) -> bytes:
    if not isinstance(code, str) or not _CCY.fullmatch(code):
        raise ContractError("currency must be an ISO 4217 alpha code")
    if type(exponent) is not int or not 0 <= exponent <= 8:
        raise ContractError("minor-unit exponent must be 0..8")
    return code.encode("ascii") + bytes([exponent])


def rail_id(rail: str) -> str:
    if not isinstance(rail, str) or not _RAIL.fullmatch(rail):
        raise ContractError("invalid rail identifier")
    return sha256(rail.encode("ascii")).hexdigest()


def _var(data: bytes, width: int, limit: int) -> bytes:
    if len(data) > limit:
        raise ContractError("variable field too long")
    return len(data).to_bytes(width, "big") + data


def _digest(domain: bytes, payload: bytes) -> str:
    return sha256(domain + payload).hexdigest()


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ContractError("truncated encoding")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def uint(self, width: int) -> int:
        return int.from_bytes(self.take(width), "big")

    def hex32(self) -> str:
        return self.take(32).hex()

    def enum(self, table: dict[str, int]) -> str:
        value = self.uint(1)
        for name, code in table.items():
            if code == value:
                return name
        raise ContractError("unknown enum discriminant")

    def var(self, width: int) -> bytes:
        return self.take(self.uint(width))

    def done(self) -> None:
        if self.pos != len(self.data):
            raise ContractError("trailing bytes")


def standard_script_type(script: bytes) -> str | None:
    if len(script) == 22 and script[:2] == b"\x00\x14":
        return "p2wpkh"
    if len(script) == 34 and script[:2] == b"\x00\x20":
        return "p2wsh"
    if len(script) == 34 and script[:2] == b"\x51\x20":
        return "p2tr"
    return None


# ------------------------------------------------------------------ pricing

@dataclass(frozen=True)
class Pricing:
    amount_mode: str
    rate_minor_per_btc: int
    fee_bps: int
    gross_minor: int
    fee_minor: int
    net_payout_minor: int
    sats_required: int
    fee_sats: int


def rate_from_price(fiat_per_btc: Decimal, exponent: int) -> int:
    """Bisq's 4-decimal fiat price -> integer minor units per BTC, rounded down.
    A Bisq price precision is never reused as a payout unit (spec D.1)."""
    if not isinstance(fiat_per_btc, Decimal) or not fiat_per_btc.is_finite() or fiat_per_btc <= 0:
        raise ContractError("price must be a positive Decimal")
    with localcontext() as ctx:
        ctx.prec = 80
        rate = int((fiat_per_btc.scaleb(exponent)).to_integral_value(rounding=ROUND_FLOOR))
    if rate <= 0:
        raise ContractError("rate rounds to zero")
    return rate


def _sats_for(minor: int, rate: int) -> int:
    with localcontext() as ctx:
        ctx.prec = 80
        return int((Decimal(minor) * SATS_PER_BTC / Decimal(rate)).to_integral_value(rounding=ROUND_CEILING))


def price(amount_mode: str, amount: int, rate_minor_per_btc: int, fee_bps: int) -> Pricing:
    """Price a cashout. Fees are basis points of the gross local amount.

    receive_exact: ``amount`` is the net minor units the recipient gets; the gross
    rounds UP and sats round UP (the sender covers rounding).
    send_exact: ``amount`` is sats sent; the gross rounds DOWN, the fee rounds UP
    and the net is what remains (the recipient bears rounding).
    """
    if type(amount) is not int or amount <= 0 or type(rate_minor_per_btc) is not int or rate_minor_per_btc <= 0:
        raise ContractError("amount and rate must be positive integers")
    if type(fee_bps) is not int or not 0 <= fee_bps < 10000:
        raise ContractError("fee_bps must be 0..9999")
    with localcontext() as ctx:
        ctx.prec = 80
        if amount_mode == "receive_exact":
            net = amount
            gross = int((Decimal(net) * 10000 / Decimal(10000 - fee_bps)).to_integral_value(rounding=ROUND_CEILING))
            fee = gross - net
            sats = _sats_for(gross, rate_minor_per_btc)
        elif amount_mode == "send_exact":
            sats = amount
            gross = int((Decimal(sats) * Decimal(rate_minor_per_btc) / SATS_PER_BTC)
                        .to_integral_value(rounding=ROUND_FLOOR))
            fee = int((Decimal(gross) * fee_bps / 10000).to_integral_value(rounding=ROUND_CEILING))
            net = gross - fee
        else:
            raise ContractError("unknown amount mode")
    if net <= 0:
        raise ContractError("amount too small after fees")
    return Pricing(amount_mode, rate_minor_per_btc, fee_bps, gross, fee, net, sats,
                   _sats_for(fee, rate_minor_per_btc) if fee else 0)


# ---------------------------------------------------------------- contracts

@dataclass(frozen=True)
class PayoutIntentV1:
    network: str
    sender_pubkey: str
    alias_pubkey: str
    alias_record_id: str
    alias_seq: int
    recipient_commitment: str
    rail: str
    currency: str
    exponent: int
    amount_mode: str
    amount: int
    max_total_fee_sats: int
    max_fee_bps: int
    min_net_payout_minor: int
    refund_script_sha256: str
    sender_nonce: str
    expiry_unix: int
    funding_source: str = "unknown"

    def encode(self) -> bytes:
        if self.max_fee_bps > 10000:
            raise ContractError("max_fee_bps above 10000")
        return b"".join((
            _uint(1, 1), _enum(NETWORKS, self.network),
            _bytes32(self.sender_pubkey), _bytes32(self.alias_pubkey), _bytes32(self.alias_record_id),
            _uint(self.alias_seq, 4, positive=True), _bytes32(self.recipient_commitment),
            _bytes32(rail_id(self.rail)), _currency(self.currency, self.exponent),
            _enum(AMOUNT_MODES, self.amount_mode), _uint(self.amount, 8, positive=True),
            _uint(self.max_total_fee_sats, 8), _uint(self.max_fee_bps, 2),
            _uint(self.min_net_payout_minor, 8), _bytes32(self.refund_script_sha256),
            _bytes32(self.sender_nonce), _uint(self.expiry_unix, 8, positive=True),
            _enum(FUNDING_SOURCES, self.funding_source),
        ))

    def intent_id(self) -> str:
        return _digest(b"CAG-PAYOUT-INTENT-V1\x00", self.encode())

    @classmethod
    def decode(cls, data: bytes, rail: str) -> "PayoutIntentV1":
        """The rail travels by name; its hash in the encoding must match."""
        r = _Reader(data)
        if r.uint(1) != 1:
            raise ContractError("unsupported intent version")
        network = r.enum(NETWORKS)
        sender, alias, record, seq, commitment = r.hex32(), r.hex32(), r.hex32(), r.uint(4), r.hex32()
        if r.hex32() != rail_id(rail):
            raise ContractError("rail does not match encoded rail ID")
        currency, exponent = r.take(3).decode("ascii"), r.uint(1)
        out = cls(network, sender, alias, record, seq, commitment, rail, currency, exponent,
                  r.enum(AMOUNT_MODES), r.uint(8), r.uint(8), r.uint(2), r.uint(8), r.hex32(), r.hex32(),
                  r.uint(8), r.enum(FUNDING_SOURCES))
        r.done()
        if out.encode() != data:
            raise ContractError("non-canonical intent encoding")
        return out


@dataclass(frozen=True)
class ProviderQuoteV1:
    network: str
    intent_id: str
    provider_pubkey: str
    reservation_id: str
    deposit_script: str  # hex
    sats_required: int
    overpay_policy: str
    underpay_policy: str
    rate_minor_per_btc: int
    currency: str
    exponent: int
    fee_sats: int
    fee_minor: int
    net_payout_minor: int
    quote_created_height: int
    quote_expiry_height: int
    quote_expiry_unix: int
    min_conf: int
    max_tip_lag: int
    evidence_policy_digest: str
    refund_fee_policy: str = "sender_pays_network_fee"

    @property
    def deposit_script_sha256(self) -> str:
        return sha256(bytes.fromhex(self.deposit_script)).hexdigest()

    def encode(self) -> bytes:
        script = bytes.fromhex(self.deposit_script)
        if self.quote_expiry_height <= self.quote_created_height:
            raise ContractError("quote must expire after its creation height")
        return b"".join((
            _uint(1, 1), _enum(NETWORKS, self.network), _bytes32(self.intent_id),
            _bytes32(self.provider_pubkey), _bytes32(self.reservation_id),
            _var(script, 1, 64), _bytes32(self.deposit_script_sha256),
            _uint(self.sats_required, 8, positive=True),
            _enum(OVERPAY_POLICIES, self.overpay_policy), _enum(UNDERPAY_POLICIES, self.underpay_policy),
            _uint(self.rate_minor_per_btc, 8, positive=True), _currency(self.currency, self.exponent),
            _uint(self.fee_sats, 8), _uint(self.fee_minor, 8), _uint(self.net_payout_minor, 8, positive=True),
            _uint(self.quote_created_height, 4, positive=True), _uint(self.quote_expiry_height, 4, positive=True),
            _uint(self.quote_expiry_unix, 8, positive=True), _uint(self.min_conf, 4, positive=True),
            _uint(self.max_tip_lag, 4), _bytes32(self.evidence_policy_digest),
            _enum(REFUND_FEE_POLICIES, self.refund_fee_policy),
        ))

    def quote_id(self) -> str:
        return _digest(b"CAG-PROVIDER-QUOTE-V1\x00", self.encode())

    def idempotency_key(self) -> str:
        return _digest(b"CAG-PAYOUT-IDEMP-V1\x00", bytes.fromhex(self.quote_id()))

    @classmethod
    def decode(cls, data: bytes) -> "ProviderQuoteV1":
        r = _Reader(data)
        if r.uint(1) != 1:
            raise ContractError("unsupported quote version")
        network, intent, provider, reservation = r.enum(NETWORKS), r.hex32(), r.hex32(), r.hex32()
        script = r.var(1)
        if r.hex32() != sha256(script).hexdigest():
            raise ContractError("deposit script hash mismatch")
        sats, over, under, rate = r.uint(8), r.enum(OVERPAY_POLICIES), r.enum(UNDERPAY_POLICIES), r.uint(8)
        currency, exponent = r.take(3).decode("ascii"), r.uint(1)
        out = cls(network, intent, provider, reservation, script.hex(), sats, over, under, rate, currency,
                  exponent, r.uint(8), r.uint(8), r.uint(8), r.uint(4), r.uint(4), r.uint(8), r.uint(4),
                  r.uint(4), r.hex32(), r.enum(REFUND_FEE_POLICIES))
        r.done()
        if out.encode() != data:
            raise ContractError("non-canonical quote encoding")
        return out


@dataclass(frozen=True)
class InventoryAttestationV1:
    """Self-asserted by the provider (spec E). ``offer_id`` binds it to one Bisq offer."""
    provider_pubkey: str
    offer_id: str
    rail: str
    currency: str
    exponent: int
    available_minor: int
    max_per_payout_minor: int
    observed_at: int
    valid_until: int

    def encode(self) -> bytes:
        if not isinstance(self.offer_id, str) or not 1 <= len(self.offer_id) <= 256:
            raise ContractError("invalid offer ID")
        if self.valid_until <= self.observed_at:
            raise ContractError("attestation must expire after observation")
        return b"".join((
            _uint(1, 1), _bytes32(self.provider_pubkey),
            sha256(self.offer_id.encode("utf-8")).digest(), _bytes32(rail_id(self.rail)),
            _currency(self.currency, self.exponent), _uint(self.available_minor, 8),
            _uint(self.max_per_payout_minor, 8), _uint(self.observed_at, 8, positive=True),
            _uint(self.valid_until, 8, positive=True),
        ))

    def digest(self) -> str:
        return _digest(b"CAG-INVENTORY-ATTEST-V1\x00", self.encode())


@dataclass(frozen=True)
class SignedInventory:
    attestation: InventoryAttestationV1
    sig: str


@dataclass(frozen=True)
class EvidencePolicyV1:
    """Accepted evidence profiles. Named by digest in the quote. ``profiles`` maps a
    profile name to its parameters plus ``approval`` = {state, by, at}."""
    mode: str  # "test" | "production"
    network: str
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        if self.mode not in ("test", "production") or self.network not in NETWORKS:
            raise ContractError("invalid evidence policy mode or network")
        for name, profile in self.profiles.items():
            if name not in EVIDENCE_PROFILES:
                raise ContractError("unknown evidence profile: " + name)
            approval = profile.get("approval") if isinstance(profile, dict) else None
            if not isinstance(approval, dict) or approval.get("state") not in ("candidate", "approved"):
                raise ContractError("every profile needs approval.state candidate|approved")
        return {"v": 1, "mode": self.mode, "network": self.network, "profiles": self.profiles}

    def digest(self) -> str:
        return _digest(b"CAG-EVIDENCE-POLICY-V1\x00", canon.dumps(self.as_dict()).encode("utf-8"))


@dataclass(frozen=True)
class FundingAcceptanceV1:
    network: str
    quote_id: str
    txid: str
    vout: int
    value: int
    script_sha256: str
    block_hash: str
    block_height: int
    confirmations: int
    tip_hash_at_acceptance: str
    evidence_profile: str
    evidence_digest: str

    def encode(self) -> bytes:
        return b"".join((
            _uint(1, 1), _enum(NETWORKS, self.network), _bytes32(self.quote_id), _bytes32(self.txid),
            _uint(self.vout, 4), _uint(self.value, 8, positive=True), _bytes32(self.script_sha256),
            _bytes32(self.block_hash), _uint(self.block_height, 4), _uint(self.confirmations, 4, positive=True),
            _bytes32(self.tip_hash_at_acceptance), _enum(EVIDENCE_PROFILES, self.evidence_profile),
            _bytes32(self.evidence_digest),
        ))

    def digest(self) -> str:
        return _digest(b"CAG-FUNDING-ACCEPT-V1\x00", self.encode())

    @property
    def outpoint(self) -> str:
        return self.txid + ":" + str(self.vout)


@dataclass(frozen=True)
class PayoutAuthorizationV1:
    quote_id: str
    funding_acceptance_digest: str
    destination_ref: str  # provider-internal opaque ID, never the destination
    net_payout_minor: int
    currency: str
    exponent: int
    idempotency_key: str
    alias_seq: int

    def encode(self) -> bytes:
        return b"".join((
            _uint(1, 1), _bytes32(self.quote_id), _bytes32(self.funding_acceptance_digest),
            _bytes32(self.destination_ref), _uint(self.net_payout_minor, 8, positive=True),
            _currency(self.currency, self.exponent), _bytes32(self.idempotency_key),
            _uint(self.alias_seq, 4, positive=True),
        ))

    def digest(self) -> str:
        return _digest(b"CAG-PAYOUT-AUTH-V1\x00", self.encode())


@dataclass(frozen=True)
class PayoutReceiptV1:
    quote_id: str
    intent_id: str
    idempotency_key: str
    rail: str
    rail_reference: str
    amount_minor: int
    currency: str
    exponent: int
    status: str
    rail_evidence_kind: str
    rail_evidence_digest: str
    delivered_at: int

    def encode(self) -> bytes:
        if not isinstance(self.rail_reference, str) or not _REFERENCE.fullmatch(self.rail_reference):
            raise ContractError("rail reference must be at most 64 safe ASCII characters")
        return b"".join((
            _uint(1, 1), _bytes32(self.quote_id), _bytes32(self.intent_id), _bytes32(self.idempotency_key),
            _bytes32(rail_id(self.rail)), _var(self.rail_reference.encode("ascii"), 1, 64),
            _uint(self.amount_minor, 8), _currency(self.currency, self.exponent),
            _enum(RECEIPT_STATUSES, self.status), _enum(RAIL_EVIDENCE_KINDS, self.rail_evidence_kind),
            _bytes32(self.rail_evidence_digest), _uint(self.delivered_at, 8),
        ))

    def receipt_id(self) -> str:
        """Stable per payout: derived from the idempotency key alone (spec B.3 T8)."""
        return _digest(b"CAG-PAYOUT-RECEIPT-ID-V1\x00", _bytes32(self.idempotency_key))

    def digest(self) -> str:
        return _digest(b"CAG-PAYOUT-RECEIPT-V1\x00", self.encode())


def verify_receipt(receipt: PayoutReceiptV1, sig: str, quote: ProviderQuoteV1) -> None:
    """S/R check: the receipt names this quote's payout and P signed it."""
    if receipt.quote_id != quote.quote_id() or receipt.idempotency_key != quote.idempotency_key():
        raise QuoteRejected("receipt is not bound to this quote's idempotency key")
    if receipt.intent_id != quote.intent_id:
        raise QuoteRejected("receipt names another intent")
    if not verify_hex(quote.provider_pubkey, receipt.digest(), sig):
        raise QuoteRejected("bad receipt signature")


@dataclass(frozen=True)
class DestinationEnrollmentV1:
    """Private R->P message: opens the public commitment for one provider."""
    alias_pubkey: str
    record_id: str
    seq: int
    rail: str
    canonical_destination: str
    salt: str
    commitment: str
    provider_pubkey: str
    created_at: int

    def encode(self) -> bytes:
        return b"".join((
            _uint(1, 1), _bytes32(self.alias_pubkey), _bytes32(self.record_id), _uint(self.seq, 4, positive=True),
            _bytes32(rail_id(self.rail)), _var(self.canonical_destination.encode("utf-8"), 2, 256),
            _bytes32(self.salt), _bytes32(self.commitment), _bytes32(self.provider_pubkey),
            _uint(self.created_at, 8, positive=True),
        ))

    def digest(self) -> str:
        return _digest(b"CAG-DEST-ENROLL-V1\x00", self.encode())


@dataclass(frozen=True)
class EnrollmentAckV1:
    enrollment_digest: str
    provider_pubkey: str
    status: str

    def encode(self) -> bytes:
        return b"".join((_uint(1, 1), _bytes32(self.enrollment_digest), _bytes32(self.provider_pubkey),
                         _enum(ENROLLMENT_STATUSES, self.status)))

    def digest(self) -> str:
        return _digest(b"CAG-ENROLL-ACK-V1\x00", self.encode())


def verify_intent(intent: PayoutIntentV1, sig: str) -> None:
    if not verify_hex(intent.sender_pubkey, intent.intent_id(), sig):
        raise QuoteRejected("bad intent signature")


def check_quote(quote: ProviderQuoteV1, sig: str, intent: PayoutIntentV1, resolution: Any, *,
                tip_height: int, now: int, seen_deposit_scripts: Collection[str] = ()) -> None:
    """Sender-side T3 check. Raises ``QuoteRejected`` naming the first failure.

    ``resolution`` is an ``alias.Resolution``; ``seen_deposit_scripts`` holds the
    deposit-script hashes of every earlier quote this sender accepted.
    """
    record = resolution.record
    if quote.network != intent.network:
        raise QuoteRejected("wrong network")
    if quote.intent_id != intent.intent_id():
        raise QuoteRejected("quote names a different intent_id")
    if (intent.alias_pubkey, intent.alias_record_id, intent.alias_seq) != (record.pubkey, record.event_id, record.seq):
        raise QuoteRejected("intent does not match the resolved alias record")
    if record.status != "active":
        raise QuoteRejected("alias record is not active")
    target = record.target_for(intent.recipient_commitment)
    if target is None or target.rail != intent.rail or target.currency != intent.currency:
        raise QuoteRejected("wrong recipient commitment")
    if quote.provider_pubkey not in target.providers:
        raise QuoteRejected("substituted provider key: not in the record's provider list")
    if not verify_hex(quote.provider_pubkey, quote.quote_id(), sig):
        raise QuoteRejected("bad quote signature")
    if (quote.currency, quote.exponent) != (intent.currency, intent.exponent):
        raise QuoteRejected("currency or exponent mismatch")
    script = bytes.fromhex(quote.deposit_script)
    if standard_script_type(script) is None:
        raise QuoteRejected("non-standard deposit script")
    if quote.deposit_script_sha256 in seen_deposit_scripts:
        raise QuoteRejected("deposit script reused across quotes")
    if tip_height >= quote.quote_expiry_height:
        raise QuoteRejected("expired quote (height)")
    if now >= quote.quote_expiry_unix or now >= intent.expiry_unix:
        raise QuoteRejected("expired quote (time)")
    if quote.quote_expiry_unix > intent.expiry_unix:
        raise QuoteRejected("quote outlives the intent")
    if not tip_height - quote.max_tip_lag <= quote.quote_created_height <= tip_height:
        raise QuoteRejected("quote_created_height is not near the current tip")
    fee_bps_ceiling = -(-intent.max_fee_bps * (quote.net_payout_minor + quote.fee_minor) // 10000)
    if quote.fee_sats > intent.max_total_fee_sats or quote.fee_minor > fee_bps_ceiling:
        raise QuoteRejected("fee above the sender's cap")
    if quote.net_payout_minor < intent.min_net_payout_minor:
        raise QuoteRejected("net payout below the sender's minimum")
    gross = quote.net_payout_minor + quote.fee_minor
    if intent.amount_mode == "receive_exact":
        if quote.net_payout_minor != intent.amount or quote.sats_required != _sats_for(gross, quote.rate_minor_per_btc):
            raise QuoteRejected("quote amounts disagree with the stated rate")
    else:
        if quote.sats_required != intent.amount or gross > intent.amount * quote.rate_minor_per_btc // SATS_PER_BTC:
            raise QuoteRejected("quote amounts disagree with the stated rate")
    if quote.fee_sats != (_sats_for(quote.fee_minor, quote.rate_minor_per_btc) if quote.fee_minor else 0):
        raise QuoteRejected("fee_sats disagrees with the stated rate")
