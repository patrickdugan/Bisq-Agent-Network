"""Read-only reconciliation contract for a proposed Bisq 2 trade-status endpoint.

This module NEVER submits, replays, confirms or retries a trade. An empty result
cannot establish that an earlier request had no effect on another node.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_STATE = re.compile(r"^[A-Z][A-Z0-9_]{0,160}$")


class ObservationError(ValueError):
    """Malformed or inconsistent trade observation: refuse to interpret it."""


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ObservationError("invalid identifier")
    return value


@dataclass(frozen=True)
class TradeObservation:
    trade_id: str
    offer_id: str
    state: str
    lifecycle: str

    def as_dict(self) -> dict[str, str]:
        return {"tradeId": self.trade_id, "offerId": self.offer_id,
                "tradeState": self.state, "lifecycle": self.lifecycle}


def parse_statuses(payload: Any, offer_id: str) -> tuple[TradeObservation, ...]:
    """Validate a COMPLETE GET /trades/status?offerId= response, not a WS delta.

    Deliberately refuses unexpected keys so future accidental exposure of
    payment, identity or key material cannot silently pass through BAN.
    """
    validate_id(offer_id)
    if not isinstance(payload, list) or len(payload) > 1000:
        raise ObservationError("expected at most 1000 status records")
    seen: set[str] = set()
    result: list[TradeObservation] = []
    required = {"tradeId", "offerId", "tradeState", "lifecycle"}
    for item in payload:
        if not isinstance(item, dict) or set(item) != required:
            raise ObservationError("unexpected trade status schema")
        trade_id = validate_id(item["tradeId"])
        row_offer_id = validate_id(item["offerId"])
        if row_offer_id != offer_id:
            raise ObservationError("response included another offer")
        state = item["tradeState"]
        if not isinstance(state, str) or not _STATE.fullmatch(state):
            raise ObservationError("invalid trade state")
        lifecycle = item["lifecycle"]
        if lifecycle not in ("OPEN", "CLOSED"):
            raise ObservationError("invalid trade lifecycle")
        if trade_id in seen:
            raise ObservationError("duplicate trade id")
        seen.add(trade_id)
        result.append(TradeObservation(trade_id, row_offer_id, state, lifecycle))
    return tuple(sorted(result, key=lambda obs: obs.trade_id))


def recovery_outcome(statuses: tuple[TradeObservation, ...]) -> str:
    """OBSERVED means a local trade exists; UNKNOWN never means safe to retry."""
    return "OBSERVED" if statuses else "UNKNOWN"
