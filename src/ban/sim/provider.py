"""Simulated reference liquidity provider (spec B.3 T0c-T9). SIMULATION ONLY.

The provider BUYS BTC: it quotes a fresh deposit script, accepts funding from
its own node's view, authorizes a payout write-ahead and pays through a mock
rail. There is no executor for real payouts: ``policy.evaluate`` keeps
``authorize_local_payout`` and ``submit_local_payout`` at DENY.
"""
from __future__ import annotations

import hmac
from hashlib import sha256
from pathlib import Path
from typing import Callable

from .._db import immediate, reader
from ..alias import AliasError, AliasRecordV1, payout_commitment
from ..bip340 import verify_hex
from ..evidence import FundingClaim, FundingEvidence, InsufficientConfirmations, LateFunding
from ..intent import (DestinationEnrollmentV1, EnrollmentAckV1, EvidencePolicyV1, PayoutAuthorizationV1,
                      PayoutIntentV1, PayoutReceiptV1, ProviderQuoteV1, QuoteRejected, ZERO32, price,
                      verify_intent)
from .ledger import PayoutLedger, Row, State
from .rails import LocalPayoutRail, RailResult, RailTimeout
from .testkeys import TestKey

CRASH_POINTS = frozenset({"after_authorize", "after_submit"})


class ProviderRefused(ValueError):
    pass


class NoInventory(ProviderRefused):
    pass


class SimulatedCrash(RuntimeError):
    """Raised at an injected crash point; the SQLite state is what survives."""


_VAULT_SCHEMA = """CREATE TABLE IF NOT EXISTS vault(
  alias_pubkey TEXT NOT NULL, commitment TEXT NOT NULL, handle TEXT NOT NULL,
  destination_ref TEXT NOT NULL UNIQUE, sealed TEXT NOT NULL,
  PRIMARY KEY(alias_pubkey, commitment))"""


def _seal(key: bytes, nonce: bytes, plaintext: bytes) -> str:
    """SIM ONLY: HMAC-SHA256 counter keystream plus HMAC tag, under a test key.
    Not reviewed encryption; a production vault needs a reviewed AEAD (spec F.6)."""
    stream = b"".join(hmac.new(key, nonce + i.to_bytes(4, "big"), sha256).digest()
                      for i in range(len(plaintext) // 32 + 1))
    body = bytes(a ^ b for a, b in zip(plaintext, stream))
    return (nonce + body + hmac.new(key, nonce + body, sha256).digest()).hex()


class ReferenceProvider:
    def __init__(self, key: TestKey, database: str | Path, rail: LocalPayoutRail, evidence: FundingEvidence,
                 policy: EvidencePolicyV1, *, rail_name: str, currency: str, exponent: int, float_minor: int,
                 rate_minor_per_btc: int, fee_bps: int, alias_lookup: Callable[[str], AliasRecordV1],
                 quote_ttl_blocks: int = 6, quote_ttl_seconds: int = 3600, min_conf: int = 1,
                 max_tip_lag: int = 2, crash_points: frozenset[str] = frozenset()):
        if not crash_points <= CRASH_POINTS:
            raise ValueError("unknown crash point")
        self.key = key
        self.database = Path(database)
        self.ledger = PayoutLedger(self.database)
        with immediate(self.database) as db:
            db.execute(_VAULT_SCHEMA)
        self.rail = rail
        self.evidence = evidence
        self.policy = policy
        self.rail_name = rail_name
        self.currency = currency
        self.exponent = exponent
        self.float_minor = float_minor
        self.rate_minor_per_btc = rate_minor_per_btc
        self.fee_bps = fee_bps
        self.alias_lookup = alias_lookup
        self.quote_ttl_blocks = quote_ttl_blocks
        self.quote_ttl_seconds = quote_ttl_seconds
        self.min_conf = min_conf
        self.max_tip_lag = max_tip_lag
        self.crash_points = crash_points
        self._vault_key = sha256(b"BAN-SIM-VAULT-KEY\x00" + key.label.encode()).digest()

    @property
    def pubkey(self) -> str:
        return self.key.pubkey

    # ------------------------------------------------------------ T0c enroll
    def enroll(self, enrollment: DestinationEnrollmentV1, sig: str, record: AliasRecordV1) -> tuple[EnrollmentAckV1, str]:
        """Accept a private destination enrollment that opens a commitment in R's
        current record naming this provider. ``record`` is P's own resolution."""
        if not verify_hex(enrollment.alias_pubkey, enrollment.digest(), sig):
            raise ProviderRefused("bad enrollment signature")
        if (record.pubkey, record.event_id, record.seq) != (enrollment.alias_pubkey, enrollment.record_id, enrollment.seq):
            raise ProviderRefused("enrollment names another record")
        if record.status != "active":
            raise ProviderRefused("alias record is revoked")
        if enrollment.provider_pubkey != self.pubkey:
            raise ProviderRefused("enrollment is for another provider")
        opened = payout_commitment(enrollment.rail, enrollment.canonical_destination, bytes.fromhex(enrollment.salt))
        if opened != enrollment.commitment:
            raise ProviderRefused("commitment does not open")
        target = record.target_for(enrollment.commitment)
        if target is None or target.rail != enrollment.rail or self.pubkey not in target.providers:
            raise ProviderRefused("commitment is not in R's record for this provider")
        destination_ref = hmac.new(self._vault_key, bytes.fromhex(enrollment.alias_pubkey + enrollment.commitment),
                                   sha256).hexdigest()
        sealed = _seal(self._vault_key, bytes.fromhex(enrollment.commitment)[:16],
                       enrollment.canonical_destination.encode("utf-8"))
        with immediate(self.database) as db:
            db.execute("INSERT OR IGNORE INTO vault VALUES(?,?,?,?,?)",
                       (enrollment.alias_pubkey, enrollment.commitment, record.name, destination_ref, sealed))
        ack = EnrollmentAckV1(enrollment.digest(), self.pubkey, "accepted")
        return ack, self.key.sign_hex(ack.digest())

    def _vault(self, alias_pubkey: str, commitment: str) -> tuple[str, str] | None:
        with reader(self.database) as db:
            return db.execute("SELECT handle,destination_ref FROM vault WHERE alias_pubkey=? AND commitment=?",
                              (alias_pubkey, commitment)).fetchone()

    # --------------------------------------------------------------- T2/T3
    def quote(self, intent: PayoutIntentV1, intent_sig: str, *, tip_height: int, now: int) -> tuple[ProviderQuoteV1, str]:
        try:
            verify_intent(intent, intent_sig)
        except QuoteRejected as exc:
            raise ProviderRefused(str(exc)) from exc
        if intent.network != self.policy.network:
            raise ProviderRefused("intent is for another network")
        if (intent.rail, intent.currency, intent.exponent) != (self.rail_name, self.currency, self.exponent):
            raise ProviderRefused("unsupported rail or currency")
        if now >= intent.expiry_unix:
            raise ProviderRefused("intent expired")
        enrolled = self._vault(intent.alias_pubkey, intent.recipient_commitment)
        if enrolled is None:
            raise ProviderRefused("no enrollment for this recipient commitment")
        handle = enrolled[0]
        self._current_record(handle, intent.alias_pubkey, intent.recipient_commitment, intent.alias_seq)
        existing = self.ledger.current_quote(intent.intent_id())
        if existing is not None:
            live = now < existing.expiry_unix and tip_height < existing.expiry_height
            if existing.state is State.QUOTED and not live:
                self.ledger.transition(existing.quote_id, State.EXPIRED, now=now, tip_height=tip_height)
            elif existing.state is not State.EXPIRED:
                # Same intent_id, same quote (T2). Once funding is seen the intent never gets a second quote.
                return existing.quote, existing.quote_sig
        pricing = price(intent.amount_mode, intent.amount, self.rate_minor_per_btc, self.fee_bps)
        if pricing.net_payout_minor > self.float_minor - self.ledger.held_minor():
            raise NoInventory("not enough unreserved float; no quote is issued on unreserved inventory")
        n = self.ledger.quote_count()
        deposit_key = TestKey("%s/deposit/%d" % (self.key.label, n))
        quote = ProviderQuoteV1(
            network=intent.network, intent_id=intent.intent_id(), provider_pubkey=self.pubkey,
            reservation_id=sha256(b"CAG-RESERVATION-V1\x00" + bytes.fromhex(intent.intent_id())
                                  + n.to_bytes(8, "big")).hexdigest(),
            deposit_script="5120" + deposit_key.pubkey, sats_required=pricing.sats_required,
            overpay_policy="refund_excess", underpay_policy="refund_minus_fee",
            rate_minor_per_btc=self.rate_minor_per_btc, currency=self.currency, exponent=self.exponent,
            fee_sats=pricing.fee_sats, fee_minor=pricing.fee_minor, net_payout_minor=pricing.net_payout_minor,
            quote_created_height=tip_height, quote_expiry_height=tip_height + self.quote_ttl_blocks,
            quote_expiry_unix=min(now + self.quote_ttl_seconds, intent.expiry_unix), min_conf=self.min_conf,
            max_tip_lag=self.max_tip_lag, evidence_policy_digest=self.policy.digest())
        sig = self.key.sign_hex(quote.quote_id())
        self.ledger.record_quote(quote, sig, intent, handle=handle, reserved_minor=pricing.net_payout_minor, now=now)
        return quote, sig

    def _current_record(self, handle: str, alias_pubkey: str, commitment: str, min_seq: int) -> AliasRecordV1:
        """P's fresh view of R's record; any doubt is an AliasError or ProviderRefused."""
        record = self.alias_lookup(handle)
        if record.pubkey != alias_pubkey:
            raise ProviderRefused("alias key changed")
        if record.seq < min_seq:
            raise ProviderRefused("alias record older than the intent's")
        target = record.target_for(commitment)
        if target is None or self.pubkey not in target.providers:
            raise ProviderRefused("commitment no longer in R's record for this provider")
        return record

    # ------------------------------------------------------------- T4/T5
    def on_funding(self, quote_id: str, *, now: int, claim: FundingClaim | None = None) -> State:
        row = self.ledger.get(quote_id)
        if row.state not in (State.QUOTED, State.EXPIRED, State.FUNDING_SEEN):
            return row.state
        quote = row.quote
        # Once the expiry sweep released the reservation, any funding is late: never pay at the stale rate.
        expired = row.state is State.EXPIRED or any(to == State.EXPIRED.value for _, to in self.ledger.history(quote_id))
        try:
            acceptance = self.evidence.accept(quote, now=now, claim=claim)
        except LateFunding as exc:
            self._seen(row, exc.outpoint, exc.block_height, now)
            return self.ledger.transition(quote_id, State.LATE_FUNDING, now=now, reason=str(exc)).state
        except InsufficientConfirmations as exc:
            seen = self._seen(row, exc.outpoint, exc.block_height, now)
            if expired:
                return self.ledger.transition(quote_id, State.LATE_FUNDING, now=now, reason="quote already expired").state
            return seen.state
        if expired:
            self._seen(row, acceptance.outpoint, acceptance.block_height, now)
            return self.ledger.transition(quote_id, State.LATE_FUNDING, now=now, reason="quote already expired").state
        self._seen(row, acceptance.outpoint, acceptance.block_height, now)
        return self.ledger.transition(quote_id, State.FUNDING_ACCEPTED, now=now,
                                      acceptance_digest=acceptance.digest(),
                                      confirmations=acceptance.confirmations).state

    def _seen(self, row: Row, outpoint: str | None, height: int | None, now: int) -> Row:
        if row.state is State.FUNDING_SEEN:
            if row.outpoint != outpoint:
                raise ProviderRefused("a different output already funds this quote")
            return row
        return self.ledger.transition(row.quote_id, State.FUNDING_SEEN, now=now, outpoint=outpoint,
                                      funding_height=height)

    # ------------------------------------------------------------ T6/T7
    def authorize(self, quote_id: str, *, now: int) -> State:
        row = self.ledger.get(quote_id)
        if row.state not in (State.FUNDING_ACCEPTED, State.HOLD):
            existing = self.ledger.authorization(quote_id)
            if existing is not None:
                return row.state  # duplicate authorization: nothing new is written or sent
            raise ProviderRefused("cannot authorize from " + row.state.value)
        try:
            record = self._current_record(row.handle, row.alias_pubkey, row.commitment, row.alias_seq)
            if record.status != "active":
                raise ProviderRefused("alias revoked")
        except (AliasError, ProviderRefused) as exc:
            if row.state is State.HOLD:
                return row.state
            return self.ledger.transition(quote_id, State.HOLD, now=now, reason=str(exc)).state
        destination_ref = self._vault(row.alias_pubkey, row.commitment)[1]
        auth = PayoutAuthorizationV1(quote_id, row.acceptance_digest, destination_ref, row.net_payout_minor,
                                     row.currency, row.exponent, row.idempotency_key, record.seq)
        updated, _ = self.ledger.authorize(auth, now=now)
        return updated.state

    def submit(self, quote_id: str, *, now: int) -> State:
        row = self.ledger.get(quote_id)
        if row.state is not State.PAYOUT_AUTHORIZED:
            return row.state  # already submitted or not authorized: never send twice
        auth = self.ledger.authorization(quote_id)
        try:  # re-check: an authorization can outlive a crash, and the alias can change meanwhile (I8)
            record = self._current_record(row.handle, row.alias_pubkey, row.commitment, auth.alias_seq)
            if record.status != "active":
                raise ProviderRefused("alias revoked")
        except (AliasError, ProviderRefused) as exc:
            return self.ledger.transition(quote_id, State.HOLD, now=now, reason=str(exc)).state
        request = sha256(b"CAG-RAIL-REQUEST-V1\x00" + auth.encode()).hexdigest()
        self.ledger.begin_attempt(quote_id, request, now=now)  # write-ahead, before the rail call
        try:
            result = self.rail.submit(row.idempotency_key, auth.destination_ref, row.net_payout_minor, row.currency)
        except RailTimeout as exc:
            return self.ledger.transition(quote_id, State.PAYOUT_UNKNOWN, now=now, reason=str(exc)).state
        if "after_submit" in self.crash_points:
            raise SimulatedCrash("after rail submit, before recording the response")
        return self._apply(row, result, now)

    def pay(self, quote_id: str, *, now: int) -> State:
        state = self.authorize(quote_id, now=now)
        if state is not State.PAYOUT_AUTHORIZED:
            return state
        if "after_authorize" in self.crash_points:
            raise SimulatedCrash("after authorization, before the rail call")
        return self.submit(quote_id, now=now)

    def _apply(self, row: Row, result: RailResult, now: int) -> State:
        evidence = sha256(result.evidence).hexdigest() if result.evidence is not None else None
        self.ledger.record_response(row.quote_id, result.status, result.reference, evidence, now=now)
        if result.status == "delivered":
            return self.ledger.transition(row.quote_id, State.PAYOUT_CONFIRMED, now=now, rail_status="delivered").state
        if result.status == "failed":
            self.ledger.transition(row.quote_id, State.PAYOUT_FAILED, now=now, rail_status="failed")
            return self.ledger.transition(row.quote_id, State.REFUND_DUE, now=now, reason="rail failure").state
        return self.ledger.get(row.quote_id).state  # pending / not_found: no change

    def poll(self, quote_id: str, *, now: int) -> State:
        """Resolve SUBMITTED or UNKNOWN only from the rail's status query."""
        row = self.ledger.get(quote_id)
        if row.state not in (State.PAYOUT_SUBMITTED, State.PAYOUT_UNKNOWN):
            return row.state
        return self._apply(row, self.rail.status(row.idempotency_key), now)

    def on_webhook(self, event: dict, *, now: int) -> State | None:
        """Webhooks are unauthenticated triggers: re-query status, never trust the body.
        Unknown references and duplicates are no-ops."""
        reference = event.get("reference") if isinstance(event, dict) else None
        row = self.ledger.by_idempotency_key(reference) if isinstance(reference, str) else None
        if row is None:
            return None
        if row.state not in (State.PAYOUT_SUBMITTED, State.PAYOUT_UNKNOWN):
            return row.state
        return self.poll(row.quote_id, now=now)

    # ---------------------------------------------------------------- T8
    def issue_receipt(self, quote_id: str, *, now: int) -> tuple[PayoutReceiptV1, str]:
        row = self.ledger.get(quote_id)
        if row.state is not State.PAYOUT_CONFIRMED:
            raise ProviderRefused("receipts are issued only for confirmed payouts")
        attempt = self.ledger.attempt(row.idempotency_key)
        has_evidence = attempt["evidence_sha256"] is not None
        receipt = PayoutReceiptV1(
            quote_id, row.intent_id, row.idempotency_key, self.rail_name, attempt["rail_reference"] or "",
            row.net_payout_minor, row.currency, row.exponent, "delivered",
            "api_status" if has_evidence else "provider_attested_only",
            attempt["evidence_sha256"] if has_evidence else ZERO32, now)
        sig = self.key.sign_hex(receipt.digest())
        self.ledger.store_receipt(quote_id, receipt.encode().hex(), sig, now=now)
        return receipt, sig

    # -------------------------------------------------------- T9 and upkeep
    def reconcile(self, statement: dict[str, tuple[str, int]], *, now: int) -> dict[str, str]:
        """``statement`` maps idempotency key -> (status, amount_minor) from the rail."""
        out = {}
        for row in self.ledger.in_state(State.RECEIPTED):
            line = statement.get(row.idempotency_key)
            to = State.RECONCILED if line == ("delivered", row.net_payout_minor) else State.DISPUTED
            out[row.quote_id] = self.ledger.transition(row.quote_id, to, now=now, statement=str(line)).state.value
        return out

    def expire(self, *, now: int, tip_height: int) -> list[str]:
        expired = []
        for row in self.ledger.in_state(State.QUOTED):
            if now >= row.expiry_unix or tip_height >= row.expiry_height:
                self.ledger.transition(row.quote_id, State.EXPIRED, now=now, tip_height=tip_height)
                expired.append(row.quote_id)
        return expired

    def recover(self, *, now: int) -> dict[str, str]:
        """After a restart: an authorized-but-unsent row is sent once with the same
        key; a sent row with no recorded response becomes UNKNOWN (spec B.5)."""
        out = {}
        for row in self.ledger.in_state(State.PAYOUT_AUTHORIZED):
            out[row.quote_id] = self.submit(row.quote_id, now=now).value
        for row in self.ledger.in_state(State.PAYOUT_SUBMITTED):
            attempt = self.ledger.attempt(row.idempotency_key)
            if attempt is not None and attempt["response_status"] is None:
                out[row.quote_id] = self.ledger.transition(row.quote_id, State.PAYOUT_UNKNOWN, now=now,
                                                           reason="restart without a recorded response").state.value
        return out
