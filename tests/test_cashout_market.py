"""Read-only BTC/ILS cashout market diagnostics, synthetic data only."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ban.cashout_market import inspect_ils_buy_orders
from ban.models import normalize_snapshot
from ban.references import MarketReference

ROOT = Path(__file__).resolve().parents[1]
RAW = json.loads((ROOT / "fixtures" / "cashout_demo.json").read_text(encoding="utf-8"))["offerbook"]


class ReadonlyCashoutMarketTests(unittest.TestCase):
    @staticmethod
    def snapshot(rows=None, currency="ILS", source="SYNTHETIC_FIXTURE"):
        return normalize_snapshot(rows if rows is not None else deepcopy(RAW), currency, source)

    def test_buy_side_count_and_never_executable(self):
        report = inspect_ils_buy_orders(self.snapshot(), "87.50")
        self.assertEqual(report["buy_side_advertised_offers"], 4)
        self.assertEqual(report["sell_side_excluded"], 2)
        self.assertEqual(report["jawwalpay_text_mentions_with_in_range_size"], 0)
        self.assertFalse(report["can_send_money"])
        self.assertEqual(report["executable_bids_verified"], 0)
        self.assertTrue(all(row["counterparty_acceptance_confirmed"] is False
                            for row in report["advertised_offers"]))
        self.assertTrue(all(row["third_party_beneficiary_permitted"] is False
                            for row in report["advertised_offers"]))
        self.assertFalse(report["upstream_offer_age_verified"])

    def test_jawwal_mention_is_hint_not_payout_approval(self):
        rows = deepcopy(RAW)
        row = next(row for row in rows if row["bisqEasyOffer"]["id"] == "synthetic-ils-buy-p1-003")
        row["quoteSidePaymentMethods"] = ["Jawwal Pay", "Bank transfer"]
        report = inspect_ils_buy_orders(self.snapshot(rows), "87.50")
        self.assertEqual(report["jawwalpay_text_mentions_with_in_range_size"], 1)
        self.assertTrue(report["advertised_offers"][0]["jawwalpay_text_mention"])
        self.assertFalse(report["advertised_offers"][0]["third_party_beneficiary_permitted"])
        self.assertEqual(report["executable_bids_verified"], 0)
        self.assertNotIn("destination", json.dumps(report))

    def test_sell_listing_cannot_inflate_jawwal_buy_count(self):
        rows = deepcopy(RAW)
        for row in rows:
            if row["bisqEasyOffer"]["direction"] == "SELL":
                row["quoteSidePaymentMethods"] = ["Jawwal Pay"]
        self.assertEqual(inspect_ils_buy_orders(self.snapshot(rows), "87.50")
                         ["jawwalpay_text_mentions_with_in_range_size"], 0)

    def test_btc_and_ils_ranges_are_just_advertised(self):
        rows = deepcopy(RAW)
        row = next(row for row in rows if row["bisqEasyOffer"]["id"] == "synthetic-ils-buy-p1-003")
        row["quoteSidePaymentMethods"] = ["JawwalPay"]
        report = inspect_ils_buy_orders(self.snapshot(rows), "2500.00")
        self.assertEqual(report["jawwalpay_text_mentions_with_in_range_size"], 0)
        self.assertEqual(report["advertised_offers"][0]["advertised_size_eligibility"],
                         "outside_advertised_bounds")

    def test_floating_offer_without_reference_is_unknown(self):
        rows = deepcopy(RAW)
        rows[2]["bisqEasyOffer"]["priceSpec"] = {"type": "MarketPriceSpec"}
        report = inspect_ils_buy_orders(self.snapshot(rows), "87.50")
        self.assertTrue(any(row["fiat_per_btc"] is None
                            and row["price_provenance"] == "reference_unavailable"
                            for row in report["advertised_offers"]))
        self.assertEqual(report["executable_bids_verified"], 0)

    def test_spoofed_live_source_still_cannot_approve_payout(self):
        report = inspect_ils_buy_orders(self.snapshot(source="BISQ2_LOCAL"), "87.50")
        self.assertEqual(report["source"], "BISQ2_LOCAL")
        self.assertFalse(report["can_send_money"])
        self.assertEqual(report["authorized_third_party_payout_providers_verified"], 0)

    def test_invalid_currency_and_ils_amounts_refused(self):
        with self.assertRaisesRegex(ValueError, "BTC/ILS"):
            inspect_ils_buy_orders(self.snapshot(currency="USD"), "87.50")
        for value in ("NaN", "Infinity", "-20", "0", "0.001", "12.345",
                      "9999999999999999", 25.0, "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                inspect_ils_buy_orders(self.snapshot(), value)

    def test_fixture_cli_uses_existing_read_only_adapter(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bisq2_ILS.json").write_text(json.dumps(RAW), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "ban.cli", "--source", "fixture",
                 "cashout-market", "--target-ils", "87.50"],
                cwd=ROOT, env={**os.environ, "PYTHONPATH": "src", "BAN_FIXTURES_DIR": d},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["source"], "SYNTHETIC_FIXTURE")
        self.assertEqual(report["buy_side_advertised_offers"], 4)
        self.assertEqual(report["executable_bids_verified"], 0)
        self.assertFalse(report["can_send_money"])


if __name__ == "__main__":
    unittest.main()
