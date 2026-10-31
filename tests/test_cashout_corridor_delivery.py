"""Full synthetic BTC/ILS/JawwalPay route, including settlement failure cases."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ban.cashout import cashout_shortlist
from ban.models import normalize_snapshot
from ban.sim.corridor_cli import simulate_mock_delivery
from ban.sim.ledger import State
from ban.sim.world import CashoutWorld, fixture_inventory

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cashout_demo.json"
FX = json.loads(FIXTURE.read_text(encoding="utf-8"))
TARGET = 8750  # synthetic $25 * 3.50 ILS/USD


class CorridorDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = CashoutWorld(self.temp.name, FX)
        self.w.enroll()
        self.resolution = self.w.resolve()
        self.intent, self.intent_sig = self.w.intent(
            self.resolution, amount_mode="receive_exact",
            amount=TARGET, min_net_payout_minor=TARGET,
        )
        snapshot = normalize_snapshot(FX["offerbook"], "ILS", "SYNTHETIC_FIXTURE")
        inventory = fixture_inventory(FX["inventory"], FX["keys"], now=self.w.now,
                                      rail=self.w.rail, currency=self.w.currency,
                                      exponent=self.w.exponent)
        self.selection = cashout_shortlist(
            snapshot, self.intent, inventory, providers=[self.w.p1.pubkey],
            now=self.w.now,
        )

    def tearDown(self):
        self.temp.cleanup()

    def settle(self, selection=None):
        return simulate_mock_delivery(self.w, self.intent, self.intent_sig,
                                      self.resolution, selection or self.selection)

    def test_full_mock_delivery_has_one_credit_receipt_and_reconciliation(self):
        result = self.settle()
        self.assertEqual(result["reconciled_state"], "RECONCILED")
        self.assertEqual(result["receipt_status"], "delivered")
        self.assertEqual(result["mock_rail_credit_minor"], TARGET)
        self.assertEqual(result["mock_rail_submissions"], 1)
        self.assertFalse(result["real_wallet_credited"])
        self.assertFalse(result["live_liquidity_verified"])
        self.assertEqual(self.w.provider.ledger.get(result["quote_id"]).state, State.RECONCILED)
        self.assertEqual(len(self.w.rail_mock.payouts), 1)
        self.assertEqual(self.w.provider.submit(result["quote_id"], now=self.w.now), State.RECONCILED)
        self.assertEqual(len(self.w.rail_mock.submit_calls), 1)
        self.assertEqual(self.w.provider.ledger.held_minor(), 0)
        self.assertNotIn(FX["alias"]["destination"], json.dumps(result))
        self.assertNotIn(self.w.target["commitment"], json.dumps(result))

    def test_wrong_provider_is_rejected_before_any_funding_or_payout(self):
        bad = dict(self.selection)
        bad["offers"] = [dict(self.selection["offers"][0], provider_pubkey="ab" * 32)]
        with self.assertRaisesRegex(ValueError, "selected liquidity"):
            self.settle(bad)
        self.assertEqual(self.w.provider.ledger.quote_count(), 0)
        self.assertEqual(len(self.w.rail_mock.submit_calls), 0)

    def test_no_buy_side_bid_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no eligible"):
            self.settle({"offers": []})
        self.assertEqual(len(self.w.rail_mock.submit_calls), 0)

    def test_failure_does_not_issue_a_receipt_or_claim_delivery(self):
        self.w.rail_mock.submit_script[:] = ["failed"]
        with self.assertRaisesRegex(RuntimeError, "mock rail submission failed"):
            self.settle()
        rows = self.w.provider.ledger.all_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, State.REFUND_DUE)
        self.assertEqual(len(self.w.rail_mock.submit_calls), 1)
        self.assertFalse(self.w.provider.ledger.attempt(rows[0].idempotency_key)["response_status"] == "delivered")

    def test_cli_prints_only_synthetic_credit_and_reconciliation(self):
        proc = subprocess.run(
            [sys.executable, "-m", "ban.sim.corridor_cli",
             "--fixture", str(FIXTURE), "--usd", "25.00",
             "--synthetic-ils-per-usd", "3.50", "--simulate-settlement"],
            cwd=FIXTURE.parent.parent,
            env={**os.environ, "PYTHONPATH": "src"},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "SIMULATION_PASS")
        self.assertEqual(report["mock_settlement"]["mock_rail_credit_minor"], TARGET)
        self.assertEqual(report["mock_settlement"]["reconciled_state"], "RECONCILED")
        self.assertEqual(report["mock_settlement"]["mock_rail_submissions"], 1)
        self.assertFalse(report["mock_settlement"]["real_wallet_credited"])
        self.assertFalse(report["payment_execution_enabled"])
        self.assertNotIn(FX["alias"]["destination"], proc.stdout)


if __name__ == "__main__":
    unittest.main()
