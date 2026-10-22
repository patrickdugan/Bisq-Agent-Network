"""Opt-in local merchant cohort. No contact details, payment accounts or trade secrets."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import sqlite3
from uuid import uuid4

from .models import currency_code

ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,31}\Z")
RAIL = re.compile(r"[A-Za-z][A-Za-z0-9 _-]{1,39}\Z")
IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
CHANNELS = frozenset({"direct", "community", "merchant_referral", "wallet_partner", "event", "other"})
STATES = frozenset({"onboarded", "active", "paused"})
CONSENT_DAYS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohort_merchants (
    merchant_id TEXT PRIMARY KEY,
    alias TEXT NOT NULL COLLATE NOCASE,
    currency TEXT NOT NULL,
    methods_json TEXT NOT NULL,
    channel TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('onboarded', 'active', 'paused')),
    consent_at TEXT NOT NULL,
    consent_until TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(currency, alias)
);
"""


class CohortError(ValueError):
    pass


class _ClosingConnection(sqlite3.Connection):
    """Commit or rollback via context manager, then close the connection."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db_path(db_path: str | Path) -> Path:
    path = Path(db_path).expanduser()
    if path.is_dir():
        raise CohortError("database path must be a file")
    return path


def _open(db_path: str | Path) -> sqlite3.Connection:
    path = _db_path(db_path)
    # Create a private file atomically when it does not already exist.
    # On Windows, local filesystem ACLs remain the operating system's responsibility.
    try:
        fd = path.open("xb")
    except FileExistsError:
        pass
    else:
        fd.close()
        try:
            path.chmod(0o600)
        except OSError:
            pass
    db = sqlite3.connect(path, timeout=5, factory=_ClosingConnection)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute(SCHEMA)
    return db


def _get_existing(db_path: str | Path) -> sqlite3.Connection | None:
    path = _db_path(db_path)
    if not path.is_file():
        return None
    return sqlite3.connect(path, timeout=5, factory=_ClosingConnection)


def _methods(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(values) <= 8:
        raise CohortError("provide 1 to 8 payment rail labels")
    result: list[str] = []
    seen = set()
    for value in values:
        label = value.strip()
        if not RAIL.fullmatch(label) or re.search(r"\d{5,}", label):
            raise CohortError("payment methods must be generic rail labels, without account details")
        folded = label.casefold()
        if folded in seen:
            raise CohortError("payment methods must be unique")
        seen.add(folded)
        result.append(label)
    return tuple(result)


def _id(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise CohortError("merchant ID must be 32 lowercase hexadecimal characters")
    return value


def _serialize(row: tuple) -> dict:
    merchant_id, alias, currency, methods_json, channel, state, consent_at, consent_until, updated_at = row
    return {
        "merchant_id": merchant_id,
        "alias": alias,
        "currency": currency,
        "payment_methods": json.loads(methods_json),
        "acquisition_channel": channel,
        "state": state,
        "state_evidence": "operator_reported",
        "consent_record": "operator_attested",
        "consent_at": consent_at,
        "consent_until": consent_until,
        "updated_at": updated_at,
        "visibility": "local_only",
    }


def enroll(
    db_path: str | Path,
    *,
    alias: str,
    currency: str,
    payment_methods: list[str] | tuple[str, ...],
    channel: str,
    consent_attested: bool,
) -> dict:
    if consent_attested is not True:
        raise CohortError("explicit operator attestation of merchant consent is required")
    name = alias.strip()
    if not ALIAS.fullmatch(name):
        raise CohortError("use a pseudonymous merchant alias with 3-32 letters, numbers, _ or -")
    code = currency_code(currency)
    methods = _methods(payment_methods)
    if channel not in CHANNELS:
        raise CohortError("invalid acquisition channel")
    now = _now()
    now_iso = now.isoformat()
    expires = (now + timedelta(days=CONSENT_DAYS)).isoformat()
    uid = uuid4().hex
    try:
        with _open(db_path) as db:
            db.execute(
                "INSERT INTO cohort_merchants "
                "(merchant_id,alias,currency,methods_json,channel,state,consent_at,consent_until,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, name, code, json.dumps(methods), channel, "onboarded", now_iso, expires, now_iso),
            )
    except sqlite3.IntegrityError:
        raise CohortError("merchant alias already enrolled for this currency") from None
    return {
        "merchant_id": uid,
        "alias": name,
        "currency": code,
        "payment_methods": list(methods),
        "acquisition_channel": channel,
        "state": "onboarded",
        "consent_record": "operator_attested",
        "consent_until": expires,
        "visibility": "local_only",
    }


def list_merchants(db_path: str | Path, currency: str | None = None) -> dict:
    code = currency_code(currency) if currency is not None else None
    now = _now().isoformat()
    db = _get_existing(db_path)
    if db is None:
        rows = []
    else:
        with db:
            # A pre-v0.3 event ledger may exist without a cohort table.
            installed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cohort_merchants'"
            ).fetchone()
            if not installed:
                rows = []
            else:
                query = ("SELECT merchant_id,alias,currency,methods_json,channel,state,"
                         "consent_at,consent_until,updated_at FROM cohort_merchants WHERE consent_until > ?")
                params: tuple = (now,)
                if code:
                    query += " AND currency = ?"
                    params += (code,)
                query += " ORDER BY consent_at, merchant_id LIMIT 1000"
                rows = db.execute(query, params).fetchall()
    return {
        "currency": code,
        "total": len(rows),
        "merchants": [_serialize(row) for row in rows],
        "storage": "local_sqlite",
        "provenance": "operator_attested_not_independently_verified",
        "visibility": "local_only",
    }


def update_status(
    db_path: str | Path, *,
    merchant_id: str,
    state: str,
    availability_attested: bool = False,
) -> dict:
    uid = _id(merchant_id)
    if state not in STATES:
        raise CohortError("invalid merchant state")
    if state == "active" and availability_attested is not True:
        raise CohortError("active status requires explicit operator attestation of merchant availability")
    now = _now().isoformat()
    with _open(db_path) as db:
        cursor = db.execute(
            "UPDATE cohort_merchants SET state=?,updated_at=? "
            "WHERE merchant_id=? AND consent_until>?",
            (state, now, uid, now),
        )
        if cursor.rowcount != 1:
            raise CohortError("merchant not found or consent expired")
    return {
        "merchant_id": uid,
        "state": state,
        "state_evidence": "operator_attested",
        "visibility": "local_only",
    }


def withdraw(db_path: str | Path, *, merchant_id: str, request_attested: bool) -> dict:
    if request_attested is not True:
        raise CohortError("merchant withdrawal request must be explicitly attested")
    uid = _id(merchant_id)
    # Delete the entire record, including acquisition source and historical alias.
    with _open(db_path) as db:
        cursor = db.execute("DELETE FROM cohort_merchants WHERE merchant_id=?", (uid,))
        if cursor.rowcount != 1:
            raise CohortError("merchant not found")
    return {"deleted": True, "merchant_id": uid, "visibility": "local_only"}


def renew_consent(db_path: str | Path, *, merchant_id: str, consent_attested: bool) -> dict:
    if consent_attested is not True:
        raise CohortError("new explicit merchant consent attestation is required")
    uid = _id(merchant_id)
    now = _now()
    expires = (now + timedelta(days=CONSENT_DAYS)).isoformat()
    with _open(db_path) as db:
        cursor = db.execute(
            "UPDATE cohort_merchants SET consent_at=?,consent_until=?,updated_at=? WHERE merchant_id=?",
            (now.isoformat(), expires, now.isoformat(), uid),
        )
        if cursor.rowcount != 1:
            raise CohortError("merchant not found")
    return {"merchant_id": uid, "consent_until": expires, "consent_record": "operator_attested"}


def purge_expired(db_path: str | Path) -> dict:
    with _open(db_path) as db:
        cursor = db.execute(
            "DELETE FROM cohort_merchants WHERE consent_until <= ?", (_now().isoformat(),)
        )
    return {"expired_profiles_deleted": cursor.rowcount}


def cohort_report(db_path: str | Path, currency: str | None = None) -> dict:
    view = list_merchants(db_path, currency)
    merchants = view["merchants"]
    states = Counter(m["state"] for m in merchants)
    channels = Counter(m["acquisition_channel"] for m in merchants)
    rails = Counter(method for m in merchants for method in m["payment_methods"])
    return {
        "currency": view["currency"],
        "enrolled": len(merchants),
        "active_self_reported": states["active"],
        "onboarded": states["onboarded"],
        "paused": states["paused"],
        "by_acquisition_channel": dict(sorted(channels.items())),
        "payment_method_coverage": dict(sorted(rails.items())),
        "first_five_merchant_target_remaining": max(0, 5 - len(merchants)),
        "twenty_merchant_target_remaining": max(0, 20 - len(merchants)),
        "verification": "operator_attested_not_independently_verified",
        "consent_window_days": CONSENT_DAYS,
        "visibility": "local_only",
        "privacy": "Pseudonymous aliases and generic rail labels only. Expired profiles excluded from reporting.",
    }
