"""``ban cashout-demo``: the spec B.3 flow against a synthetic world, then every
F.3 attack replayed and refused. SIMULATION ONLY: no network, keys, funds,
real rail or proof. Each scenario runs in its own temporary world."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from typing import Callable

from ..alias import (AliasError, BindingMismatch, DestinationLeak, Equivocation, Expired, InvalidRecord, Revoked,
                     Rollback, StaleResolution, parse_record)
from ..cashout_select import cashout_shortlist
from ..evidence import (FixtureEvidence, InsufficientConfirmations, PolicyRejected, ReplayedOutput, StaleChain,
                        UntrustedAssertion, WrongOutput, admit_third_party)
from ..intent import EvidencePolicyV1, QuoteRejected, check_quote, standard_script_type, verify_receipt
from ..models import normalize_snapshot
from ..policy import Decision, evaluate
from .ledger import ReplayRejected, State
from .provider import ProviderRefused
from .rails import MockJawwalRail
from .world import CashoutWorld, fixture_inventory, payout_target, salt_for


def _world(fx: dict, root: Path, name: str) -> CashoutWorld:
    path = root / name
    path.mkdir(parents=True)
    world = CashoutWorld(path, fx)
    world.enroll()
    return world


def _refused(exc_types: tuple[type[BaseException], ...] | type[BaseException], action: Callable[[], object]) -> str:
    try:
        action()
    except exc_types as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    raise _NotRefused("action completed")


class _NotRefused(AssertionError):
    pass


def _funded(w: CashoutWorld, provider=None, **intent_overrides):
    provider = provider or w.provider
    _, quote, sig = w.quote(provider, **intent_overrides)
    claim = w.fund(quote)
    w.advance(blocks=w.cfg["provider"]["min_conf"] - 1, seconds=600)
    state = provider.on_funding(quote.quote_id(), now=w.now)
    if state is not State.FUNDING_ACCEPTED:
        raise _NotRefused("setup: funding not accepted (%s)" % state.value)
    return quote, claim


# --------------------------------------------------------------------- flow

def happy_path(fx: dict, root: Path) -> list[dict]:
    w = _world(fx, root, "flow")
    steps: list[dict] = []
    doc = fx["nip05"][w.domain]
    steps.append({"step": "T0a", "transition": "handle registration (domain NIP-05 fixture)",
                  "binding": "%s -> %s" % (w.name, doc["names"][w.handle]), "redirects": "refused"})
    steps.append({"step": "T0b", "transition": "AliasRecordV1 published", "record_id": w.record1["id"],
                  "seq": 1, "relays": fx["relays"], "public_payout": "commitment only, no destination"})
    steps.append({"step": "T0c", "transition": "private destination enrollment at P1",
                  "ack": "accepted (signed by P1)", "destination_visible_to": "P1 only"})
    res = w.resolve()
    steps.append({"step": "T1", "transition": "sender resolves %s" % w.name, "identity": res.identity,
                  "pubkey_fingerprint": res.record.pubkey[:16], "seq": res.record.seq,
                  "relay_heads": dict(res.relay_heads), "warnings": list(res.warnings)})
    snapshot = normalize_snapshot(fx["offerbook"], w.currency, "SYNTHETIC_FIXTURE")
    intent, intent_sig = w.intent(res)
    inventory = fixture_inventory(fx["inventory"], fx["keys"], now=w.now, rail=w.rail, currency=w.currency,
                                  exponent=w.exponent)
    selection = cashout_shortlist(snapshot, intent, inventory, providers=res.record.target_for(
        intent.recipient_commitment).providers, now=w.now)
    chosen = selection["offers"][0]
    if chosen["offer_id"] != fx["provider"]["offer_id"] or chosen["rate_minor_per_btc"] != w.provider.rate_minor_per_btc:
        raise ValueError("fixture provider does not match the top cashout offer")
    steps.append({"step": "select", "transition": "BUY-side shortlist",
                  "offerbook": {o.offer_id: o.maker_direction for o in snapshot.offers},
                  "selected": [o["offer_id"] for o in selection["offers"]], "excluded": selection["excluded"],
                  "chosen": chosen["offer_id"], "rate_minor_per_btc": chosen["rate_minor_per_btc"]})
    steps.append({"step": "T2", "transition": "PayoutIntentV1 (signed by an ephemeral sender key)",
                  "intent_id": intent.intent_id(), "amount_mode": intent.amount_mode, "amount_minor": intent.amount,
                  "max_total_fee_sats": intent.max_total_fee_sats, "funding_source_hint": intent.funding_source})
    quote, sig = w.provider.quote(intent, intent_sig, tip_height=w.chain.tip_height, now=w.now)
    check_quote(quote, sig, intent, res, tip_height=w.chain.tip_height, now=w.now)
    steps.append({"step": "T3", "transition": "ProviderQuoteV1 + reservation (checked by sender)",
                  "quote_id": quote.quote_id(), "deposit_script": quote.deposit_script,
                  "script_type": standard_script_type(bytes.fromhex(quote.deposit_script)),
                  "sats_required": quote.sats_required, "fee_minor": quote.fee_minor, "fee_sats": quote.fee_sats,
                  "net_payout_minor": quote.net_payout_minor, "expiry_height": quote.quote_expiry_height,
                  "reserved_minor": w.provider.ledger.held_minor()})
    qid = quote.quote_id()
    claim = w.fund(quote)
    first = w.provider.on_funding(qid, now=w.now)
    steps.append({"step": "T4", "transition": "funding tx (fixture chain)", "outpoint": "%s:%d" % (claim.txid, claim.vout),
                  "height": w.chain.tip_height, "provider_state": first.value})
    w.advance(blocks=w.cfg["provider"]["min_conf"] - 1, seconds=600)
    accepted = w.provider.on_funding(qid, now=w.now)
    steps.append({"step": "T5", "transition": "FundingAcceptanceV1 (provider's own node view)",
                  "provider_state": accepted.value, "min_conf": quote.min_conf})
    authorized = w.provider.authorize(qid, now=w.now)
    steps.append({"step": "T6", "transition": "PayoutAuthorizationV1 (write-ahead)", "provider_state": authorized.value,
                  "idempotency_key": quote.idempotency_key()})
    submitted = w.provider.submit(qid, now=w.now)
    confirmed = w.provider.poll(qid, now=w.now)
    steps.append({"step": "T7", "transition": "mock Jawwal payout (capabilities unverified)",
                  "submit": submitted.value, "status_query": confirmed.value,
                  "rail_calls": len(w.rail_mock.submit_calls)})
    receipt, rsig = w.provider.issue_receipt(qid, now=w.now)
    verify_receipt(receipt, rsig, quote)
    steps.append({"step": "T8", "transition": "PayoutReceiptV1 verified by sender", "receipt_id": receipt.receipt_id(),
                  "status": receipt.status, "rail_evidence_kind": receipt.rail_evidence_kind,
                  "rail_reference": receipt.rail_reference})
    recon = w.provider.reconcile({quote.idempotency_key(): ("delivered", quote.net_payout_minor)}, now=w.now)
    steps.append({"step": "T9", "transition": "reconciliation against the mock rail statement", "result": recon[qid],
                  "reserved_minor_after": w.provider.ledger.held_minor(),
                  "ledger_history": ["%s->%s" % edge for edge in w.provider.ledger.history(qid)]})
    return steps


# ------------------------------------------------------------------ attacks

def _sell_maker(w: CashoutWorld, fx: dict) -> str:
    snapshot = normalize_snapshot(fx["offerbook"], w.currency, "SYNTHETIC_FIXTURE")
    intent, _ = w.intent()
    inventory = fixture_inventory(fx["inventory"], fx["keys"], now=w.now, rail=w.rail, currency=w.currency,
                                  exponent=w.exponent)
    result = cashout_shortlist(snapshot, intent, inventory, providers=[w.p1.pubkey], now=w.now)
    if any(o["maker_direction"] != "BUY" for o in result["offers"]) or not result["excluded"].get("maker_sells_btc"):
        raise _NotRefused("a SELL maker reached the cashout shortlist")
    return "excluded %d SELL makers (one with a valid P1 attestation)" % result["excluded"]["maker_sells_btc"]


def _binding_mismatch(w: CashoutWorld, fx: dict) -> str:
    w.withdraw_all()
    w.publish(w.record(w.attacker, seq=1))
    return _refused(BindingMismatch, w.resolve)


def _forged_signature(w: CashoutWorld, fx: dict) -> str:
    event = dict(w.record1, sig=("1" if w.record1["sig"][0] == "0" else "0") + w.record1["sig"][1:])
    return _refused(InvalidRecord, lambda: parse_record(event, now=w.now))


def _seq2(w: CashoutWorld, **kw) -> dict:
    return w.record(w.alice, seq=2, prev=w.record1["id"], **kw)


def _rollback(w: CashoutWorld, fx: dict) -> str:
    w.publish(_seq2(w))
    w.resolve()
    w.withdraw_all()
    w.publish(w.record1)
    return _refused(Rollback, w.resolve)


def _equivocation(w: CashoutWorld, fx: dict) -> str:
    w.resolve()
    w.publish(_seq2(w), relays=[0])
    w.publish(_seq2(w, valid_seconds=86400), relays=[1])
    return _refused(Equivocation, w.resolve)


def _expired(w: CashoutWorld, fx: dict) -> str:
    w.withdraw_all()
    w.publish(w.record(w.alice, seq=1, created_at=w.now - 10 * 86400, valid_seconds=5 * 86400))
    return _refused(Expired, w.resolve)


def _revoked(w: CashoutWorld, fx: dict) -> str:
    w.publish(_seq2(w, status="revoked"))
    return _refused(Revoked, w.resolve) + " (no intent can be built)"


def _rebind(w: CashoutWorld, fx: dict) -> str:
    w.resolve()
    w.publish(w.record(w.attacker, seq=2, prev=w.record1["id"]))
    w.nip05.documents[w.domain] = {"names": {w.handle: w.attacker.pubkey}}
    res = w.resolve()
    if res.identity != "new_identity" or "new_recipient" not in res.warnings:
        raise _NotRefused("domain rebind carried the pin over")
    return "flagged new_recipient: treated as a new identity, pin not carried over"


def _payto(w: CashoutWorld, fx: dict) -> str:
    event = w.record(w.alice, seq=1, extra_tags=[["payto", "revolut", "@alice"]])
    return _refused(DestinationLeak, lambda: parse_record(event, now=w.now))


def _disagreement(w: CashoutWorld, fx: dict) -> str:
    w.publish(_seq2(w), relays=[0])
    res = w.resolve()
    threshold = fx["relay_disagreement_threshold_minor"]
    return _refused(StaleResolution, lambda: res.admit(threshold + 1, threshold_minor=threshold))


def _quote_check(w: CashoutWorld, mutate: Callable) -> str:
    res = w.resolve()
    intent, intent_sig = w.intent(res)
    quote, sig = w.provider.quote(intent, intent_sig, tip_height=w.chain.tip_height, now=w.now)
    return _refused(QuoteRejected, lambda: check_quote(*mutate(w, res, intent, quote, sig),
                                                       tip_height=w.chain.tip_height, now=w.now))


def _resign(w: CashoutWorld, res, intent, quote, **changes):
    forged = replace(quote, **changes)
    return forged, w.p1.sign_hex(forged.quote_id()), intent, res


def _substituted(w, res, intent, quote, sig):
    forged = replace(quote, provider_pubkey=w.attacker.pubkey)
    return forged, w.attacker.sign_hex(forged.quote_id()), intent, res


def _other_commitment(w, res, intent, quote, sig):
    other, _ = w.intent(res, recipient_commitment=salt_for("not-alice").hex())
    return _resign(w, res, other, quote, intent_id=other.intent_id())


def _other_intent(w, res, intent, quote, sig):
    other, _ = w.intent(res)
    return quote, sig, other, res


def _fee_cap(w: CashoutWorld, fx: dict) -> str:
    res = w.resolve()
    intent, intent_sig = w.intent(res, max_total_fee_sats=100)
    quote, sig = w.provider.quote(intent, intent_sig, tip_height=w.chain.tip_height, now=w.now)
    return _refused(QuoteRejected, lambda: check_quote(quote, sig, intent, res, tip_height=w.chain.tip_height,
                                                       now=w.now))


def _expired_quote(w: CashoutWorld, fx: dict) -> str:
    res = w.resolve()
    intent, intent_sig = w.intent(res)
    quote, sig = w.provider.quote(intent, intent_sig, tip_height=w.chain.tip_height, now=w.now)
    return _refused(QuoteRejected, lambda: check_quote(quote, sig, intent, res, tip_height=quote.quote_expiry_height,
                                                       now=w.now))


def _script_reuse(w: CashoutWorld, fx: dict) -> str:
    res = w.resolve()
    intent, intent_sig = w.intent(res)
    quote, sig = w.provider.quote(intent, intent_sig, tip_height=w.chain.tip_height, now=w.now)
    sender = _refused(QuoteRejected, lambda: check_quote(quote, sig, intent, res, tip_height=w.chain.tip_height,
                                                         now=w.now, seen_deposit_scripts={quote.deposit_script_sha256}))
    other, _ = w.intent(res)
    clone = replace(quote, intent_id=other.intent_id(), reservation_id="ee" * 32)
    ledger = _refused(ReplayRejected, lambda: w.provider.ledger.record_quote(
        clone, sig, other, handle=w.name, reserved_minor=1, now=w.now))
    return sender + "; " + ledger


def _wrong_output(w: CashoutWorld, fx: dict) -> str:
    _, quote, _ = w.quote()
    claim = w.fund(quote, value=quote.sats_required - 1)
    w.advance(blocks=2)
    return _refused(WrongOutput, lambda: w.provider.on_funding(quote.quote_id(), now=w.now, claim=claim))


def _old_output(w: CashoutWorld, fx: dict) -> str:
    _, quote, _ = w.quote()
    claim = w.fund(quote, height=quote.quote_created_height - 10)
    return _refused(ReplayedOutput, lambda: w.provider.on_funding(quote.quote_id(), now=w.now, claim=claim))


def _late(w: CashoutWorld, fx: dict) -> str:
    _, quote, _ = w.quote()
    w.advance(blocks=quote.quote_expiry_height - w.chain.tip_height)
    w.fund(quote)
    w.advance(blocks=2)
    state = w.provider.on_funding(quote.quote_id(), now=w.now)
    if state is not State.LATE_FUNDING:
        raise _NotRefused("late funding accepted as " + state.value)
    return "LATE_FUNDING; " + _refused(ProviderRefused, lambda: w.provider.authorize(quote.quote_id(), now=w.now))


def _stale_chain(w: CashoutWorld, fx: dict) -> str:
    _, quote, _ = w.quote()
    w.advance(blocks=3)
    claim = w.fund(quote, height=w.chain.tip_height - 1, branch="fork")
    return _refused(StaleChain, lambda: w.provider.on_funding(quote.quote_id(), now=w.now, claim=claim))


def _confirmations(w: CashoutWorld, fx: dict) -> str:
    _, quote, _ = w.quote()
    claim = w.fund(quote)
    out = _refused(InsufficientConfirmations, lambda: FixtureEvidence(w.chain, w.policy).accept(
        quote, now=w.now, claim=claim))
    state = w.provider.on_funding(quote.quote_id(), now=w.now, claim=claim)
    if state is not State.FUNDING_SEEN:
        raise _NotRefused("accepted below min_conf")
    return out + " (provider stays FUNDING_SEEN)"


def _verified_flag(w: CashoutWorld, fx: dict) -> str:
    _, quote, _ = w.quote()
    policy = EvidencePolicyV1("test", w.network, {"sp1_btc_payment_v1": {
        "approval": {"state": "candidate", "by": "SYNTHETIC_FIXTURE", "at": w.now}}})
    quote = replace(quote, evidence_policy_digest=policy.digest())
    return _refused(UntrustedAssertion, lambda: admit_third_party(
        policy, quote, {"profile": "sp1_btc_payment_v1", "verified": True, "proof_verified": True}))


def _production(w: CashoutWorld, fx: dict) -> str:
    _, quote, _ = w.quote()
    claim = w.fund(quote)
    w.advance(blocks=2)
    prod = EvidencePolicyV1("production", w.network, {"own_core_v1": {
        "approval": {"state": "candidate", "by": "SYNTHETIC_FIXTURE", "at": w.now}, "chain": w.network}})
    quote = replace(quote, evidence_policy_digest=prod.digest())
    return _refused(PolicyRejected, lambda: FixtureEvidence(w.chain, prod).accept(quote, now=w.now, claim=claim))


def _outpoint_replay(w: CashoutWorld, fx: dict) -> str:
    _, claim = _funded(w)
    _, quote_b, _ = w.quote()
    return _refused(ReplayRejected, lambda: w.provider.ledger.transition(
        quote_b.quote_id(), State.FUNDING_SEEN, now=w.now, outpoint="%s:%d" % (claim.txid, claim.vout),
        funding_height=w.chain.tip_height))


def _duplicate_payout(w: CashoutWorld, fx: dict) -> str:
    quote, _ = _funded(w)
    for _ in range(3):
        w.provider.pay(quote.quote_id(), now=w.now)
    if len(w.rail_mock.submit_calls) != 1:
        raise _NotRefused("rail called %d times" % len(w.rail_mock.submit_calls))
    return "3 authorize+submit attempts -> 1 rail call"


def _timeout(w: CashoutWorld, fx: dict) -> str:
    provider = w.new_provider(rail=MockJawwalRail(["timeout"], ["delivered"]))
    quote, _ = _funded(w, provider)
    states = [provider.pay(quote.quote_id(), now=w.now), provider.submit(quote.quote_id(), now=w.now)]
    provider.recover(now=w.now)
    if states != [State.PAYOUT_UNKNOWN] * 2 or len(w.rail_mock.submit_calls) != 1:
        raise _NotRefused("timeout led to a resubmit")
    final = provider.poll(quote.quote_id(), now=w.now)
    return "PAYOUT_UNKNOWN, no resubmit; resolved by status() -> " + final.value


def _failure(w: CashoutWorld, fx: dict) -> str:
    provider = w.new_provider(rail=MockJawwalRail(["failed"]))
    quote, _ = _funded(w, provider)
    state = provider.pay(quote.quote_id(), now=w.now)
    if state is not State.REFUND_DUE:
        raise _NotRefused("rail failure ended in " + state.value)
    return "PAYOUT_FAILED -> REFUND_DUE (reservation held until REFUNDED)"


def _revoked_before_payout(w: CashoutWorld, fx: dict) -> str:
    quote, _ = _funded(w)
    moved = payout_target(w.rail, w.currency, "SYNTHETIC-JAWWAL-WALLET-0002", salt_for("moved"), [w.p1.pubkey])
    w.publish(_seq2(w, status="revoked", payout=[moved]))
    state = w.provider.pay(quote.quote_id(), now=w.now)
    if state is not State.HOLD or w.rail_mock.submit_calls:
        raise _NotRefused("payout proceeded after revocation")
    return "HOLD before any rail call"


def _policy(w: CashoutWorld, fx: dict) -> str:
    results = {a: evaluate(a).decision for a in ("authorize_local_payout", "submit_local_payout", "execute_cashout")}
    if any(d is not Decision.DENY for d in results.values()):
        raise _NotRefused("policy allowed a payout action")
    return "DENY: " + ", ".join(results)


ATTACKS: dict[str, Callable[[CashoutWorld, dict], str]] = {
    "sell_maker_in_cashout_shortlist": _sell_maker,
    "nip05_binding_mismatch": _binding_mismatch,
    "forged_record_signature": _forged_signature,
    "rollback": _rollback,
    "equivocation": _equivocation,
    "expired_record": _expired,
    "revoked_record": _revoked,
    "domain_rebind_without_rotation": _rebind,
    "payto_tag": _payto,
    "relay_disagreement_above_threshold": _disagreement,
    "substituted_provider_key": lambda w, fx: _quote_check(w, _substituted),
    "wrong_network": lambda w, fx: _quote_check(w, lambda w, r, i, q, s: _resign(w, r, i, q, network="mainnet")),
    "wrong_recipient_commitment": lambda w, fx: _quote_check(w, _other_commitment),
    "quote_for_other_intent": lambda w, fx: _quote_check(w, _other_intent),
    "fee_above_cap": _fee_cap,
    "expired_quote": _expired_quote,
    "deposit_script_reuse": _script_reuse,
    "wrong_output": _wrong_output,
    "replay_old_output": _old_output,
    "late_funding": _late,
    "stale_chain": _stale_chain,
    "insufficient_confirmations": _confirmations,
    "verified_true_assertion": _verified_flag,
    "candidate_keys_in_production": _production,
    "outpoint_replay_across_quotes": _outpoint_replay,
    "duplicate_payout": _duplicate_payout,
    "rail_timeout": _timeout,
    "rail_failure": _failure,
    "alias_revoked_before_payout": _revoked_before_payout,
    "policy_denies_real_payout": _policy,
}


def run_attack(fx: dict, root: Path, name: str) -> dict:
    if name not in ATTACKS:
        raise ValueError("unknown attack in fixture: " + name)
    w = _world(fx, root, "attack-" + name)
    try:
        return {"attack": name, "refused": True, "by": ATTACKS[name](w, fx)}
    except (_NotRefused, AliasError) as exc:
        return {"attack": name, "refused": False, "by": "%s: %s" % (type(exc).__name__, exc)}


def run_demo(fixture: str | Path) -> dict:
    fx = json.loads(Path(fixture).read_text(encoding="utf-8"))
    if not isinstance(fx, dict) or fx.get("source") != "SYNTHETIC_FIXTURE":
        raise ValueError("cashout-demo accepts SYNTHETIC_FIXTURE data only")
    with tempfile.TemporaryDirectory(prefix="ban-cashout-demo-") as tmp:
        root = Path(tmp)
        flow = happy_path(fx, root)
        attacks = [run_attack(fx, root, name) for name in fx["attacks"]]
    refused = sum(1 for a in attacks if a["refused"])
    return {
        "source": "SYNTHETIC_FIXTURE",
        "simulation_only": True,
        "transitions": flow,
        "attacks": attacks,
        "attacks_refused": refused,
        "attacks_total": len(attacks),
        "all_attacks_refused": refused == len(attacks),
        "warning": "Simulation only: synthetic keys and fixture chain, mock rail, no network. Jawwal Pay "
                   "capabilities are unverified; real payouts stay DENY in ban.policy.",
    }
