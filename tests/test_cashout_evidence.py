from dataclasses import replace
from decimal import Decimal
import os
import tempfile
import unittest

from ban.evidence import (CoreRpcEvidence, EvidenceRejected, FixtureEvidence, FundingClaim, InsufficientConfirmations,
                          PolicyRejected, ReplayedOutput, StaleChain, UntrustedAssertion, VerifierUnavailable,
                          WrongOutput, admit_third_party, load_fixture_observation, synthetic_block_hash)
from ban.intent import EvidencePolicyV1
from ban.sim.ledger import IllegalTransition, State
from ban.sim.provider import ProviderRefused
from ban.sim.world import CashoutWorld

PROGRAM = {"pay_vkey": "0x00de861a80542b3089fed26b37ac4e1f4bb8664dcddadb3a6de5ae7205751e75",
           "node_vkey": "n" * 8, "leaf_vkey": "l" * 8, "segment_vkey": "s" * 8,
           "groth16_vk_sha256": "a" * 64, "verifier_sha256": "b" * 64}


def approval(state):
    return {"state": state, "by": "SYNTHETIC_FIXTURE", "at": 1}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = CashoutWorld(self.temp.name)
        self.w.enroll()
        self.intent, self.quote, self.sig = self.w.quote()
        self.qid = self.quote.quote_id()
        self.evidence = FixtureEvidence(self.w.chain, self.w.policy)

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, claim=None, evidence=None, quote=None):
        return (evidence or self.evidence).accept(quote or self.quote, now=self.w.now, claim=claim)

    def test_honest_funding_is_accepted_with_cores_confirmation_count(self):
        claim = self.w.fund(self.quote)
        self.w.advance(blocks=1)
        acceptance = self.accept(claim)
        self.assertEqual((acceptance.confirmations, acceptance.vout, acceptance.value), (2, 0, self.quote.sats_required))
        self.assertEqual(self.accept().digest(), acceptance.digest())  # scan by script finds the same output

    def test_wrong_output_script_vout_and_value(self):
        other = self.w.fund(self.quote, script="0014" + "33" * 20, tag="other-script")
        self.w.advance(blocks=2)
        with self.assertRaisesRegex(WrongOutput, "deposit script"):
            self.accept(other)
        paid = self.w.fund(self.quote, vout=1, tag="vout")
        self.w.advance(blocks=2)
        with self.assertRaisesRegex(WrongOutput, "deposit script"):
            self.accept(FundingClaim(paid.txid, 0))  # vout 0 pays someone else
        with self.assertRaisesRegex(WrongOutput, "vout"):
            self.accept(FundingClaim(paid.txid, 5))
        self.accept(paid)
        short = self.w.fund(self.quote, value=self.quote.sats_required - 1, tag="short")
        self.w.advance(blocks=2)
        with self.assertRaisesRegex(WrongOutput, "below sats_required"):
            self.accept(short)

    def test_funding_below_quote_created_height_is_a_replayed_old_output(self):
        old = self.w.fund(self.quote, height=self.quote.quote_created_height - 50, tag="old")
        with self.assertRaises(ReplayedOutput):
            self.accept(old)

    def test_funding_after_expiry_is_late_and_never_paid_at_the_old_rate(self):
        self.w.advance(blocks=self.quote.quote_expiry_height - self.w.chain.tip_height)
        self.w.fund(self.quote)
        self.w.advance(blocks=2)
        self.assertEqual(self.w.provider.on_funding(self.qid, now=self.w.now), State.LATE_FUNDING)
        with self.assertRaises((ProviderRefused, IllegalTransition)):
            self.w.provider.authorize(self.qid, now=self.w.now)
        self.assertIsNone(self.w.provider.ledger.authorization(self.qid))
        self.assertEqual(self.w.rail_mock.submit_calls, [])

    def test_funding_seen_after_the_expiry_sweep_is_late(self):
        self.w.advance(seconds=self.quote.quote_expiry_unix - self.w.now)
        self.assertEqual(self.w.provider.expire(now=self.w.now, tip_height=self.w.chain.tip_height), [self.qid])
        self.assertEqual(self.w.provider.ledger.held_minor(), 0)
        self.w.fund(self.quote)
        self.w.advance(blocks=2)
        self.assertEqual(self.w.provider.on_funding(self.qid, now=self.w.now), State.LATE_FUNDING)

    def test_funding_at_the_expiry_height_is_in_time_whichever_runs_first(self):
        for sweep_first in (True, False):
            with self.subTest(sweep_first=sweep_first):
                _, quote, _ = self.w.quote()
                qid = quote.quote_id()
                self.w.fund(quote, height=quote.quote_expiry_height)
                self.w.chain.tip_height = quote.quote_expiry_height + 1
                if sweep_first:
                    self.assertNotIn(qid, self.w.provider.expire(now=self.w.now, tip_height=self.w.chain.tip_height))
                else:
                    self.w.provider.on_funding(qid, now=self.w.now)
                self.assertEqual(self.w.provider.ledger.get(qid).state, State.FUNDING_ACCEPTED)
                self.w.chain.tip_height = self.w.cfg["tip_height"]

    def test_stale_chain_tip_lag_and_off_active_chain(self):
        claim = self.w.fund(self.quote)
        self.w.advance(blocks=4)
        lagging = FixtureEvidence(self.w.chain, self.w.policy, evidence_tip_height=self.w.chain.tip_height - 3)
        with self.assertRaisesRegex(StaleChain, "max_tip_lag"):
            self.accept(claim, evidence=lagging)
        forked = self.w.fund(self.quote, height=self.w.chain.tip_height - 1, branch="fork", tag="fork")
        with self.assertRaisesRegex(StaleChain, "active chain"):
            self.accept(forked)
        self.accept(claim)

    def test_core_confirmations_below_min_conf(self):
        claim = self.w.fund(self.quote)
        with self.assertRaises(InsufficientConfirmations):
            self.accept(claim)
        self.assertEqual(self.w.provider.on_funding(self.qid, now=self.w.now, claim=claim), State.FUNDING_SEEN)
        self.w.advance(blocks=1)
        self.assertEqual(self.w.provider.on_funding(self.qid, now=self.w.now, claim=claim), State.FUNDING_ACCEPTED)

    def test_reorg_before_authorization_returns_to_funding_seen(self):
        claim = self.w.fund(self.quote)
        self.w.advance(blocks=1)
        self.assertEqual(self.w.provider.on_funding(self.qid, now=self.w.now), State.FUNDING_ACCEPTED)
        self.w.chain.tip_height -= 1  # the confirming block is reorged out: 1 confirmation, below min_conf
        self.assertEqual(self.w.provider.authorize(self.qid, now=self.w.now), State.FUNDING_SEEN)
        self.w.chain.tip_height += 1
        self.assertEqual(self.w.provider.on_funding(self.qid, now=self.w.now), State.FUNDING_ACCEPTED)
        tx = self.w.chain.txs[claim.txid]
        tx.block_hash = synthetic_block_hash(tx.block_height, "fork")  # the funding block itself leaves the chain
        self.assertEqual(self.w.provider.pay(self.qid, now=self.w.now), State.FUNDING_SEEN)
        self.assertIsNone(self.w.provider.ledger.authorization(self.qid))
        self.assertEqual(self.w.rail_mock.submit_calls, [])
        tx.block_hash = synthetic_block_hash(tx.block_height)
        self.assertEqual(self.w.provider.on_funding(self.qid, now=self.w.now), State.FUNDING_ACCEPTED)
        self.assertEqual(self.w.provider.pay(self.qid, now=self.w.now), State.PAYOUT_SUBMITTED)
        self.assertEqual(len(self.w.rail_mock.submit_calls), 1)
        edges = self.w.provider.ledger.history(self.qid)
        self.assertEqual(edges.count(("FUNDING_ACCEPTED", "FUNDING_SEEN")), 2)

    def test_verified_true_without_a_configured_verifier_is_rejected(self):
        policy = EvidencePolicyV1("test", "regtest", {"sp1_btc_payment_v1": dict(PROGRAM, approval=approval("candidate"))})
        quote = replace(self.quote, evidence_policy_digest=policy.digest())
        for flag in ("verified", "proof_verified"):
            with self.subTest(flag=flag), self.assertRaises(UntrustedAssertion):
                admit_third_party(policy, quote, {"profile": "sp1_btc_payment_v1", "program": PROGRAM, flag: True})
        with self.assertRaises(VerifierUnavailable):
            admit_third_party(policy, quote, {"profile": "sp1_btc_payment_v1", "program": PROGRAM})
        obs = {"txid": "00" * 32, "vout": 0, "value": 1, "script_hex": "", "block_hash": "00" * 32, "block_height": 1,
               "block_time": 1, "on_active_chain": True, "node_tip_height": 1, "node_tip_hash": "00" * 32,
               "evidence_tip_height": 1, "verified": True}
        with self.assertRaises(UntrustedAssertion):
            load_fixture_observation(obs)

    def test_candidate_keys_under_production_mode_are_rejected(self):
        prod = EvidencePolicyV1("production", "regtest", {
            "sp1_btc_payment_v1": dict(PROGRAM, approval=approval("candidate")),
            "own_core_v1": {"approval": approval("candidate"), "chain": "regtest", "min_conf": 1}})
        quote = replace(self.quote, evidence_policy_digest=prod.digest())
        with self.assertRaisesRegex(PolicyRejected, "production"):
            admit_third_party(prod, quote, {"profile": "sp1_btc_payment_v1", "program": PROGRAM})
        claim = self.w.fund(self.quote)
        self.w.advance(blocks=2)
        with self.assertRaisesRegex(PolicyRejected, "production"):
            FixtureEvidence(self.w.chain, prod).accept(quote, now=self.w.now, claim=claim)

    def test_unpinned_program_words_are_rejected(self):
        policy = EvidencePolicyV1("test", "regtest", {"sp1_btc_payment_v1": dict(PROGRAM, approval=approval("approved"))})
        quote = replace(self.quote, evidence_policy_digest=policy.digest())
        with self.assertRaisesRegex(PolicyRejected, "pinned"):
            admit_third_party(policy, quote, {"profile": "sp1_btc_payment_v1", "program": dict(PROGRAM, leaf_vkey="x")})
        unpinned = EvidencePolicyV1("test", "regtest", {"sp1_btc_payment_v1": dict(PROGRAM, segment_vkey="",
                                                                                   approval=approval("approved"))})
        quote = replace(self.quote, evidence_policy_digest=unpinned.digest())
        with self.assertRaisesRegex(PolicyRejected, "pinned"):
            admit_third_party(unpinned, quote, {"profile": "sp1_btc_payment_v1", "program": dict(PROGRAM, segment_vkey="")})
        cashu = EvidencePolicyV1("test", "regtest", {"cashu_bitvm_claim_v1": {"approval": approval("approved")}})
        with self.assertRaisesRegex(PolicyRejected, "reserved"):
            admit_third_party(cashu, replace(self.quote, evidence_policy_digest=cashu.digest()),
                              {"profile": "cashu_bitvm_claim_v1"})

    def test_quote_must_name_the_policy_in_force(self):
        claim = self.w.fund(self.quote)
        self.w.advance(blocks=2)
        other = EvidencePolicyV1("test", "regtest", {"own_core_v1": {"approval": approval("approved"),
                                                                    "chain": "regtest", "min_conf": 6}})
        with self.assertRaisesRegex(PolicyRejected, "different evidence policy"):
            FixtureEvidence(self.w.chain, other).accept(self.quote, now=self.w.now, claim=claim)

    def test_core_rpc_evidence_is_loopback_only(self):
        for url in ("http://192.0.2.1:18443", "https://127.0.0.1:18443", "http://user:pw@127.0.0.1:18443",
                    "http://127.0.0.1", "http://example.test:18443", "http://127.0.0.1:18443/?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                CoreRpcEvidence(url, self.w.policy)
        CoreRpcEvidence("http://127.0.0.1:18443", self.w.policy)
        CoreRpcEvidence("http://[::1]:18443", self.w.policy)

    def fake_core(self, *, reorg=False, tip_moves=False):
        quote, sats = self.quote, self.quote.sats_required
        height, tip = 201, 203
        block = "aa" * 32
        replies = {
            "scantxoutset": {"bestblock": "ff" * 32, "unspents": [{"txid": "cc" * 32, "vout": 0, "height": height,
                                                                     "amount": Decimal(sats).scaleb(-8)}]},
            "getblockchaininfo": {"chain": "regtest", "blocks": tip, "bestblockhash": "ee" * 32 if tip_moves else "ff" * 32},
            "getblockhash": block,
            "getrawtransaction": {"vout": [{"value": Decimal(sats).scaleb(-8),
                                            "scriptPubKey": {"hex": quote.deposit_script}}]},
            "getblockheader": {"confirmations": tip - height + 1, "time": 1},
            "getbestblockhash": "ff" * 32,
        }

        class FakeCore(CoreRpcEvidence):
            def _call(self, method, params):
                if method == "getrawtransaction" and reorg:
                    raise EvidenceRejected("Core RPC error: No such transaction found in the provided block")
                return replies[method]
        return FakeCore("http://127.0.0.1:18443", self.w.policy)

    def test_core_rpc_evidence_proves_inclusion_and_a_stable_tip(self):
        self.assertEqual(self.fake_core().accept(self.quote, now=self.w.now).confirmations, 3)
        with self.assertRaisesRegex(StaleChain, "not in the active-chain block"):
            self.fake_core(reorg=True).accept(self.quote, now=self.w.now)
        with self.assertRaisesRegex(StaleChain, "tip moved"):
            self.fake_core(tip_moves=True).accept(self.quote, now=self.w.now)

    @unittest.skipUnless(os.getenv("BAN_REGTEST_RPC_URL"), "set BAN_REGTEST_RPC_URL to a user-run loopback regtest node")
    def test_core_rpc_evidence_against_a_live_regtest_node(self):
        core = CoreRpcEvidence(os.environ["BAN_REGTEST_RPC_URL"], self.w.policy,
                               cookie_path=os.getenv("BAN_REGTEST_RPC_COOKIE"))
        with self.assertRaisesRegex(WrongOutput, "no unspent output"):
            core.accept(self.quote, now=self.w.now)  # a fresh deposit script nobody paid


if __name__ == "__main__":
    unittest.main()
