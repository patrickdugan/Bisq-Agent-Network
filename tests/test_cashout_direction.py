import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from ban.adapters import FixtureAdapter
from ban.cashout_select import cashout_shortlist
from ban.concierge import shortlist
from ban.intent import ContractError, price, rate_from_price
from ban.models import normalize_snapshot
from ban.policy import Decision, evaluate
from ban.sim.testkeys import TestKey
from ban.sim.world import CashoutWorld, fixture_inventory, signed_inventory

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "cashout_demo.json").read_text(encoding="utf-8"))


class CashoutDirectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.world = CashoutWorld(self.temp.name, FIXTURE)
        self.snapshot = normalize_snapshot(FIXTURE["offerbook"], "ILS", "SYNTHETIC_FIXTURE")
        self.intent, _ = self.world.intent()
        self.providers = [self.world.p1.pubkey]
        self.now = self.world.now

    def tearDown(self):
        self.temp.cleanup()

    def inventory(self, **overrides):
        entries = [dict(e, **overrides.get(e["offer_id"], {})) for e in FIXTURE["inventory"]]
        return fixture_inventory(entries, FIXTURE["keys"], now=self.now, rail="jawwal_pay", currency="ILS", exponent=2)

    def select(self, inventory=None, intent=None):
        return cashout_shortlist(self.snapshot, intent or self.intent, inventory or self.inventory(),
                                 providers=self.providers, now=self.now)

    def test_cashout_shortlist_never_returns_a_sell_maker(self):
        result = self.select()
        self.assertGreater(len(result["offers"]), 0)
        self.assertTrue(all(o["maker_direction"] == "BUY" for o in result["offers"]))
        # synthetic-ils-sell-001 even carries a validly signed P1 attestation; direction still excludes it.
        self.assertEqual(result["excluded"]["maker_sells_btc"], 2)
        self.assertEqual([o["offer_id"] for o in result["offers"]],
                         ["synthetic-ils-buy-p1-003", "synthetic-ils-buy-p1-006"])
        self.assertTrue(result["requires_human_trade_confirmation"])

    def test_buyer_shortlist_still_returns_only_sell_makers(self):
        php = FixtureAdapter().offers("PHP")
        for snapshot in (php, self.snapshot):
            result = shortlist(snapshot, "1000")
            self.assertGreater(result["matched_count"], 0)
            self.assertTrue(all(o["maker_direction"] == "SELL" for o in result["offers"]))

    def test_missing_inventory_is_excluded(self):
        inventory = self.inventory()
        del inventory["synthetic-ils-buy-p1-006"]
        result = self.select(inventory)
        self.assertEqual(result["excluded"]["missing_inventory"], 1)
        self.assertNotIn("synthetic-ils-buy-p1-006", [o["offer_id"] for o in result["offers"]])

    def test_stale_inventory_is_excluded(self):
        result = self.select()
        self.assertEqual(result["excluded"]["stale_inventory"], 1)  # synthetic-ils-buy-p1-005, an hour old
        future = self.inventory(**{"synthetic-ils-buy-p1-003": {"age_seconds": -120}})
        self.assertNotIn("synthetic-ils-buy-p1-003", [o["offer_id"] for o in self.select(future)["offers"]])

    def test_insufficient_inventory_is_excluded(self):
        thin = self.inventory(**{"synthetic-ils-buy-p1-003": {"available_minor": 4999},
                                 "synthetic-ils-buy-p1-006": {"max_per_payout_minor": 100}})
        result = self.select(thin)
        self.assertEqual(result["excluded"]["insufficient_inventory"], 2)
        self.assertEqual(result["offers"], [])

    def test_wrong_key_inventory_is_excluded(self):
        result = self.select()
        self.assertEqual(result["excluded"]["wrong_key_inventory"], 1)  # P2 is not in Alice's provider list
        inventory = self.inventory()
        good = inventory["synthetic-ils-buy-p1-003"]
        forged = signed_inventory(TestKey("sim/attacker"), **{
            k: getattr(good.attestation, k) for k in good.attestation.__dataclass_fields__ if k != "provider_pubkey"})
        inventory["synthetic-ils-buy-p1-003"] = forged
        self.assertEqual(self.select(inventory)["excluded"]["wrong_key_inventory"], 2)
        # A P1 signature over another offer's attestation does not transfer.
        inventory = self.inventory()
        inventory["synthetic-ils-buy-p1-003"] = inventory["synthetic-ils-buy-p1-006"]
        self.assertEqual(self.select(inventory)["excluded"]["wrong_key_inventory"], 2)
        # A valid key over a tampered amount fails the signature.
        inventory = self.inventory()
        att = inventory["synthetic-ils-buy-p1-003"]
        tampered = type(att)(type(att.attestation)(**dict(att.attestation.__dict__, available_minor=10**9)), att.sig)
        inventory["synthetic-ils-buy-p1-003"] = tampered
        self.assertEqual(self.select(inventory)["excluded"]["wrong_key_inventory"], 2)

    def test_receive_exact_and_send_exact_use_decimal_with_explicit_rounding(self):
        rate = rate_from_price(Decimal("380000.0000"), 2)
        self.assertEqual(rate, 38_000_000)
        self.assertEqual(rate_from_price(Decimal("380000.0099"), 2), 38_000_000)  # floor, never a payout unit
        recv = price("receive_exact", 5000, rate, 100)
        # gross = ceil(5000 * 10000 / 9900) = 5051; sats = ceil(5051e8 / 38e6) = 13293 (13292.1...)
        self.assertEqual((recv.gross_minor, recv.fee_minor, recv.net_payout_minor, recv.sats_required),
                         (5051, 51, 5000, 13293))
        self.assertEqual(recv.fee_sats, 135)  # ceil(51e8 / 38e6) = 134.2 -> 135
        send = price("send_exact", 13293, rate, 100)
        # gross = floor(13293 * 38e6 / 1e8) = 5051 (5051.34); fee = ceil(50.51) = 51; net = 5000
        self.assertEqual((send.gross_minor, send.fee_minor, send.net_payout_minor, send.sats_required),
                         (5051, 51, 5000, 13293))
        odd = price("send_exact", 13292, rate, 100)
        self.assertEqual((odd.gross_minor, odd.fee_minor, odd.net_payout_minor), (5050, 51, 4999))
        with self.assertRaises(ContractError):
            price("receive_exact", 5000, rate, 10000)
        with self.assertRaises(ContractError):
            price("pay_whatever", 5000, rate, 0)
        send_intent, _ = self.world.intent(amount_mode="send_exact", amount=13293, min_net_payout_minor=1)
        ranked = self.select(intent=send_intent)["offers"]
        self.assertEqual(ranked[0]["offer_id"], "synthetic-ils-buy-p1-003")
        self.assertGreater(ranked[0]["indicative_net_payout_minor"], ranked[1]["indicative_net_payout_minor"])

    def test_cashout_policy_actions_are_denied(self):
        for action in ("authorize_local_payout", "submit_local_payout"):
            self.assertEqual(evaluate(action).decision, Decision.DENY)
        for unknown in ("execute_cashout", "jawwal_payout", "release_local_payout"):
            self.assertEqual(evaluate(unknown).decision, Decision.DENY)
            self.assertIn("fail closed", evaluate(unknown).reason)


if __name__ == "__main__":
    unittest.main()
