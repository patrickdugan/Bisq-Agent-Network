"""BAN <-> P2P-Settlement-Arena interchange contract (spec D.5).

P2P-Settlement-Arena (github.com/patrickdugan/P2P-Settlement-Arena @0f826272) is a
deterministic Rust lab that scores P2P settlement. This module lets it model the
cashout direction and lets BAN's simulated provider report in its vocabulary:

- ``passed`` means the oracle matched the scenario's expected outcome;
- ``attack_success`` means a forbidden economic outcome happened.

The two are never conflated: an unmitigated ``provider_withholds`` passes *and*
shows ``attack_success=true``, the way the arena reports ``seller-withholds``.
Fiat moves only on a modeled rail event, never on a provider's claim (arena
AGENTS.md:7). Nothing here touches a real rail, key or node.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from . import canon

ARENA_CONTRACT_VERSION = "ban-arena-cashout/1"
ARENA_PIN = "0f8262723d761b34890c49e04e80e5e2254f5387"
PROFILE_BTC_FIRST = "cashout-btc-first"    # S funds BTC, then P pays R: S bears the exposure
PROFILE_BISQ_EASY = "cashout-bisq-easy"    # P pays R first, S releases BTC on R's ack or rail evidence
PROFILES = (PROFILE_BTC_FIRST, PROFILE_BISQ_EASY)

# Direction, encoded once: the provider BUYS BTC, so it is the arena's buyer.
ROLE_MAP: Mapping[str, str] = MappingProxyType({
    "provider": "buyer",       # maker BUY offer: buys BTC, pays fiat
    "sender": "seller",        # delivers BTC
    "recipient": "fiat_payee",  # new arena role: fiat goes to a third party, not the seller
})
if ROLE_MAP["provider"] != "buyer" or ROLE_MAP["sender"] != "seller":  # survives python -O, unlike assert
    raise ImportError("arena role map must keep the provider as the BTC buyer")

ACTORS = ("provider", "sender", "recipient", "environment")
EXPECTED = ("settled", "blocked", "exposure_observed")

# behavior -> (acting party, expected oracle in btc-first, expected oracle in bisq-easy).
# The party that moves first bears the second mover's misbehavior: S in btc-first, P in
# bisq-easy. Misbehavior by the first mover, the environment or an outside attacker is
# blocked by the other side's checks.
BEHAVIORS: Mapping[str, tuple[str, str, str]] = MappingProxyType({
    "honest": ("provider", "settled", "settled"),
    "provider_withholds": ("provider", "exposure_observed", "blocked"),
    "sender_withholds": ("sender", "blocked", "exposure_observed"),
    "replay_funding": ("sender", "blocked", "exposure_observed"),
    "wrong_output": ("sender", "blocked", "exposure_observed"),
    "stale_chain": ("environment", "blocked", "exposure_observed"),
    "duplicate_payout": ("provider", "blocked", "blocked"),
    "rail_timeout": ("environment", "blocked", "blocked"),
    "late_funding": ("sender", "blocked", "exposure_observed"),
    "substituted_provider_key": ("environment", "blocked", "blocked"),
    "alias_equivocation": ("recipient", "blocked", "blocked"),
    # btc-first: the false claim is refused (no receipt, R not credited), but S's BTC stays
    # held unpaid, so the exposure is reported. bisq-easy: S never releases on a claim.
    "claim_without_credit": ("provider", "exposure_observed", "blocked"),
})

_CCY = re.compile(r"[A-Z]{3}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_:-]{0,79}")


class ArenaContractError(ValueError):
    pass


class ConservationError(ArenaContractError):
    pass


def _nonneg(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ArenaContractError(name + " must be a non-negative integer")
    return value


def arena_supported(currency: str, exponent: int, net_payout_minor: int) -> bool:
    """The arena scores USD only, $6-$600 (PROTOCOL_PROFILES.md:15)."""
    return currency == "USD" and exponent == 2 and 600 <= net_payout_minor <= 60_000


# ------------------------------------------------------------------ scenarios

@dataclass(frozen=True)
class ArenaScenarioV1:
    profile: str
    scenario: str
    seed: int
    amount_sats: int
    net_payout_minor: int
    currency: str
    exponent: int
    quote_expiry_height: int
    min_conf: int
    behavior: Mapping[str, str]
    expected: str

    def as_dict(self) -> dict:
        if self.profile not in PROFILES or not isinstance(self.scenario, str) or not _NAME.fullmatch(self.scenario):
            raise ArenaContractError("unknown profile or invalid scenario name")
        if set(self.behavior) != set(ACTORS) or any(b not in BEHAVIORS for b in self.behavior.values()):
            raise ArenaContractError("behavior must name every actor with a known behavior")
        if self.expected not in EXPECTED:
            raise ArenaContractError("expected must be settled, blocked or exposure_observed")
        if not isinstance(self.currency, str) or not _CCY.fullmatch(self.currency):
            raise ArenaContractError("currency must be an ISO 4217 alpha code")
        if type(self.exponent) is not int or not 0 <= self.exponent <= 8:
            raise ArenaContractError("exponent must be 0..8")
        for name in ("seed", "amount_sats", "net_payout_minor", "quote_expiry_height", "min_conf"):
            _nonneg(getattr(self, name), name)
        return {
            "v": 1, "contract": ARENA_CONTRACT_VERSION, "profile": self.profile, "scenario": self.scenario,
            "seed": self.seed, "amount_sats": self.amount_sats, "net_payout_minor": self.net_payout_minor,
            "currency": self.currency, "exponent": self.exponent, "quote_expiry_height": self.quote_expiry_height,
            "min_conf": self.min_conf, "behavior": dict(self.behavior), "expected": self.expected,
            "arena_supported": arena_supported(self.currency, self.exponent, self.net_payout_minor),
            "source": "SYNTHETIC_FIXTURE",
        }

    def canonical(self) -> str:
        return canon.dumps(self.as_dict())

    def scenario_id(self) -> str:
        return sha256(b"BAN-ARENA-SCENARIO-V1\x00" + self.canonical().encode("utf-8")).hexdigest()


def scenario_for(profile: str, behavior: str, *, amount_sats: int, net_payout_minor: int, currency: str,
                 exponent: int, quote_expiry_height: int, min_conf: int, seed: int = 7) -> ArenaScenarioV1:
    """The scenario where exactly one party deviates with ``behavior``."""
    if behavior not in BEHAVIORS:
        raise ArenaContractError("unknown behavior: " + str(behavior))
    actor, btc_first, bisq_easy = BEHAVIORS[behavior]
    actors = {a: "honest" for a in ACTORS}
    actors[actor] = behavior
    return ArenaScenarioV1(profile, behavior, seed, amount_sats, net_payout_minor, currency, exponent,
                           quote_expiry_height, min_conf, MappingProxyType(actors),
                           btc_first if profile == PROFILE_BTC_FIRST else bisq_easy)


def scenario_from_dict(data: Mapping[str, Any]) -> ArenaScenarioV1:
    """Strict import of an exported scenario (the inverse of ``as_dict``)."""
    keys = {"v", "contract", "profile", "scenario", "seed", "amount_sats", "net_payout_minor", "currency",
            "exponent", "quote_expiry_height", "min_conf", "behavior", "expected", "arena_supported", "source"}
    if not isinstance(data, Mapping) or set(data) != keys:
        raise ArenaContractError("scenario must have exactly the ArenaScenarioV1 fields")
    if data["v"] != 1 or data["contract"] != ARENA_CONTRACT_VERSION or data["source"] != "SYNTHETIC_FIXTURE":
        raise ArenaContractError("unsupported scenario version, contract or source")
    if not isinstance(data["behavior"], Mapping):
        raise ArenaContractError("behavior must be an object")
    out = ArenaScenarioV1(data["profile"], data["scenario"], data["seed"], data["amount_sats"],
                          data["net_payout_minor"], data["currency"], data["exponent"], data["quote_expiry_height"],
                          data["min_conf"], MappingProxyType(dict(data["behavior"])), data["expected"])
    if out.as_dict() != dict(data):
        raise ArenaContractError("scenario fields are inconsistent (arena_supported or types)")
    return out


# ---------------------------------------------------------------- result lines

_SCENARIO_KEYS: Mapping[str, dict[str, type]] = MappingProxyType({
    # README.md:31 and agents.rs:75
    "bisq-easy": {"protocol": str, "scenario": str, "passed": bool, "attack_success": bool,
                  "blocked_attacks": int, "committed_events": int, "final_phase": str,
                  "buyer_usd_at_risk_cents": int, "seller_btc_at_risk_sats": int, "safety_findings": list},
    # scenarios.rs:45
    "bisq-musig-model": {"protocol": str, "scenario": str, "passed": bool, "attack_success": bool,
                         "recovered": bool, "trials": int, "committed_events": int, "acknowledgements": int,
                         "final_phase": str, "safety_findings": list},
})
_CASHOUT_KEYS: dict[str, type] = {
    "protocol": str, "scenario": str, "passed": bool, "attack_success": bool, "blocked_attacks": int,
    "committed_events": int, "final_phase": str, "sender_btc_at_risk_sats": int, "provider_minor_at_risk": int,
    "recipient_credited_minor": int, "currency": str, "exponent": int, "safety_findings": list,
}
# control.rs:401-402
_CONTROL_KEYS: dict[str, type] = {
    "kind": str, "protocol": str, "case": str, "guard": str, "phase": str, "round": int,
    "external_bank_paid": bool, "model_fiat_received": bool, "invoice_valid": bool,
    "initial_invoice_rejected": bool, "btc_delivered": bool, "buyer_btc_sats": int, "declined": bool,
    "forged_credit": bool, "blocked_actions": int, "actions_attempted": int, "last_action": str,
    "last_status": str, "last_fault": str, "journal_valid": bool, "main_task_success": bool,
    "side_task_success": bool, "buyer_usd_at_risk_cents": int, "seller_btc_at_risk_sats": int,
    "safety_findings": list,
}


@dataclass(frozen=True)
class ArenaResultV1:
    kind: str  # "scenario" | "control_state"
    protocol: str
    fields: Mapping[str, Any] = field(repr=False)

    @property
    def passed(self) -> bool | None:
        return self.fields.get("passed")

    @property
    def attack_success(self) -> bool | None:
        return self.fields.get("attack_success")


def _strict_object(line: str) -> dict:
    def pairs(items: list[tuple[str, Any]]) -> dict:
        out: dict = {}
        for key, value in items:
            if key in out:
                raise ArenaContractError("duplicate key: " + key)
            out[key] = value
        return out

    def no_float(text: str) -> Any:
        raise ArenaContractError("non-integer number: " + text)

    if not isinstance(line, str) or "\n" in line.strip() or len(line) > 16384:
        raise ArenaContractError("expected one JSON line")
    try:
        data = json.loads(line, object_pairs_hook=pairs, parse_float=no_float, parse_constant=no_float)
    except ArenaContractError:
        raise
    except ValueError as exc:
        raise ArenaContractError("invalid JSON line") from exc
    if not isinstance(data, dict):
        raise ArenaContractError("a result line is a JSON object")
    return data


def _check_types(data: dict, schema: dict[str, type]) -> None:
    missing, unknown = set(schema) - set(data), set(data) - set(schema)
    if missing or unknown:
        raise ArenaContractError("missing keys %s, unknown keys %s" % (sorted(missing), sorted(unknown)))
    for key, kind in schema.items():
        value = data[key]
        if kind is bool and type(value) is not bool:
            raise ArenaContractError(key + " must be a boolean")
        if kind is int and (type(value) is not int or value < 0):
            raise ArenaContractError(key + " must be a non-negative integer")
        if kind is str and not isinstance(value, str):
            raise ArenaContractError(key + " must be a string")
        if kind is list and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)):
            raise ArenaContractError(key + " must be a list of strings")


def parse_arena_line(line: str) -> ArenaResultV1:
    """Strictly parse an arena scenario line, a ``control_state`` line or a cashout line."""
    data = _strict_object(line)
    if "kind" in data:
        if data["kind"] != "control_state" or data.get("protocol") != "bisq-easy":
            raise ArenaContractError("unsupported line kind or protocol")
        _check_types(data, _CONTROL_KEYS)
        return ArenaResultV1("control_state", "bisq-easy", MappingProxyType(data))
    protocol = data.get("protocol")
    if protocol in PROFILES:
        _check_types(data, _CASHOUT_KEYS)
        if not _CCY.fullmatch(data["currency"]) or data["exponent"] > 8:
            raise ArenaContractError("invalid currency or exponent")
    elif protocol in _SCENARIO_KEYS:
        _check_types(data, dict(_SCENARIO_KEYS[protocol]))
    else:
        raise ArenaContractError("unknown protocol")
    return ArenaResultV1("scenario", protocol, MappingProxyType(data))


# -------------------------------------------------------------------- traces

@dataclass(frozen=True)
class QuoteTrace:
    """One quote's B.4 history plus the value that moved for it."""
    transitions: tuple[tuple[str, str], ...]
    net_payout_minor: int
    sender_sent_sats: int        # new active-chain sats S sent to this quote's deposit script
    provider_received_sats: int  # what P's node holds at that script
    refunded_sats: int
    rail_credits_minor: tuple[int, ...]  # modeled rail credit events to R; the only way R is credited
    provider_claimed_minor: int  # what P claimed delivered (a receipt or a claim); never credits R
    funding_accepted: bool


@dataclass(frozen=True)
class CashoutTrace:
    scenario: str
    expected: str
    currency: str
    exponent: int
    quotes: tuple[QuoteTrace, ...]
    refusals: tuple[str, ...]  # finding codes for each refused step
    profile: str = PROFILE_BTC_FIRST


def check_conservation(trace: CashoutTrace) -> None:
    """Sats and fiat minor units are each conserved across S, P and R, and R is
    credited only by a modeled rail event, never by P's claim."""
    for q in trace.quotes:
        for name in ("net_payout_minor", "sender_sent_sats", "provider_received_sats", "refunded_sats",
                     "provider_claimed_minor"):
            _nonneg(getattr(q, name), name)
        for credit in q.rail_credits_minor:
            _nonneg(credit, "rail credit")
        if q.sender_sent_sats != q.provider_received_sats:
            raise ConservationError("sats: S sent %d but P received %d" % (q.sender_sent_sats, q.provider_received_sats))
        if q.refunded_sats > q.provider_received_sats:
            raise ConservationError("sats: P refunded more than it received")
        states = {to for _, to in q.transitions}
        if "RECEIPTED" in states and sum(q.rail_credits_minor) < q.net_payout_minor:
            raise ConservationError("fiat: a receipt was issued without a rail credit (claim is not credit)")
        if sum(q.rail_credits_minor) and not q.funding_accepted:
            raise ConservationError("fiat: R was credited for a quote with no accepted funding")


def _outcome(trace: CashoutTrace) -> tuple[int, int, int, list[str]]:
    sender_risk = provider_risk = credited = 0
    findings = list(trace.refusals)
    for q in trace.quotes:
        credit = sum(q.rail_credits_minor)
        credited += credit
        if credit < q.net_payout_minor and q.funding_accepted:
            residual = q.provider_received_sats - q.refunded_sats
            if residual:
                sender_risk += residual
                findings.append("provider_non_delivery")
        if credit > q.net_payout_minor:
            provider_risk += credit - q.net_payout_minor
            findings.append("duplicate_credit")
        if credit and not q.funding_accepted:
            provider_risk += credit
            findings.append("payout_without_funding")
    return sender_risk, provider_risk, credited, sorted(set(findings))


def ledger_trace_to_arena_lines(trace: CashoutTrace) -> list[str]:
    """Score one BAN trace as an arena line (``protocol: cashout-btc-first``)."""
    if trace.profile != PROFILE_BTC_FIRST:
        raise ArenaContractError("BAN simulates only cashout-btc-first; cashout-bisq-easy is arena-side (G.12)")
    if trace.expected not in EXPECTED:
        raise ArenaContractError("unknown expected oracle")
    check_conservation(trace)
    sender_risk, provider_risk, credited, findings = _outcome(trace)
    attack_success = bool(sender_risk or provider_risk)
    if trace.expected == "settled":
        net = sum(q.net_payout_minor for q in trace.quotes if q.funding_accepted)
        passed = not attack_success and credited == net and net > 0
    else:
        passed = attack_success == (trace.expected == "exposure_observed")
    transitions = [t for q in trace.quotes for t in q.transitions]
    line = {
        "protocol": trace.profile, "scenario": trace.scenario, "passed": passed, "attack_success": attack_success,
        "blocked_attacks": len(trace.refusals), "committed_events": len(transitions),
        "final_phase": transitions[-1][1] if transitions else "NONE",
        "sender_btc_at_risk_sats": sender_risk, "provider_minor_at_risk": provider_risk,
        "recipient_credited_minor": credited, "currency": trace.currency, "exponent": trace.exponent,
        "safety_findings": findings,
    }
    text = json.dumps(line, separators=(",", ":"))
    parse_arena_line(text)  # every exported line must satisfy the contract it claims
    return [text]


def write_jsonl(lines: Sequence[str], path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
