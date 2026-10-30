"""Funding is re-judged before every rail call (invariant I9). SIMULATION ONLY.

Each case runs unchanged on a fixture-chain ``CashoutWorld`` and on a ``RegtestWorld``:
``orphan_funding`` and ``reconfirm_funding`` are the only chain moves, and each world
implements them on its own chain (a fork in the fixture, ``invalidateblock`` on regtest).
A case raises if an outcome deviates and returns what it observed.
"""
from __future__ import annotations

from typing import Callable

from .demo import _NotRefused, _refused
from .ledger import State
from .provider import ReferenceProvider, SimulatedCrash
from .world import CashoutWorld, payout_target, salt_for


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise _NotRefused(message)


def accepted(w: CashoutWorld, provider: ReferenceProvider | None = None):
    provider = provider or w.provider
    _, quote, _ = w.quote(provider)
    claim = w.fund(quote)
    w.advance(blocks=quote.min_conf - 1)
    state = provider.on_funding(quote.quote_id(), now=w.now)
    expect(state is State.FUNDING_ACCEPTED, "setup: funding not accepted (%s)" % state.value)
    return quote, claim


def calls_for(w: CashoutWorld, quote) -> int:
    return sum(1 for call in w.rail_mock.submit_calls if call[0] == quote.idempotency_key())


def _authorized_then_crashed(w: CashoutWorld):
    crashing = w.new_provider(crash_points=frozenset({"after_authorize"}))
    quote, claim = accepted(w, crashing)
    _refused(SimulatedCrash, lambda: crashing.pay(quote.quote_id(), now=w.now))  # authorized, never sent
    return quote, claim, w.new_provider()


def _hold_resume_after_funding_vanishes(w: CashoutWorld) -> dict:
    quote, claim, restarted = _authorized_then_crashed(w)
    qid = quote.quote_id()
    moved = payout_target(w.rail, w.currency, "SYNTHETIC-JAWWAL-WALLET-0002", salt_for("moved"), [w.p1.pubkey])
    seq2 = w.record(w.alice, seq=2, prev=w.record1["id"], payout=[moved])
    w.publish(seq2)
    held = restarted.recover(now=w.now).get(qid)  # submit() sees the moved commitment: HOLD, authorization kept
    w.publish(w.record(w.alice, seq=3, prev=seq2["id"]))  # the commitment is back, so the hold could lift
    w.orphan_funding(claim)
    resumed = [restarted.pay(qid, now=w.now) for _ in range(2)]
    calls = calls_for(w, quote)
    expect(held == State.HOLD.value and resumed == [State.FUNDING_SEEN] * 2 and calls == 0,
           "HOLD resume without funding: %s -> %s, %d rail calls" % (held, [s.value for s in resumed], calls))
    return {"held": held, "authorization_kept": restarted.ledger.authorization(qid) is not None,
            "resumed": [s.value for s in resumed], "rail_calls": calls,
            "history": ["%s->%s" % edge for edge in restarted.ledger.history(qid)]}


def _recovery_submit_after_funding_vanishes(w: CashoutWorld) -> dict:
    quote, claim, restarted = _authorized_then_crashed(w)
    qid = quote.quote_id()
    w.orphan_funding(claim)
    recovered = restarted.recover(now=w.now).get(qid)  # submit() re-judges before the attempt: HOLD
    attempt = restarted.ledger.attempt(quote.idempotency_key())
    after = restarted.authorize(qid, now=w.now)
    calls = calls_for(w, quote)
    expect(recovered == State.HOLD.value and attempt is None and after is State.FUNDING_SEEN and calls == 0,
           "recovery without funding: %s -> %s, %d rail calls" % (recovered, after.value, calls))
    return {"recovered": recovered, "rail_attempt_written": attempt is not None, "authorize_after": after.value,
            "rail_calls": calls}


def _reconfirmed_in_new_block(w: CashoutWorld) -> dict:
    quote, claim = accepted(w)
    qid = quote.quote_id()
    expect(w.provider.authorize(qid, now=w.now) is State.PAYOUT_AUTHORIZED, "setup: not authorized")
    first = w.provider.ledger.authorization(qid)
    old = w.provider.ledger.get(qid)
    w.orphan_funding(claim)
    w.reconfirm_funding(claim)  # valid again, but in another block than the one accepted
    steps = [w.provider.submit(qid, now=w.now), w.provider.authorize(qid, now=w.now),
             w.provider.on_funding(qid, now=w.now), w.provider.pay(qid, now=w.now)]
    row = w.provider.ledger.get(qid)
    auth = w.provider.ledger.authorization(qid)
    calls = calls_for(w, quote)
    expect(steps == [State.HOLD, State.FUNDING_SEEN, State.FUNDING_ACCEPTED, State.PAYOUT_SUBMITTED] and calls == 1
           and row.acceptance_block != old.acceptance_block and auth.funding_acceptance_digest == row.acceptance_digest
           and auth.funding_acceptance_digest != first.funding_acceptance_digest
           and auth.idempotency_key == first.idempotency_key,
           "reconfirmed funding: %s, %d rail calls" % ([s.value for s in steps], calls))
    return {"steps": [s.value for s in steps], "old_block": old.acceptance_block, "new_block": row.acceptance_block,
            "old_digest": first.funding_acceptance_digest, "authorized_digest": auth.funding_acceptance_digest,
            "current_digest": row.acceptance_digest, "same_idempotency_key": auth.idempotency_key == first.idempotency_key,
            "rail_calls": calls}


def _attempt_begun_is_never_resent(w: CashoutWorld) -> dict:
    sent, sent_claim = accepted(w)
    sid = sent.quote_id()
    expect(w.provider.pay(sid, now=w.now) is State.PAYOUT_SUBMITTED, "setup: not submitted")
    auth = w.provider.ledger.authorization(sid)
    w.orphan_funding(sent_claim)  # after the attempt: the provider bears the reorg (B.5)
    retries = [w.provider.pay(sid, now=w.now), w.provider.submit(sid, now=w.now),
               w.provider.authorize(sid, now=w.now)]
    w.provider.recover(now=w.now)
    after_retries = w.provider.ledger.get(sid).state
    rebound = w.provider.ledger.authorization(sid) != auth
    crashing = w.new_provider(crash_points=frozenset({"after_submit"}))
    lost, lost_claim = accepted(w, crashing)  # rail called, response never recorded
    lid = lost.quote_id()
    _refused(SimulatedCrash, lambda: crashing.pay(lid, now=w.now))
    w.orphan_funding(lost_claim)
    recovered = w.new_provider().recover(now=w.now).get(lid)
    counts = (calls_for(w, sent), calls_for(w, lost))
    expect(retries == [State.PAYOUT_SUBMITTED] * 3 and after_retries is State.PAYOUT_SUBMITTED and not rebound
           and recovered == State.PAYOUT_UNKNOWN.value and counts == (1, 1),
           "begun attempts: %s, recovered %s, rail calls %s" % ([s.value for s in retries], recovered, counts))
    return {"retries": [s.value for s in retries], "after_recover": after_retries.value,
            "authorization_rebound": rebound, "crashed_after_submit_recovered": recovered,
            "rail_calls": list(counts)}


RECHECK_CASES: dict[str, Callable[[CashoutWorld], dict]] = {
    "hold_resume_after_funding_vanishes": _hold_resume_after_funding_vanishes,
    "recovery_submit_after_funding_vanishes": _recovery_submit_after_funding_vanishes,
    "reconfirmed_in_new_block": _reconfirmed_in_new_block,
    "attempt_begun_is_never_resent": _attempt_begun_is_never_resent,
}
