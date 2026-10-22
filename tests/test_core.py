import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from ban.adapters import AdapterError, Bisq2LocalAdapter, FixtureAdapter
from ban.concierge import shortlist
from ban.gtm import market_report, outreach_draft
from ban.ledger import metrics, record
from ban.models import InvalidData, fixed_price, normalize_snapshot, positive_decimal
from ban.policy import Decision, evaluate
from ban.proofs import PaymentClaim, PaymentVerifier, VerificationUnavailable


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = FixtureAdapter().offers("PHP")

    def test_fixture(self):
        self.assertEqual(self.snapshot.source, "SYNTHETIC_FIXTURE")
        self.assertEqual(len(self.snapshot.offers), 4)
        self.assertEqual(self.snapshot.rejected_rows, 0)

    def test_buyer_seller_direction_and_payment_filter(self):
        result = shortlist(self.snapshot, "1000", "GCash")
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(result["offers"][0]["offer_id"], "synthetic-php-sell-gcash-001")
        self.assertEqual(result["offers"][0]["indicative_btc"], "0.00023809")
        self.assertEqual(result["offers"][1]["indicative_btc"], None)
        self.assertTrue(result["requires_human_trade_confirmation"])

    def test_price_precision(self):
        spec = {"type": "FixPriceSpec", "priceQuote": {"value": 123456, "precision": 2,
                "market": {"baseCurrencyCode": "BTC", "quoteCurrencyCode": "PHP"}}}
        self.assertEqual(fixed_price(spec, "PHP"), Decimal("1234.56"))

    def test_invalid_quote_is_discarded(self):
        bad = self.snapshot.offers[0].as_dict()
        rows = [{"bisqEasyOffer": {"id": "fake", "direction": "SELL",
                "market": {"baseCurrencyCode": "BTC", "quoteCurrencyCode": "EUR"},
                "priceSpec": {"type": "MarketPriceSpec"}}}]
        snap = normalize_snapshot(rows, "PHP", "TEST")
        self.assertEqual(snap.rejected_rows, 1)
        self.assertEqual(len(snap.offers), 0)

    def test_amount_guard(self):
        for value in ("-4", "NaN", "Infinity", "0", "invalid"):
            with self.assertRaises(InvalidData):
                positive_decimal(value)

    def test_supply_report(self):
        r = market_report(self.snapshot)
        self.assertEqual(r["btc_seller_offers"], 3)
        self.assertEqual(r["btc_buyer_offers"], 1)

    def test_policy_fail_closed(self):
        self.assertEqual(evaluate("read_offers").decision, Decision.ALLOW)
        self.assertEqual(evaluate("publish_offer").decision, Decision.APPROVAL_REQUIRED)
        self.assertEqual(evaluate("release_btc").decision, Decision.DENY)
        self.assertEqual(evaluate("hack_backend").decision, Decision.DENY)

    def test_proof_fails_closed(self):
        with self.assertRaises(VerificationUnavailable):
            PaymentVerifier().verify(PaymentClaim("bank", "wire", "hash", "n", b"fake"))

    def test_only_local_api(self):
        for url in ("https://evil.com/api/v1", "http://127.0.0.1:8090/api/v1/other",
                    "http://admin:pass@localhost:8090/api/v1",
                    "http://localhost:8090/api/v1?redirect=https://evil.com"):
            with self.assertRaises(AdapterError):
                Bisq2LocalAdapter(url)
        self.assertEqual(Bisq2LocalAdapter().base_url, "http://127.0.0.1:8090/api/v1")

    def test_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.db"
            record(path, "merchant_activated", "PHP", "community_1")
            record(path, "quote_viewed", "PHP", "community_1")
            self.assertEqual(metrics(path)["total_events"], 2)
            self.assertEqual(metrics(path)["by_event"]["merchant_activated"], 1)
            with self.assertRaises(ValueError):
                record(path, "trade_completed", "PHP", "community_1")

    def test_outreach_is_draft(self):
        draft = outreach_draft("Local Merchant", "PHP", "GCash")
        self.assertEqual(draft["approval_state"], "draft_only")
        with self.assertRaises(ValueError):
            outreach_draft("a\nb", "PHP", "GCash")


if __name__ == "__main__":
    unittest.main()
