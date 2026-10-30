from dataclasses import replace
import tempfile
import unittest

from ban._db import immediate
from ban.evidence import WrongOutput
from ban.intent import ZERO32, PayoutAuthorizationV1, QuoteRejected, verify_receipt
from ban.sim.ledger import DuplicatePayout, IllegalTransition, InvariantViolation, ReplayRejected, State
from ban.sim.provider import NoInventory, ProviderRefused, SimulatedCrash
from ban.sim.rails import MockJawwalRail
from ban.sim.world import CashoutWorld, payout_target, salt_for


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = CashoutWorld(self.temp.name)
        self.w.enroll()

    def tearDown(self):
        self.temp.cleanup()

    def funded(self, provider=None):
        provider = provider or self.w.provider
        intent, quote, sig = self.w.quote(provider)
        claim = self.w.fund(quote)
        self.w.advance(blocks=1, seconds=600)
        self.assertEqual(provider.on_funding(quote.quote_id(), now=self.w.now), State.FUNDING_ACCEPTED)
        return quote, claim

    def restart(self, **kw):
        return self.w.new_provider(**kw)

    # -- replay and duplicates
    def test_same_outpoint_for_two_quotes_is_rejected(self):
        quote_a, claim = self.funded()
        _, quote_b, _ = self.w.quote()
        ledger = self.w.provider.ledger
        with self.assertRaisesRegex(ReplayRejected, "I2"):
            ledger.transition(quote_b.quote_id(), State.FUNDING_SEEN, now=self.w.now,
                              outpoint=claim.txid + ":" + str(claim.vout), funding_height=self.w.chain.tip_height)
        self.assertEqual(ledger.get(quote_b.quote_id()).state, State.QUOTED)
        with self.assertRaises(WrongOutput):  # provider path: B's script is not in A's output
            self.w.provider.on_funding(quote_b.quote_id(), now=self.w.now, claim=claim)

    def test_deposit_script_is_never_reused(self):
        intent, quote, sig = self.w.quote()
        other, _ = self.w.intent()
        clone = replace(quote, intent_id=other.intent_id(), reservation_id="ee" * 32)
        with self.assertRaisesRegex(ReplayRejected, "I3"):
            self.w.provider.ledger.record_quote(clone, sig, other, handle=self.w.name, reserved_minor=1, now=self.w.now)

    def test_same_quote_authorized_twice_submits_once(self):
        quote, _ = self.funded()
        qid = quote.quote_id()
        self.assertEqual(self.w.provider.pay(qid, now=self.w.now), State.PAYOUT_SUBMITTED)
        self.assertEqual(self.w.provider.pay(qid, now=self.w.now), State.PAYOUT_SUBMITTED)
        self.assertEqual(self.w.provider.authorize(qid, now=self.w.now), State.PAYOUT_SUBMITTED)
        self.assertEqual(self.w.provider.submit(qid, now=self.w.now), State.PAYOUT_SUBMITTED)
        self.assertEqual(len(self.w.rail_mock.submit_calls), 1)
        with self.assertRaises((DuplicatePayout, IllegalTransition)):
            self.w.provider.ledger.begin_attempt(qid, "00" * 32, now=self.w.now)
        auth = self.w.provider.ledger.authorization(qid)
        with self.assertRaisesRegex(InvariantViolation, "I1"):
            self.w.provider.ledger.authorize(replace(auth, idempotency_key="ab" * 32), now=self.w.now)

    # -- rail outcomes
    def test_rail_timeout_is_unknown_never_resubmitted_and_resolved_only_by_status(self):
        rail = MockJawwalRail(["timeout"], ["pending", "delivered"])
        provider = self.restart(rail=rail)
        quote, _ = self.funded(provider)
        qid = quote.quote_id()
        self.assertEqual(provider.pay(qid, now=self.w.now), State.PAYOUT_UNKNOWN)
        self.assertEqual(provider.submit(qid, now=self.w.now), State.PAYOUT_UNKNOWN)
        self.assertEqual(provider.recover(now=self.w.now), {})
        self.assertEqual(provider.pay(qid, now=self.w.now), State.PAYOUT_UNKNOWN)
        self.assertEqual(len(rail.submit_calls), 1)
        with self.assertRaisesRegex(InvariantViolation, "I4"):
            provider.ledger.transition(qid, State.PAYOUT_CONFIRMED, now=self.w.now)
        self.assertEqual(provider.poll(qid, now=self.w.now), State.PAYOUT_UNKNOWN)  # rail says pending
        self.assertEqual(provider.poll(qid, now=self.w.now), State.PAYOUT_CONFIRMED)
        self.assertEqual(len(rail.submit_calls), 1)

    def test_lost_request_stays_unknown_when_status_finds_nothing(self):
        rail = MockJawwalRail(["timeout_lost"])
        provider = self.restart(rail=rail)
        quote, _ = self.funded(provider)
        self.assertEqual(provider.pay(quote.quote_id(), now=self.w.now), State.PAYOUT_UNKNOWN)
        self.assertEqual(provider.poll(quote.quote_id(), now=self.w.now), State.PAYOUT_UNKNOWN)  # not_found
        self.assertEqual(len(rail.submit_calls), 1)

    def test_rail_failure_is_refund_due(self):
        rail = MockJawwalRail(["failed"])
        provider = self.restart(rail=rail)
        quote, _ = self.funded(provider)
        qid = quote.quote_id()
        self.assertEqual(provider.pay(qid, now=self.w.now), State.REFUND_DUE)
        with self.assertRaises(ProviderRefused):
            provider.issue_receipt(qid, now=self.w.now)
        self.assertTrue(provider.ledger.get(qid).reservation_held)
        provider.ledger.transition(qid, State.REFUNDED, now=self.w.now, refund_txid="ff" * 32)
        self.assertFalse(provider.ledger.get(qid).reservation_held)

    def test_duplicate_and_forged_webhooks_are_no_ops(self):
        rail = MockJawwalRail(["pending"], [])
        provider = self.restart(rail=rail)
        quote, _ = self.funded(provider)
        qid, key = quote.quote_id(), quote.idempotency_key()
        provider.pay(qid, now=self.w.now)
        forged = {"reference": key, "status": "delivered"}  # unsigned body; the rail still says pending
        self.assertEqual(provider.on_webhook(forged, now=self.w.now), State.PAYOUT_SUBMITTED)
        self.assertIsNone(provider.on_webhook({"reference": "00" * 32, "status": "delivered"}, now=self.w.now))
        rail.payouts[key] = "delivered"
        self.assertEqual(provider.on_webhook(rail.webhook(key), now=self.w.now), State.PAYOUT_CONFIRMED)
        calls = len(rail.status_calls)
        history = provider.ledger.history(qid)
        self.assertEqual(provider.on_webhook(rail.webhook(key), now=self.w.now), State.PAYOUT_CONFIRMED)
        self.assertEqual((len(rail.status_calls), provider.ledger.history(qid)), (calls, history))

    # -- crash injection
    def test_crash_between_authorize_and_submit(self):
        crashing = self.restart(crash_points=frozenset({"after_authorize"}))
        quote, _ = self.funded(crashing)
        qid = quote.quote_id()
        with self.assertRaises(SimulatedCrash):
            crashing.pay(qid, now=self.w.now)
        self.assertEqual(crashing.ledger.get(qid).state, State.PAYOUT_AUTHORIZED)
        self.assertEqual(self.w.rail_mock.submit_calls, [])
        restarted = self.restart()
        self.assertEqual(restarted.recover(now=self.w.now), {qid: State.PAYOUT_SUBMITTED.value})
        self.assertEqual(restarted.recover(now=self.w.now), {})
        self.assertEqual(restarted.poll(qid, now=self.w.now), State.PAYOUT_CONFIRMED)
        self.assertEqual(len(self.w.rail_mock.submit_calls), 1)

    def test_crash_between_submit_and_recording_the_response(self):
        crashing = self.restart(crash_points=frozenset({"after_submit"}))
        quote, _ = self.funded(crashing)
        qid = quote.quote_id()
        with self.assertRaises(SimulatedCrash):
            crashing.pay(qid, now=self.w.now)
        self.assertEqual(crashing.ledger.get(qid).state, State.PAYOUT_SUBMITTED)
        self.assertIsNone(crashing.ledger.attempt(quote.idempotency_key())["response_status"])
        restarted = self.restart()
        self.assertEqual(restarted.recover(now=self.w.now), {qid: State.PAYOUT_UNKNOWN.value})
        self.assertEqual(restarted.poll(qid, now=self.w.now), State.PAYOUT_CONFIRMED)
        self.assertEqual(len(self.w.rail_mock.submit_calls), 1)

    # -- reservations
    def test_reservation_held_until_expiry_and_no_quote_on_unreserved_inventory(self):
        small = self.w.new_provider(database=self.temp.name + "/small.sqlite3")
        small.float_minor = 7000
        self.w.enroll(small)
        _, quote, _ = self.w.quote(small)
        self.assertEqual(small.ledger.held_minor(), quote.net_payout_minor)
        with self.assertRaises(NoInventory):
            self.w.quote(small)
        self.w.advance(blocks=quote.quote_expiry_height - self.w.chain.tip_height)
        self.assertEqual(small.expire(now=self.w.now, tip_height=self.w.chain.tip_height), [quote.quote_id()])
        self.assertEqual(small.ledger.held_minor(), 0)
        self.w.quote(small)

    def test_a_funded_intent_never_gets_a_second_quote(self):
        intent, sig = self.w.intent()
        quote, qsig = self.w.provider.quote(intent, sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.w.fund(quote)
        self.w.advance(blocks=quote.quote_expiry_height - self.w.chain.tip_height + 1)
        self.w.provider.on_funding(quote.quote_id(), now=self.w.now)
        again = self.w.provider.quote(intent, sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.assertEqual(again, (quote, qsig))
        self.assertEqual(self.w.provider.ledger.quote_count(), 1)

    def test_expired_unfunded_quote_is_replaced_and_its_reservation_released(self):
        intent, sig = self.w.intent()
        quote, _ = self.w.provider.quote(intent, sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.w.advance(blocks=quote.quote_expiry_height - self.w.chain.tip_height)
        fresh, _ = self.w.provider.quote(intent, sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.assertNotEqual(fresh.quote_id(), quote.quote_id())
        self.assertEqual(self.w.provider.ledger.get(quote.quote_id()).state, State.EXPIRED)
        self.assertEqual(self.w.provider.ledger.held_minor(), fresh.net_payout_minor)

    # -- HOLD (I8)
    def test_alias_revoked_after_authorization_holds_before_the_rail_call(self):
        crashing = self.restart(crash_points=frozenset({"after_authorize"}))
        quote, _ = self.funded(crashing)
        with self.assertRaises(SimulatedCrash):
            crashing.pay(quote.quote_id(), now=self.w.now)
        self.w.publish(self.w.record(self.w.alice, seq=2, prev=self.w.record1["id"], status="revoked"))
        restarted = self.restart()
        self.assertEqual(restarted.recover(now=self.w.now), {quote.quote_id(): State.HOLD.value})
        self.assertEqual(self.w.rail_mock.submit_calls, [])

    def test_alias_revoked_between_quote_and_payout_holds(self):
        quote, _ = self.funded()
        self.w.publish(self.w.record(self.w.alice, seq=2, prev=self.w.record1["id"], status="revoked"))
        self.assertEqual(self.w.provider.pay(quote.quote_id(), now=self.w.now), State.HOLD)
        self.assertEqual(self.w.rail_mock.submit_calls, [])
        self.assertEqual(self.w.provider.ledger.transition(quote.quote_id(), State.REFUND_DUE, now=self.w.now).state,
                         State.REFUND_DUE)

    def test_commitment_changed_between_quote_and_payout_holds(self):
        quote, _ = self.funded()
        moved = payout_target(self.w.rail, self.w.currency, "SYNTHETIC-JAWWAL-WALLET-0002", salt_for("moved"),
                              [self.w.p1.pubkey])
        self.w.publish(self.w.record(self.w.alice, seq=2, prev=self.w.record1["id"], payout=[moved]))
        self.assertEqual(self.w.provider.pay(quote.quote_id(), now=self.w.now), State.HOLD)
        self.assertEqual(self.w.rail_mock.submit_calls, [])
        # Only a fresh check that passes leaves HOLD for authorization.
        self.w.publish(self.w.record(self.w.alice, seq=3, prev="ab" * 32))
        self.assertEqual(self.w.provider.pay(quote.quote_id(), now=self.w.now), State.PAYOUT_SUBMITTED)

    def test_authorization_amount_and_alias_seq_are_bound_to_the_quote(self):
        quote, _ = self.funded()
        row = self.w.provider.ledger.get(quote.quote_id())
        auth = PayoutAuthorizationV1(row.quote_id, row.acceptance_digest, "dd" * 32, row.net_payout_minor,
                                     row.currency, row.exponent, row.idempotency_key, row.alias_seq)
        with self.assertRaisesRegex(InvariantViolation, "I6"):
            self.w.provider.ledger.authorize(replace(auth, net_payout_minor=row.net_payout_minor + 1), now=self.w.now)
        # This quote was made at alias seq 1; model one made at seq 2 so seq 1 at authorization is below it.
        with immediate(self.w.provider.ledger.database) as db:
            db.execute("UPDATE quotes SET alias_seq=2 WHERE quote_id=?", (row.quote_id,))
        with self.assertRaisesRegex(InvariantViolation, "I8"):
            self.w.provider.ledger.authorize(auth, now=self.w.now)
        _, other, _ = self.w.quote()
        with self.assertRaisesRegex(InvariantViolation, "I7"):
            self.w.provider.ledger.transition(other.quote_id(), State.FUNDING_SEEN, now=self.w.now,
                                              outpoint="cc" * 32 + ":0", funding_height=other.quote_created_height - 1)

    # -- receipts
    def test_receipt_is_bound_to_the_idempotency_key_and_signed_by_the_provider(self):
        quote, _ = self.funded()
        qid = quote.quote_id()
        self.w.provider.pay(qid, now=self.w.now)
        self.w.provider.poll(qid, now=self.w.now)
        receipt, sig = self.w.provider.issue_receipt(qid, now=self.w.now)
        verify_receipt(receipt, sig, quote)
        self.assertEqual(receipt.idempotency_key, quote.idempotency_key())
        self.assertEqual(receipt.receipt_id(), replace(receipt, status="unknown").receipt_id())
        self.assertEqual(receipt.rail_evidence_kind, "api_status")
        self.assertNotIn(self.w.destination, receipt.encode().decode("latin-1"))
        with self.assertRaisesRegex(QuoteRejected, "signature"):
            verify_receipt(replace(receipt, amount_minor=receipt.amount_minor + 1), sig, quote)
        with self.assertRaisesRegex(QuoteRejected, "signature"):
            verify_receipt(receipt, self.w.attacker.sign_hex(receipt.digest()), quote)
        _, other, _ = self.w.quote()
        with self.assertRaisesRegex(QuoteRejected, "idempotency"):
            verify_receipt(receipt, sig, other)
        self.assertEqual(self.w.provider.reconcile({quote.idempotency_key(): ("delivered", 5000)}, now=self.w.now),
                         {qid: "RECONCILED"})
        self.assertFalse(self.w.provider.ledger.get(qid).reservation_held)

    def test_provider_attested_only_when_rail_evidence_is_absent(self):
        rail = MockJawwalRail(["delivered"], evidence=False)
        provider = self.restart(rail=rail)
        quote, _ = self.funded(provider)
        self.assertEqual(provider.pay(quote.quote_id(), now=self.w.now), State.PAYOUT_CONFIRMED)
        receipt, sig = provider.issue_receipt(quote.quote_id(), now=self.w.now)
        verify_receipt(receipt, sig, quote)
        self.assertEqual((receipt.rail_evidence_kind, receipt.rail_evidence_digest), ("provider_attested_only", ZERO32))

    def test_reconciliation_mismatch_is_disputed(self):
        quote, _ = self.funded()
        qid = quote.quote_id()
        self.w.provider.pay(qid, now=self.w.now)
        self.w.provider.poll(qid, now=self.w.now)
        self.w.provider.issue_receipt(qid, now=self.w.now)
        self.assertEqual(self.w.provider.reconcile({quote.idempotency_key(): ("delivered", 4999)}, now=self.w.now),
                         {qid: "DISPUTED"})


if __name__ == "__main__":
    unittest.main()
