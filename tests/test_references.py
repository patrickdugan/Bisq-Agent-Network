"""Read-only market reference and merchant quote safety regression tests."""
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from ban.adapters import FixtureAdapter
from ban.concierge import shortlist
from ban.merchant import draft_merchant_quote
from ban.price_contract import ContractViolation
from ban.references import decode_reference, coherent_reference, resolved_price

REF_FILE = Path(__file__).resolve().parents[1] / "fixtures" / "bisq2_PHP_quote.json"


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FixtureAdapter()
        self.snapshot = self.adapter.offers("PHP")
        self.reference = self.adapter.reference_price("PHP")

    def test_valid_reference_envelope(self):
        self.assertEqual(self.reference.fiat_per_btc, Decimal("4150000.0000"))
        self.assertFalse(self.reference.upstream_age_verified)
        self.assertIsNotNone(coherent_reference(self.snapshot, self.reference))

    def test_dynamic_markup_is_a_fraction(self):
        offer = next(o for o in self.snapshot.offers if o.pricing_type == "FloatPriceSpec")
        price, provenance = resolved_price(offer, self.reference)
        self.assertEqual(price, Decimal("4253750.0000"))
        self.assertEqual(provenance, "floating_derived_unverified_age")
        result = shortlist(self.snapshot, "1000", "GCash", self.reference)
        self.assertEqual(result["matched_count"], 2)
        self.assertTrue(any(x["price_provenance"] == provenance for x in result["offers"]))
        self.assertFalse(result["reference_upstream_age_verified"])

    def test_stale_reference_becomes_unknown(self):
        ref = replace(self.reference, observed_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
        self.assertIsNone(coherent_reference(self.snapshot, ref))
        match = shortlist(self.snapshot, "1000", "GCash", ref)
        dynamic = next(x for x in match["offers"] if x["pricing_type"] == "FloatPriceSpec")
        self.assertIsNone(dynamic["indicative_btc"])
        with self.assertRaises(ValueError):
            draft_merchant_quote(ref, margin_bps="150", fiat_amount="1000")

    def test_wrong_currency_reference_refused(self):
        ref = replace(self.reference, currency="USD")
        self.assertIsNone(coherent_reference(self.snapshot, ref))

    def test_missing_reference_or_corrupt_monetary_price(self):
        raw = json.loads(REF_FILE.read_text())
        raw["quotes"]["PHP"]["quoteSideMonetary"]["precision"] = 2
        with self.assertRaises(ContractViolation):
            decode_reference(raw, "PHP", "BISQ2_LOCAL")
        with self.assertRaises(ContractViolation):
            decode_reference({"quotes": {}}, "PHP", "BISQ2_LOCAL")

    def test_merchant_ask_quote_draft(self):
        draft = draft_merchant_quote(self.reference, margin_bps="150", fiat_amount="1000")
        self.assertEqual(draft["suggested_fiat_per_btc"], "4212250.0000")
        self.assertEqual(draft["approval_state"], "draft_only")
        self.assertEqual(draft["route"], "none")
        self.assertFalse(draft["upstream_age_verified"])

    def test_reject_invalid_merchant_spreads(self):
        for bad in ("-1", "2501", "NaN", "Infinity", "1.234", "bad"):
            with self.subTest(margin=bad):
                with self.assertRaises(ValueError):
                    draft_merchant_quote(self.reference, margin_bps=bad, fiat_amount="1000")


if __name__ == "__main__":
    unittest.main()
