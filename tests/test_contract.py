"""Contract regressions based on Bisq 2 PriceQuote and AmountSpec source."""
import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from ban.price_contract import ContractViolation, decode_price_quote, decode_amount_spec, float_markup
from ban.models import normalize_snapshot
from ban.concierge import shortlist


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "bisq2_PHP.json"


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.price = self.rows[0]["bisqEasyOffer"]["priceSpec"]["priceQuote"]

    def test_four_decimal_fiat_and_eight_decimal_btc(self):
        self.assertEqual(decode_price_quote(self.price, "PHP"), Decimal("4200000.0000"))
        bounds = decode_amount_spec(self.rows[0]["bisqEasyOffer"]["amountSpec"], "PHP")
        self.assertEqual(bounds.lower, Decimal("500.0000"))
        self.assertEqual(bounds.upper, Decimal("2500.0000"))
        self.assertEqual(bounds.contains(Decimal("2500"), None), "within_advertised_bounds")
        self.assertEqual(bounds.contains(Decimal("2500.01"), None), "outside_advertised_bounds")

    def test_reject_wrong_precision_and_mismatched_price_sides(self):
        for field, new_value in (("precision", 2), ("value", self.price["value"] * 100)):
            malformed = copy.deepcopy(self.price)
            malformed[field] = new_value
            with self.assertRaises(ContractViolation):
                decode_price_quote(malformed, "PHP")
        malformed = copy.deepcopy(self.price)
        malformed["quoteSideMonetary"]["code"] = "USD"
        with self.assertRaises(ContractViolation):
            decode_price_quote(malformed, "PHP")
        malformed = copy.deepcopy(self.price)
        malformed["baseSideMonetary"]["precision"] = 4
        with self.assertRaises(ContractViolation):
            decode_price_quote(malformed, "PHP")

    def test_range_validation(self):
        for spec in (
            {"type": "QuoteSideRangeAmountSpec", "minAmount": 200, "maxAmount": 100},
            {"type": "BaseSideFixedAmountSpec", "amount": 0},
            {"type": "Unknown", "amount": 500},
            {"type": "QuoteSideFixedAmountSpec", "amount": True},
        ):
            with self.assertRaises(ContractViolation):
                decode_amount_spec(spec, "PHP")

    def test_outside_advertised_range_excluded(self):
        snapshot = normalize_snapshot(self.rows, "PHP", "SYNTHETIC_FIXTURE")
        result = shortlist(snapshot, "3000", "GCash")
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["excluded_outside_advertised_bounds"], 1)
        self.assertEqual(result["offers"][0]["offer_id"], "synthetic-php-sell-dynamic-003")

    def test_deduplicate_offer_ids(self):
        snap = normalize_snapshot([self.rows[0], copy.deepcopy(self.rows[0])], "PHP", "TEST")
        self.assertEqual(len(snap.offers), 1)
        self.assertEqual(snap.rejected_rows, 1)

    def test_quote_side_fixed_spec(self):
        bounds = decode_amount_spec({"type": "QuoteSideFixedAmountSpec", "amount": 9000000}, "PHP")
        self.assertEqual(bounds.contains(Decimal("900"), None), "fixed_amount_price_check_required")
        self.assertEqual(bounds.contains(Decimal("901"), None), "outside_advertised_bounds")

    def test_base_side_sat_amounts(self):
        bounds = decode_amount_spec(
            {"type": "BaseSideRangeAmountSpec", "minAmount": 10000, "maxAmount": 250000}, "PHP"
        )
        self.assertEqual(bounds.lower, Decimal("0.00010000"))
        self.assertEqual(bounds.upper, Decimal("0.00250000"))
        self.assertEqual(bounds.contains(Decimal("1000"), Decimal("4200000")), "within_advertised_bounds")
        self.assertEqual(bounds.contains(Decimal("50000"), Decimal("4200000")), "outside_advertised_bounds")

    def test_markup_is_fraction_not_percentage_number(self):
        self.assertEqual(float_markup({"type": "FloatPriceSpec", "percentage": 0.025}), Decimal("0.025"))


if __name__ == "__main__":
    unittest.main()
