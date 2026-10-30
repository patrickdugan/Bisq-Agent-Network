"""Local payout rail interface and a scripted mock. SIMULATION ONLY.

Jawwal Pay's real API, authentication, idempotency, status query, webhook
signing, currency and units are UNVERIFIED. ``MockJawwalRail`` assumes the two
capabilities the production gate requires (an idempotency key used as the
merchant reference, and query-by-reference) so the ledger can be exercised.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class RailTimeout(RuntimeError):
    """The rail did not answer. The payout may or may not have happened."""


@dataclass(frozen=True)
class RailResult:
    status: str  # pending | delivered | failed | not_found
    reference: str
    evidence: bytes | None = None  # raw rail response bytes; None when the rail returns nothing usable


class LocalPayoutRail(Protocol):
    def submit(self, idempotency_key: str, destination_ref: str,
               amount_minor: int, currency: str) -> RailResult: ...

    def status(self, idempotency_key: str) -> RailResult: ...


class MockJawwalRail:
    """Scripted outcomes. ``submit_script`` entries: ``delivered``, ``pending``,
    ``failed``, ``timeout`` (rail processed it, reply lost) and ``timeout_lost``
    (nothing reached the rail). ``status_script`` entries are what successive
    ``status()`` calls report for a received payout."""

    def __init__(self, submit_script: Sequence[str] = ("pending",),
                 status_script: Sequence[str] = ("delivered",), *, evidence: bool = True):
        self.submit_script = list(submit_script)
        self.status_script = list(status_script)
        self.evidence = evidence
        self.submit_calls: list[tuple[str, str, int, str]] = []
        self.status_calls: list[str] = []
        self.payouts: dict[str, str] = {}

    @staticmethod
    def reference(idempotency_key: str) -> str:
        return "JWL-SIM-" + idempotency_key[:16]

    def _result(self, key: str, status: str) -> RailResult:
        body = ('{"merchant_reference":"%s","status":"%s","source":"SYNTHETIC_FIXTURE"}' % (key, status)).encode()
        return RailResult(status, self.reference(key), body if self.evidence else None)

    def submit(self, idempotency_key: str, destination_ref: str, amount_minor: int, currency: str) -> RailResult:
        self.submit_calls.append((idempotency_key, destination_ref, amount_minor, currency))
        if idempotency_key in self.payouts:  # a well-behaved rail dedupes by merchant reference
            return self._result(idempotency_key, self.payouts[idempotency_key])
        outcome = self.submit_script.pop(0) if self.submit_script else "pending"
        if outcome == "timeout_lost":
            raise RailTimeout("no response (request lost)")
        stored = "pending" if outcome == "timeout" else outcome
        self.payouts[idempotency_key] = stored
        if outcome == "timeout":
            raise RailTimeout("no response (request processed)")
        return self._result(idempotency_key, stored)

    def status(self, idempotency_key: str) -> RailResult:
        self.status_calls.append(idempotency_key)
        if idempotency_key not in self.payouts:
            return RailResult("not_found", "", None)
        if self.payouts[idempotency_key] == "pending" and self.status_script:
            self.payouts[idempotency_key] = self.status_script.pop(0)
        return self._result(idempotency_key, self.payouts[idempotency_key])

    def webhook(self, idempotency_key: str) -> dict:
        """An unsigned status notification, as a rail of unverified authenticity might send."""
        return {"reference": idempotency_key, "status": self.payouts.get(idempotency_key, "not_found")}
