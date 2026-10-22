"""Durable, observation-only ledger for ambiguous Bisq trading operations.

This is intentionally NOT an execution gateway. It records externally reported
unknown outcomes and reconciles against the local Bisq node. It never sends an
offer, confirms a payment, or marks an absent trade safe to retry.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3

from .trade_status import TradeObservation, ObservationError, validate_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uncertain_intents (
  intent_id TEXT PRIMARY KEY,
  offer_id TEXT NOT NULL,
  intent_commitment TEXT NOT NULL,
  result TEXT NOT NULL CHECK(result IN ('UNKNOWN', 'OBSERVED')),
  observed_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@dataclass(frozen=True)
class RecoveryRecord:
    intent_id: str
    offer_id: str
    result: str
    observed_trade_ids: tuple[str, ...]

    @property
    def safe_to_retry(self) -> bool:
        # A missing local trade is not proof that the P2P action never happened.
        return False


class RecoveryJournal:
    """A tiny durable journal, distinct from the aggregate GTM event ledger."""

    def __init__(self, database: str | Path):
        self.database = Path(database)
        with self._connect() as db:
            db.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=5.0)

    def record_ambiguous(self, intent_id: str, offer_id: str, commitment: str) -> RecoveryRecord:
        """Record a *previously submitted* uncertain intent, without executing it.

        Intent IDs and commitment digests are local opaque identifiers.
        Never pass raw payment account data, private keys or signatures.
        """
        validate_id(intent_id)
        validate_id(offer_id)
        if not isinstance(commitment, str) or len(commitment) != 64 or any(
                ch not in "0123456789abcdef" for ch in commitment):
            raise ObservationError("expected a lowercase SHA-256 intent commitment")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT offer_id,intent_commitment FROM uncertain_intents WHERE intent_id=?",
                (intent_id,)).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO uncertain_intents(intent_id,offer_id,intent_commitment,result) VALUES(?,?,?,'UNKNOWN')",
                    (intent_id, offer_id, commitment))
            elif existing != (offer_id, commitment):
                raise ObservationError("intent identifier reused for different terms")
        return self.read(intent_id)

    def read(self, intent_id: str) -> RecoveryRecord:
        validate_id(intent_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT offer_id,result,observed_ids FROM uncertain_intents WHERE intent_id=?",
                (intent_id,)).fetchone()
        if row is None:
            raise ObservationError("unknown recovery intent")
        return RecoveryRecord(intent_id, row[0], row[1], tuple(json.loads(row[2])))

    def reconcile(self, intent_id: str, observations: tuple[TradeObservation, ...]) -> RecoveryRecord:
        """Persist observed IDs; an empty query NEVER clears UNKNOWN or authorizes a retry.

        Caller must supply an independently validated, complete status response
        for this offer. A snapshot can show a matching trade but cannot prove
        that an earlier request created that particular trade.
        """
        if not isinstance(observations, tuple) or not all(
                isinstance(obs, TradeObservation) for obs in observations):
            raise ObservationError("expected validated observations")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT offer_id,result,observed_ids FROM uncertain_intents WHERE intent_id=?",
                (intent_id,)).fetchone()
            if row is None:
                raise ObservationError("unknown recovery intent")
            offer_id, prior_status, prior_json = row
            if any(obs.offer_id != offer_id for obs in observations):
                raise ObservationError("observation belongs to another offer")
            prior = set(json.loads(prior_json))
            current = {obs.trade_id for obs in observations}
            merged = prior | current
            result = "OBSERVED" if merged else prior_status
            db.execute(
                "UPDATE uncertain_intents SET result=?,observed_ids=?,updated_at=CURRENT_TIMESTAMP WHERE intent_id=?",
                (result, json.dumps(sorted(merged)), intent_id))
        return self.read(intent_id)
