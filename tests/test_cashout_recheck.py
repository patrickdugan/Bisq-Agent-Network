"""I9: the rail is called only after the same ``submit()`` re-judged the funding on the
node's current active chain, for an authorization that binds the current acceptance.

``RecheckCases`` asserts each case of ``ban.sim.recheck``. It runs on the fixture chain
everywhere, and on a private throwaway regtest node with ``BAN_REGTEST=1``.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from ban._db import immediate
from ban.sim.ledger import DuplicatePayout, State
from ban.sim.recheck import RECHECK_CASES, accepted, calls_for
from ban.sim.regtest import RegtestNode, case_world
from ban.sim.world import CashoutWorld

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "cashout_demo.json").read_text(encoding="utf-8"))
REGTEST = os.getenv("BAN_REGTEST") == "1"


class RecheckCases:
    def world(self):
        raise NotImplementedError

    def test_hold_resume_after_the_funding_vanished_sends_nothing(self):
        obs = RECHECK_CASES["hold_resume_after_funding_vanishes"](self.world())
        self.assertEqual(obs["held"], "HOLD")
        self.assertTrue(obs["authorization_kept"])
        self.assertEqual(obs["resumed"], ["FUNDING_SEEN", "FUNDING_SEEN"])
        self.assertIn("HOLD->FUNDING_SEEN", obs["history"])
        self.assertEqual(obs["rail_calls"], 0)

    def test_recovery_submit_after_the_funding_vanished_sends_nothing(self):
        obs = RECHECK_CASES["recovery_submit_after_funding_vanishes"](self.world())
        self.assertEqual(obs["recovered"], "HOLD")
        self.assertFalse(obs["rail_attempt_written"])
        self.assertEqual(obs["authorize_after"], "FUNDING_SEEN")
        self.assertEqual(obs["rail_calls"], 0)

    def test_funding_reconfirmed_in_a_new_block_is_paid_once_under_the_new_acceptance(self):
        obs = RECHECK_CASES["reconfirmed_in_new_block"](self.world())
        self.assertEqual(obs["steps"], ["HOLD", "FUNDING_SEEN", "FUNDING_ACCEPTED", "PAYOUT_SUBMITTED"])
        self.assertNotEqual(obs["new_block"], obs["old_block"])
        self.assertNotEqual(obs["authorized_digest"], obs["old_digest"])
        self.assertEqual(obs["authorized_digest"], obs["current_digest"])
        self.assertTrue(obs["same_idempotency_key"])
        self.assertEqual(obs["rail_calls"], 1)

    def test_a_begun_rail_attempt_is_never_resent_by_the_funding_recheck(self):
        obs = RECHECK_CASES["attempt_begun_is_never_resent"](self.world())
        self.assertEqual(obs["retries"], ["PAYOUT_SUBMITTED"] * 3)
        self.assertEqual(obs["after_recover"], "PAYOUT_SUBMITTED")
        self.assertFalse(obs["authorization_rebound"])
        self.assertEqual(obs["crashed_after_submit_recovered"], "PAYOUT_UNKNOWN")
        self.assertEqual(obs["rail_calls"], [1, 1])


class FixtureRecheckTests(RecheckCases, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.w = CashoutWorld(self.temp.name)
        self.w.enroll()

    def world(self):
        return self.w

    def test_submit_holds_an_authorization_that_binds_a_stale_acceptance(self):
        quote, _ = accepted(self.w)
        qid = quote.quote_id()
        self.assertEqual(self.w.provider.authorize(qid, now=self.w.now), State.PAYOUT_AUTHORIZED)
        with immediate(self.w.provider.ledger.database) as db:  # the acceptance moved on; the authorization did not
            db.execute("UPDATE quotes SET acceptance_digest=? WHERE quote_id=?", ("ee" * 32, qid))
        self.assertEqual(self.w.provider.submit(qid, now=self.w.now), State.HOLD)
        self.assertEqual(calls_for(self.w, quote), 0)

    def test_an_authorization_with_a_rail_attempt_is_final(self):
        quote, _ = accepted(self.w)
        qid = quote.quote_id()
        self.assertEqual(self.w.provider.pay(qid, now=self.w.now), State.PAYOUT_SUBMITTED)
        ledger = self.w.provider.ledger
        auth = ledger.authorization(qid)
        self.assertEqual(ledger.authorize(replace(auth, alias_seq=auth.alias_seq + 1), now=self.w.now)[1], False)
        with immediate(ledger.database) as db:  # even from a corrupted state, the begun attempt guards the key
            db.execute("UPDATE quotes SET state='HOLD' WHERE quote_id=?", (qid,))
        with self.assertRaises(DuplicatePayout):
            ledger.authorize(replace(auth, alias_seq=auth.alias_seq + 1), now=self.w.now)
        self.assertEqual(ledger.authorization(qid), auth)
        self.assertEqual(calls_for(self.w, quote), 1)


@unittest.skipUnless(REGTEST, "set BAN_REGTEST=1 (and BAN_BITCOIND if bitcoind is not on PATH) to run on a private "
                              "throwaway regtest node")
class RegtestRecheckTests(RecheckCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = RegtestNode()
        cls.addClassCleanup(cls.node.close)
        cls.node.start()
        cls.node.setup_wallet(mature_blocks=110)
        cls.root = Path(tempfile.mkdtemp(prefix="ban-regtest-worlds-", dir=cls.node.root))
        cls.addClassCleanup(shutil.rmtree, cls.root, ignore_errors=True)

    def world(self):
        return case_world(self.node, FIXTURE, self.root, self._testMethodName)


if __name__ == "__main__":
    unittest.main()
