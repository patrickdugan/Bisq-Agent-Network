"""Regtest funding evidence (spec G.5).

``ChainGuardTests`` and ``CoreRpcTransportTests`` run everywhere. ``RegtestFundingTests``
start a private throwaway ``bitcoind -regtest`` and run only with ``BAN_REGTEST=1``
(set ``BAN_BITCOIND`` if bitcoind is not on PATH, and ``BAN_REGTEST_ROOT`` to choose
where the datadir is made). The node is stopped and its datadir deleted afterwards.
"""
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

from ban import cli
from ban.arena_contract import parse_arena_line
from ban.evidence import CoreRpcEvidence, EvidenceRejected, StaleChain
from ban.intent import EvidencePolicyV1
from ban.sim.regtest import (READ_ONLY, REGTEST_CASES, REGTEST_GENESIS, NotRegtest, RegtestNode, RegtestRpc,
                             RegtestWorld, case_world)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "cashout_demo.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
REGTEST = os.getenv("BAN_REGTEST") == "1"
POLICY = EvidencePolicyV1("test", "regtest", {"own_core_v1": {
    "approval": {"state": "approved", "by": "SYNTHETIC_FIXTURE", "at": 1}, "chain": "regtest", "min_conf": 1}})
OTHER_GENESIS = {
    "main": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
    "test": "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943",
    "testnet4": "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043",
    "signet": "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6",
}


class FakeRpc(RegtestRpc):
    """Records every method sent; reports the chain the test chooses."""

    def __init__(self, chain="regtest", genesis=REGTEST_GENESIS):
        super().__init__("http://127.0.0.1:1", None)
        self.chain, self.genesis, self.sent = chain, genesis, []

    def _raw(self, method, params=None, *, wallet=None):
        self.sent.append(method)
        replies = {"getblockchaininfo": {"chain": self.chain}, "getblockhash": self.genesis,
                   "decodescript": {"address": "bcrt1p-fake"}, "sendtoaddress": "aa" * 32, "bumpfee": {"txid": "bb" * 32},
                   "gettransaction": {"decoded": {"vout": [{"n": 0, "scriptPubKey": {"hex": "5120" + "11" * 32}}]}}}
        return replies.get(method, "ok")


def guarded_actions(rpc):
    return {"createwallet": rpc.setup_wallet, "mine": lambda: rpc.mine(1),
            "pay": lambda: rpc.pay("5120" + "11" * 32, 1000), "bumpfee": lambda: rpc.bump("aa" * 32),
            "invalidateblock": lambda: rpc.call("invalidateblock", ["aa" * 32]), "stop": lambda: rpc.call("stop"),
            "unlisted": lambda: rpc.call("someunlistedmethod")}


class ChainGuardTests(unittest.TestCase):
    def test_wallet_mining_and_broadcast_refuse_any_node_that_is_not_regtest(self):
        cases = list(OTHER_GENESIS.items())
        cases += [("regtest", OTHER_GENESIS["main"]), ("main", REGTEST_GENESIS), ("regtest", "00" * 32)]
        for chain, genesis in cases:
            for name in guarded_actions(FakeRpc()):
                rpc = FakeRpc(chain, genesis)
                with self.subTest(chain=chain, genesis=genesis[:8], action=name):
                    with self.assertRaises(NotRegtest):
                        guarded_actions(rpc)[name]()
                    self.assertLessEqual(set(rpc.sent), READ_ONLY, "a state-changing call reached a non-regtest node")

    def test_on_regtest_every_state_changing_call_follows_a_fresh_guard(self):
        rpc = FakeRpc()
        for action in guarded_actions(rpc).values():
            action()
        changing = [i for i, method in enumerate(rpc.sent) if method not in READ_ONLY]
        self.assertGreaterEqual(len(changing), 8)
        for i in changing:
            self.assertEqual(rpc.sent[i - 2:i], ["getblockchaininfo", "getblockhash"], rpc.sent[i])

    def test_node_command_is_regtest_loopback_and_offline(self):
        node = RegtestNode(sys.executable, root=tempfile.gettempdir())  # never started: command() only
        node.datadir, node.port = Path("never-created"), 18999
        command = node.command()
        self.assertIn("-regtest", command)
        for arg in ("-rpcbind=127.0.0.1", "-rpcallowip=127.0.0.1", "-rpcport=18999", "-listen=0", "-connect=0",
                    "-dnsseed=0", "-datadir=never-created"):
            self.assertIn(arg, command)
        self.assertFalse([a for a in command if a.startswith(("-testnet", "-signet", "-chain", "-main"))])

    def test_regtest_world_refuses_other_networks(self):
        with self.assertRaises(ValueError):
            RegtestWorld(Path(tempfile.gettempdir()) / "unused", FakeRpc(), {"network": "signet"})


class _Reply(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        status, body = self.server.reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CoreRpcTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Reply)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d" % self.server.server_port

    def test_core_rpc_errors_arrive_as_http_500_and_are_evidence_rejections(self):
        core = CoreRpcEvidence(self.url, POLICY)
        self.server.reply = (500, b'{"result":null,"error":{"code":-5,"message":"No such transaction found in the '
                                  b'provided block."},"id":"ban"}')  # Bitcoin Core 31.1's actual reply
        with self.assertRaisesRegex(EvidenceRejected, "No such transaction"):
            core._call("getrawtransaction", ["00" * 32, True, "11" * 32])
        self.server.reply = (401, b"")  # wrong cookie: a node problem, not a rejection
        with self.assertRaises(urllib.error.HTTPError):
            core._call("getblockchaininfo", [])

    def test_core_rpc_never_goes_through_a_proxy(self):
        self.server.reply = (200, b'{"result":7,"error":null,"id":"ban"}')
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://192.0.2.1:9", "http_proxy": "http://192.0.2.1:9",
                                          "NO_PROXY": "", "no_proxy": ""}):
            self.assertEqual(CoreRpcEvidence(self.url, POLICY, timeout=5)._call("getblockcount", []), 7)


@unittest.skipUnless(REGTEST, "set BAN_REGTEST=1 (and BAN_BITCOIND if bitcoind is not on PATH) to run on a private "
                              "throwaway regtest node")
class RegtestFundingTests(unittest.TestCase):
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

    def only_row(self, w):
        [row] = w.provider.ledger.all_rows()
        return row

    def test_wallet_funding_is_accepted_by_core_rpc_evidence_at_min_conf(self):
        w = self.world()
        obs = REGTEST_CASES["accepted_at_min_conf"](w)
        self.assertEqual((obs["at_1_conf"], obs["at_min_conf"]), ("FUNDING_SEEN", "FUNDING_ACCEPTED"))
        self.assertEqual((obs["value"], obs["confirmations"], obs["min_conf"]), (obs["sats_required"], 2, 2))
        self.assertEqual((obs["evidence_profile"], obs["rail_calls"]), ("own_core_v1", 1))
        row = self.only_row(w)
        self.assertEqual(row.outpoint, obs["outpoint"])
        self.assertGreaterEqual(w.node.wallet_tx(row.outpoint.split(":")[0])["confirmations"], 2)

    def test_underpay_is_a_wrong_output_and_binds_nothing(self):
        w = self.world()
        obs = REGTEST_CASES["underpay"](w)
        self.assertEqual(obs["paid_sats"], obs["sats_required"] - 1)
        self.assertIn("WrongOutput: output value below sats_required", obs["refusal"])
        self.assertEqual((obs["state"], obs["rail_calls"]), ("QUOTED", 0))
        self.assertIsNone(self.only_row(w).outpoint)

    def test_overpay_is_accepted_and_pays_only_the_quoted_net(self):
        w = self.world()
        obs = REGTEST_CASES["overpay"](w)
        self.assertEqual(obs["state"], "FUNDING_ACCEPTED")
        self.assertEqual(obs["accepted_value"], obs["sats_required"] + 25_000)
        self.assertEqual(obs["rail_amounts_minor"], [obs["net_payout_minor"]])
        self.assertEqual(obs["overpay_policy"], "refund_excess")

    def test_funding_mined_above_quote_expiry_height_is_late_funding(self):
        w = self.world()
        obs = REGTEST_CASES["late_funding"](w)
        self.assertEqual(obs["funding_height"], obs["quote_expiry_height"] + 1)
        self.assertEqual(obs["state"], "LATE_FUNDING")
        self.assertIn("cannot authorize from LATE_FUNDING", obs["authorize"])
        self.assertEqual(obs["rail_calls"], 0)
        self.assertEqual(obs["at_expiry_height"], "FUNDING_ACCEPTED")  # the boundary block is in time

    def test_prefunded_script_below_quote_created_height_is_a_replayed_output(self):
        w = self.world()
        obs = REGTEST_CASES["replayed_output"](w)
        self.assertLess(obs["funding_height"], obs["quote_created_height"])
        self.assertIn("ReplayedOutput: funding confirmed below quote_created_height", obs["refusal"])
        self.assertEqual(obs["state"], "QUOTED")

    def test_invalidateblock_below_min_conf_before_authorization_returns_to_funding_seen(self):
        w = self.world()
        obs = REGTEST_CASES["reorg_below_min_conf"](w)
        self.assertEqual(obs["confirmations_after_reorg"], 1)
        self.assertEqual(obs["authorize_after_reorg"], "FUNDING_SEEN")
        self.assertFalse(obs["authorized_while_reorged"])
        self.assertIn("FUNDING_ACCEPTED->FUNDING_SEEN", obs["history"])
        self.assertEqual((obs["after_reconsiderblock"], obs["rail_calls"]), ("FUNDING_ACCEPTED", 1))

    def test_invalidateblock_of_the_funding_block_returns_to_funding_seen(self):
        w = self.world()
        obs = REGTEST_CASES["reorg_orphans_funding_block"](w)
        self.assertTrue(obs["back_in_mempool"])
        self.assertEqual(obs["authorize_after_reorg"], "FUNDING_SEEN")
        self.assertFalse(obs["authorized_while_reorged"])
        self.assertNotEqual(obs["reconfirmed_in"], obs["invalidated"])
        self.assertEqual((obs["after_reconfirm"], obs["rail_calls"]), ("FUNDING_ACCEPTED", 1))

    def test_rbf_replacement_paying_the_same_script_is_accepted_exactly_once(self):
        w = self.world()
        obs = REGTEST_CASES["rbf_replacement"](w)
        self.assertNotEqual(obs["original"].split(":")[0], obs["replacement"].split(":")[0])
        self.assertIn("WrongOutput", obs["while_unconfirmed"])
        self.assertEqual(obs["states"], ["FUNDING_ACCEPTED"] * 3)
        self.assertEqual(obs["acceptances"], 1)
        self.assertEqual(obs["bound_outpoint"], obs["replacement"])
        self.assertEqual(obs["unspents_paying_script"], 1)
        self.assertLess(obs["original_confirmations"], 1)
        self.assertIn("WrongOutput", obs["original_claim"])
        self.assertEqual(obs["rail_calls"], 1)

    def test_same_outpoint_offered_for_a_second_quote_is_rejected(self):
        w = self.world()
        obs = REGTEST_CASES["outpoint_for_two_quotes"](w)
        self.assertEqual(obs["first"], "FUNDING_ACCEPTED")
        self.assertIn("WrongOutput: node shows no unspent output", obs["second_by_evidence"])
        self.assertIn("ReplayRejected: funding outpoint already bound to another quote (I2)", obs["second_by_ledger"])
        self.assertEqual(obs["second_state"], "QUOTED")

    def test_real_core_rpc_errors_are_evidence_rejections(self):
        core = CoreRpcEvidence(self.node.url, POLICY, cookie_path=str(self.node.cookie_path))
        with self.assertRaisesRegex(EvidenceRejected, "No such transaction found in the provided block"):
            core._call("getrawtransaction", ["00" * 32, True, self.node.call("getbestblockhash")])

    def test_reorg_between_the_scan_and_the_block_reads_is_stale_chain(self):
        w = self.world()
        _, quote, _ = w.quote()
        claim = w.fund(quote)
        w.advance(blocks=quote.min_conf - 1)
        funding_block = w.node.wallet_tx(claim.txid)["blockhash"]
        node = self.node
        for hook in ("scantxoutset", "getblockhash", "getrawtransaction"):
            class Racing(CoreRpcEvidence):
                def _call(self, method, params):
                    result = super()._call(method, params)
                    if method == hook:  # the funding block leaves the active chain mid-read
                        node.call("invalidateblock", [funding_block])
                    return result
            with self.subTest(reorg_after=hook):
                with self.assertRaises(StaleChain):
                    Racing(node.url, w.policy, cookie_path=str(node.cookie_path)).accept(quote, now=w.now, claim=claim)
                node.call("reconsiderblock", [funding_block])
                self.assertEqual(w.provider.evidence.accept(quote, now=w.now, claim=claim).block_hash, funding_block)

    def test_cli_regtest_demo_runs_end_to_end_and_deletes_its_node(self):
        out = io.StringIO()
        arena = self.root / "arena_cashout_regtest.jsonl"
        argv = ["ban", "cashout-demo", "--regtest", "--fixture", str(FIXTURE_PATH), "--arena-out", str(arena)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            cli.main()
        result = json.loads(out.getvalue())
        self.assertEqual([s["step"] for s in result["transitions"]],
                         ["T0a", "T0b", "T0c", "T1", "select", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"])
        steps = {s["step"]: s for s in result["transitions"]}
        self.assertEqual((steps["T4"]["transition"], steps["T4"]["provider_state"]),
                         ("funding tx (regtest node)", "FUNDING_SEEN"))
        self.assertEqual(steps["T5"]["provider_state"], "FUNDING_ACCEPTED")
        self.assertEqual(steps["T9"]["result"], "RECONCILED")
        self.assertEqual([c["case"] for c in result["cases"]], list(REGTEST_CASES))
        self.assertTrue(result["all_cases_held"], [c for c in result["cases"] if not c["held"]])
        self.assertEqual(result["chain"]["network"], "regtest")
        self.assertTrue(result["chain"]["datadir_removed"])
        self.assertFalse(Path(result["chain"]["datadir"]).exists())
        self.assertTrue(result["arena"]["all_passed"])
        honest = parse_arena_line(arena.read_text(encoding="utf-8").splitlines()[0]).fields
        self.assertEqual((honest["scenario"], honest["passed"], honest["attack_success"],
                          honest["recipient_credited_minor"]), ("honest", True, False, 5000))
        self.assertNotIn(FIXTURE["alias"]["destination"], out.getvalue())


if __name__ == "__main__":
    unittest.main()
