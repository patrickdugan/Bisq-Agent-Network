"""Durable payout ledger for the simulated reference provider (spec B.4).

SQLite, one ``BEGIN IMMEDIATE`` transaction per change. The schema enforces the
replay invariants: ``UNIQUE(outpoint)`` (I2), ``UNIQUE(deposit_script_sha256)``
(I3), one authorization row per quote (I1) and one rail attempt per
idempotency key, written before the rail call.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import sqlite3

from .._db import immediate, reader
from ..intent import PayoutAuthorizationV1, PayoutIntentV1, ProviderQuoteV1


class State(str, Enum):
    INTENT_RECEIVED = "INTENT_RECEIVED"
    QUOTED = "QUOTED"
    EXPIRED = "EXPIRED"
    FUNDING_SEEN = "FUNDING_SEEN"
    FUNDING_ACCEPTED = "FUNDING_ACCEPTED"
    LATE_FUNDING = "LATE_FUNDING"
    REQUOTED = "REQUOTED"
    PAYOUT_AUTHORIZED = "PAYOUT_AUTHORIZED"
    PAYOUT_SUBMITTED = "PAYOUT_SUBMITTED"
    PAYOUT_CONFIRMED = "PAYOUT_CONFIRMED"
    PAYOUT_FAILED = "PAYOUT_FAILED"
    PAYOUT_UNKNOWN = "PAYOUT_UNKNOWN"
    RECEIPTED = "RECEIPTED"
    RECONCILED = "RECONCILED"
    REFUND_DUE = "REFUND_DUE"
    REFUNDED = "REFUNDED"
    HOLD = "HOLD"
    DISPUTED = "DISPUTED"


S = State
ALLOWED: dict[State, frozenset[State]] = {
    S.INTENT_RECEIVED: frozenset({S.QUOTED}),
    S.QUOTED: frozenset({S.EXPIRED, S.FUNDING_SEEN}),
    # Deviation: B.4 has no edge for funding seen after the expiry sweep; it is late funding.
    S.EXPIRED: frozenset({S.FUNDING_SEEN}),
    S.FUNDING_SEEN: frozenset({S.FUNDING_ACCEPTED, S.LATE_FUNDING}),
    S.FUNDING_ACCEPTED: frozenset({S.PAYOUT_AUTHORIZED, S.HOLD, S.FUNDING_SEEN}),
    S.PAYOUT_AUTHORIZED: frozenset({S.PAYOUT_SUBMITTED, S.HOLD}),
    S.PAYOUT_SUBMITTED: frozenset({S.PAYOUT_CONFIRMED, S.PAYOUT_FAILED, S.PAYOUT_UNKNOWN}),
    S.PAYOUT_UNKNOWN: frozenset({S.PAYOUT_CONFIRMED, S.PAYOUT_FAILED}),
    S.PAYOUT_CONFIRMED: frozenset({S.RECEIPTED}),
    S.RECEIPTED: frozenset({S.RECONCILED}),
    S.PAYOUT_FAILED: frozenset({S.REFUND_DUE}),
    S.REFUND_DUE: frozenset({S.REFUNDED}),
    S.LATE_FUNDING: frozenset({S.REFUND_DUE, S.REQUOTED}),
    S.HOLD: frozenset({S.PAYOUT_AUTHORIZED, S.REFUND_DUE}),
}
RELEASES_RESERVATION = frozenset({S.EXPIRED, S.REFUNDED, S.RECONCILED})  # I5


class LedgerError(ValueError):
    pass


class IllegalTransition(LedgerError):
    pass


class ReplayRejected(LedgerError):
    pass


class InvariantViolation(LedgerError):
    pass


class DuplicatePayout(LedgerError):
    pass


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS intents(
      intent_id TEXT PRIMARY KEY, intent_hex TEXT NOT NULL, rail TEXT NOT NULL,
      state TEXT NOT NULL, current_quote_id TEXT, received_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS quotes(
      quote_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(intent_id),
      state TEXT NOT NULL,
      deposit_script_sha256 TEXT NOT NULL UNIQUE,
      outpoint TEXT UNIQUE, funding_height INTEGER,
      reservation_id TEXT NOT NULL UNIQUE, reserved_minor INTEGER NOT NULL,
      reservation_held INTEGER NOT NULL CHECK(reservation_held IN (0, 1)),
      net_payout_minor INTEGER NOT NULL, fee_minor INTEGER NOT NULL, fee_sats INTEGER NOT NULL,
      sats_required INTEGER NOT NULL, currency TEXT NOT NULL, exponent INTEGER NOT NULL,
      idempotency_key TEXT NOT NULL UNIQUE,
      created_height INTEGER NOT NULL, expiry_height INTEGER NOT NULL, expiry_unix INTEGER NOT NULL,
      handle TEXT NOT NULL, alias_pubkey TEXT NOT NULL, alias_seq INTEGER NOT NULL,
      commitment TEXT NOT NULL, quote_hex TEXT NOT NULL, quote_sig TEXT NOT NULL,
      acceptance_digest TEXT, paid_minor INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS authorizations(
      quote_id TEXT PRIMARY KEY REFERENCES quotes(quote_id),
      idempotency_key TEXT NOT NULL UNIQUE, auth_hex TEXT NOT NULL, alias_seq INTEGER NOT NULL,
      authorized_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS attempts(
      idempotency_key TEXT PRIMARY KEY REFERENCES authorizations(idempotency_key),
      request_digest TEXT NOT NULL, submitted_at INTEGER NOT NULL,
      response_status TEXT, rail_reference TEXT, evidence_sha256 TEXT, responded_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS transitions(
      n INTEGER PRIMARY KEY AUTOINCREMENT, quote_id TEXT NOT NULL, from_state TEXT NOT NULL,
      to_state TEXT NOT NULL, evidence TEXT NOT NULL, at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS receipts(
      quote_id TEXT PRIMARY KEY REFERENCES quotes(quote_id), receipt_hex TEXT NOT NULL, sig TEXT NOT NULL)""",
)

_ROW = ("quote_id,intent_id,state,deposit_script_sha256,outpoint,funding_height,reservation_id,reserved_minor,"
        "reservation_held,net_payout_minor,fee_minor,fee_sats,sats_required,currency,exponent,idempotency_key,"
        "created_height,expiry_height,expiry_unix,handle,alias_pubkey,alias_seq,commitment,quote_hex,quote_sig,"
        "acceptance_digest")


@dataclass(frozen=True)
class Row:
    quote_id: str
    intent_id: str
    state: State
    deposit_script_sha256: str
    outpoint: str | None
    funding_height: int | None
    reservation_id: str
    reserved_minor: int
    reservation_held: bool
    net_payout_minor: int
    fee_minor: int
    fee_sats: int
    sats_required: int
    currency: str
    exponent: int
    idempotency_key: str
    created_height: int
    expiry_height: int
    expiry_unix: int
    handle: str
    alias_pubkey: str
    alias_seq: int
    commitment: str
    quote_hex: str
    quote_sig: str
    acceptance_digest: str | None

    @property
    def quote(self) -> ProviderQuoteV1:
        return ProviderQuoteV1.decode(bytes.fromhex(self.quote_hex))


def _row(values: tuple) -> Row:
    data = list(values)
    data[2] = State(data[2])
    data[8] = bool(data[8])
    return Row(*data)


class PayoutLedger:
    def __init__(self, database: str | Path):
        self.database = Path(database)
        with immediate(self.database) as db:
            for stmt in _SCHEMA:
                db.execute(stmt)

    # -- reads
    def get(self, quote_id: str) -> Row:
        with reader(self.database) as db:
            values = db.execute("SELECT " + _ROW + " FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
        if values is None:
            raise LedgerError("unknown quote")
        return _row(values)

    def by_idempotency_key(self, key: str) -> Row | None:
        with reader(self.database) as db:
            values = db.execute("SELECT " + _ROW + " FROM quotes WHERE idempotency_key=?", (key,)).fetchone()
        return _row(values) if values else None

    def in_state(self, *states: State) -> list[Row]:
        marks = ",".join("?" for _ in states)
        with reader(self.database) as db:
            rows = db.execute("SELECT " + _ROW + " FROM quotes WHERE state IN (" + marks + ") ORDER BY quote_id",
                              [s.value for s in states]).fetchall()
        return [_row(v) for v in rows]

    def all_rows(self) -> list[Row]:
        return self.in_state(*State)

    def current_quote(self, intent_id: str) -> Row | None:
        with reader(self.database) as db:
            found = db.execute("SELECT current_quote_id FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        return self.get(found[0]) if found and found[0] else None

    def held_minor(self) -> int:
        with reader(self.database) as db:
            return db.execute("SELECT COALESCE(SUM(reserved_minor),0) FROM quotes WHERE reservation_held=1").fetchone()[0]

    def committed_minor(self) -> int:
        """Float that is reserved or already paid out. A reconciled payout releases its
        reservation, but the money left the float, so it stays committed."""
        with reader(self.database) as db:
            return db.execute("SELECT COALESCE(SUM(MAX(CASE WHEN reservation_held=1 THEN reserved_minor ELSE 0 END,"
                              " paid_minor)),0) FROM quotes").fetchone()[0]

    def quote_count(self) -> int:
        with reader(self.database) as db:
            return db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]

    def attempt(self, idempotency_key: str) -> dict | None:
        with reader(self.database) as db:
            row = db.execute("SELECT request_digest,response_status,rail_reference,evidence_sha256 FROM attempts "
                             "WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row is None:
            return None
        return dict(zip(("request_digest", "response_status", "rail_reference", "evidence_sha256"), row))

    def authorization(self, quote_id: str) -> PayoutAuthorizationV1 | None:
        with reader(self.database) as db:
            row = db.execute("SELECT auth_hex FROM authorizations WHERE quote_id=?", (quote_id,)).fetchone()
        return _decode_auth(bytes.fromhex(row[0])) if row else None

    def history(self, quote_id: str) -> list[tuple[str, str]]:
        with reader(self.database) as db:
            return db.execute("SELECT from_state,to_state FROM transitions WHERE quote_id=? ORDER BY n",
                              (quote_id,)).fetchall()

    # -- writes
    def record_quote(self, quote: ProviderQuoteV1, sig: str, intent: PayoutIntentV1, *, handle: str,
                     reserved_minor: int, now: int) -> Row:
        quote_id = quote.quote_id()
        try:
            with immediate(self.database) as db:
                existing = db.execute("SELECT state FROM intents WHERE intent_id=?", (quote.intent_id,)).fetchone()
                if existing is None:
                    db.execute("INSERT INTO intents VALUES(?,?,?,?,NULL,?)",
                               (quote.intent_id, intent.encode().hex(), intent.rail, S.INTENT_RECEIVED.value, now))
                db.execute("INSERT INTO quotes(" + _ROW + ",updated_at) VALUES(" + ",".join("?" * 27) + ")", (
                    quote_id, quote.intent_id, S.QUOTED.value, quote.deposit_script_sha256, None, None,
                    quote.reservation_id, reserved_minor, 1, quote.net_payout_minor, quote.fee_minor,
                    quote.fee_sats, quote.sats_required, quote.currency, quote.exponent, quote.idempotency_key(),
                    quote.quote_created_height, quote.quote_expiry_height, quote.quote_expiry_unix, handle,
                    intent.alias_pubkey, intent.alias_seq, intent.recipient_commitment, quote.encode().hex(),
                    sig, None, now))
                db.execute("UPDATE intents SET state=?,current_quote_id=? WHERE intent_id=?",
                           (S.QUOTED.value, quote_id, quote.intent_id))
                db.execute("INSERT INTO transitions(quote_id,from_state,to_state,evidence,at) VALUES(?,?,?,?,?)",
                           (quote_id, S.INTENT_RECEIVED.value, S.QUOTED.value, "{}", now))
        except sqlite3.IntegrityError as exc:
            if "deposit_script_sha256" in str(exc):
                raise ReplayRejected("deposit script already belongs to another quote (I3)") from exc
            raise ReplayRejected("quote conflicts with an existing row: " + str(exc)) from exc
        return self.get(quote_id)

    def transition(self, quote_id: str, to: State, *, now: int, **evidence) -> Row:
        """Move one quote along an allowed B.4 edge, persisting the evidence."""
        to = State(to)
        try:
            with immediate(self.database) as db:
                self._transition(db, quote_id, to, now, evidence)
        except sqlite3.IntegrityError as exc:
            if "outpoint" in str(exc):
                raise ReplayRejected("funding outpoint already bound to another quote (I2)") from exc
            raise
        return self.get(quote_id)

    def _transition(self, db: sqlite3.Connection, quote_id: str, to: State, now: int, evidence: dict) -> None:
        row = db.execute("SELECT state,created_height,reservation_held FROM quotes WHERE quote_id=?",
                         (quote_id,)).fetchone()
        if row is None:
            raise LedgerError("unknown quote")
        current = State(row[0])
        if to is not S.DISPUTED and to not in ALLOWED.get(current, frozenset()):
            raise IllegalTransition("%s -> %s is not allowed" % (current.value, to.value))
        if to is S.FUNDING_ACCEPTED and not row[2]:
            raise InvariantViolation("funding cannot be accepted after the reservation was released (I5)")
        if current is S.PAYOUT_UNKNOWN and not evidence.get("rail_status"):
            raise InvariantViolation("PAYOUT_UNKNOWN leaves only on an observed rail status (I4)")
        if to is S.PAYOUT_AUTHORIZED and db.execute(
                "SELECT 1 FROM authorizations WHERE quote_id=?", (quote_id,)).fetchone() is None:
            raise InvariantViolation("PAYOUT_AUTHORIZED requires a written authorization")
        updates, params = ["state=?", "updated_at=?"], [to.value, now]
        if to is S.FUNDING_SEEN and "outpoint" in evidence:
            if evidence["funding_height"] < row[1]:
                raise InvariantViolation("funding below quote_created_height (I7)")
            updates += ["outpoint=?", "funding_height=?"]
            params += [evidence["outpoint"], evidence["funding_height"]]
        if to is S.FUNDING_ACCEPTED:
            updates.append("acceptance_digest=?")
            params.append(evidence["acceptance_digest"])
        if to is S.PAYOUT_CONFIRMED:
            updates.append("paid_minor=net_payout_minor")
        if to in RELEASES_RESERVATION:
            updates.append("reservation_held=0")
        db.execute("UPDATE quotes SET " + ",".join(updates) + " WHERE quote_id=?", params + [quote_id])
        db.execute("INSERT INTO transitions(quote_id,from_state,to_state,evidence,at) VALUES(?,?,?,?,?)",
                   (quote_id, current.value, to.value, json.dumps(evidence, sort_keys=True, default=str), now))

    def authorize(self, auth: PayoutAuthorizationV1, *, now: int) -> tuple[Row, bool]:
        """Write-ahead authorization. Returns ``(row, created)``; a repeat for the
        same quote returns the existing authorization and creates nothing (I1)."""
        with immediate(self.database) as db:
            values = db.execute("SELECT " + _ROW + " FROM quotes WHERE quote_id=?", (auth.quote_id,)).fetchone()
            if values is None:
                raise LedgerError("unknown quote")
            row = _row(values)
            existing = db.execute("SELECT idempotency_key FROM authorizations WHERE quote_id=?",
                                  (auth.quote_id,)).fetchone()
            if existing is not None and row.state is not S.HOLD:
                if existing[0] != auth.idempotency_key:
                    raise InvariantViolation("second authorization with another key (I1)")
                return row, False
            if row.state not in (S.FUNDING_ACCEPTED, S.HOLD):
                raise IllegalTransition("cannot authorize from " + row.state.value)
            if (auth.net_payout_minor, auth.currency, auth.exponent) != (row.net_payout_minor, row.currency, row.exponent):
                raise InvariantViolation("payout amount and currency are fixed at the quote (I6)")
            if auth.idempotency_key != row.idempotency_key or auth.funding_acceptance_digest != row.acceptance_digest:
                raise InvariantViolation("authorization does not bind this quote's key and funding")
            if auth.alias_seq < row.alias_seq:
                raise InvariantViolation("alias seq at authorization is below the quote's (I8)")
            if existing is None:
                db.execute("INSERT INTO authorizations VALUES(?,?,?,?,?)",
                           (auth.quote_id, auth.idempotency_key, auth.encode().hex(), auth.alias_seq, now))
            self._transition(db, auth.quote_id, S.PAYOUT_AUTHORIZED, now, {"authorization": auth.digest()})
        return self.get(auth.quote_id), existing is None

    def begin_attempt(self, quote_id: str, request_digest: str, *, now: int) -> Row:
        """Persist the one rail attempt for this idempotency key, then mark SUBMITTED."""
        try:
            with immediate(self.database) as db:
                state, key = db.execute("SELECT state,idempotency_key FROM quotes WHERE quote_id=?",
                                        (quote_id,)).fetchone()
                if State(state) is not S.PAYOUT_AUTHORIZED:
                    raise IllegalTransition("submit requires PAYOUT_AUTHORIZED, not " + state)
                db.execute("INSERT INTO attempts(idempotency_key,request_digest,submitted_at) VALUES(?,?,?)",
                           (key, request_digest, now))
                self._transition(db, quote_id, S.PAYOUT_SUBMITTED, now, {"request_digest": request_digest})
        except sqlite3.IntegrityError as exc:
            raise DuplicatePayout("a rail attempt already exists for this idempotency key") from exc
        return self.get(quote_id)

    def record_response(self, quote_id: str, status: str, reference: str, evidence_sha256: str | None,
                        *, now: int) -> None:
        with immediate(self.database) as db:
            key = db.execute("SELECT idempotency_key FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()[0]
            db.execute("UPDATE attempts SET response_status=?,rail_reference=?,evidence_sha256=?,responded_at=? "
                       "WHERE idempotency_key=?", (status, reference, evidence_sha256, now, key))

    def store_receipt(self, quote_id: str, receipt_hex: str, sig: str, *, now: int) -> Row:
        with immediate(self.database) as db:
            db.execute("INSERT OR REPLACE INTO receipts VALUES(?,?,?)", (quote_id, receipt_hex, sig))
            self._transition(db, quote_id, S.RECEIPTED, now, {"receipt_sig": sig[:16]})
        return self.get(quote_id)


def _decode_auth(data: bytes) -> PayoutAuthorizationV1:
    pos = 1
    quote_id, acceptance, destination = (data[pos:pos + 32].hex(), data[pos + 32:pos + 64].hex(),
                                         data[pos + 64:pos + 96].hex())
    pos += 96
    net = int.from_bytes(data[pos:pos + 8], "big")
    currency, exponent = data[pos + 8:pos + 11].decode("ascii"), data[pos + 11]
    key = data[pos + 12:pos + 44].hex()
    seq = int.from_bytes(data[pos + 44:pos + 48], "big")
    auth = PayoutAuthorizationV1(quote_id, acceptance, destination, net, currency, exponent, key, seq)
    if auth.encode() != data:
        raise LedgerError("corrupt authorization row")
    return auth
