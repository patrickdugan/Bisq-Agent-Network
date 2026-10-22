import json
import tempfile
from pathlib import Path
import os
from unittest.mock import patch
import threading
import unittest
import urllib.error
import urllib.request
from ban.server import make_server
from ban.cohort import enroll


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp.name) / "app.db"
        cls.server = make_server(0, "fixture", db_path=str(cls.db_path))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.root = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.root + path, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers

    def test_health(self):
        status, data, headers = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "read_only")
        self.assertEqual(data["source"], "SYNTHETIC_FIXTURE")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_market_and_quote(self):
        self.assertEqual(self.get("/api/offers?currency=PHP")[1]["offer_count"], 4)
        data = self.get("/api/quote?currency=PHP&amount=1000&payment=GCash")[1]
        self.assertEqual(data["matched_count"], 2)
        self.assertTrue(data["requires_human_trade_confirmation"])

    def test_plan(self):
        tasks = self.get("/api/plan?currency=PHP")[1]["tasks"]
        self.assertTrue(tasks)
        self.assertEqual(tasks[0]["external_execution"], "human_only")

    def test_bad_amount_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.root + "/api/quote?currency=PHP&amount=NaN")
        self.assertEqual(caught.exception.code, 400)

    def test_post_is_disabled(self):
        req = urllib.request.Request(self.root + "/api/quote", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 405)

    def test_merchant_quote_draft(self):
        status, data, _ = self.get("/api/merchant-quote?currency=PHP&fiat_amount=1000&margin_bps=150")
        self.assertEqual(status, 200)
        self.assertEqual(data["approval_state"], "draft_only")
        self.assertEqual(data["suggested_fiat_per_btc"], "4212250.0000")
        self.assertEqual(data["route"], "none")
        self.assertFalse(data["upstream_age_verified"])

    def test_local_host_required(self):
        req = urllib.request.Request(self.root + "/api/health", headers={"Host": "evil.test"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(caught.exception.code, 403)

    def test_dynamic_buyer_reference(self):
        data = self.get("/api/quote?currency=PHP&amount=1000&payment=GCash")[1]
        offer = next(x for x in data["offers"] if x["pricing_type"] == "FloatPriceSpec")
        self.assertEqual(offer["fiat_per_btc"], "4253750.0000")
        self.assertEqual(offer["price_provenance"], "floating_derived_unverified_age")

    def test_cohort_endpoint_returns_only_aggregate_counts(self):
        item = enroll(self.db_path, alias="OperatorMerchant", currency="PHP",
                      payment_methods=["GCash"], channel="community", consent_attested=True)
        status, view, _ = self.get("/api/cohort?currency=PHP")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(view["enrolled"], 1)
        self.assertEqual(view["verification"], "operator_attested_not_independently_verified")
        self.assertNotIn(item["merchant_id"], json.dumps(view))
        self.assertNotIn("OperatorMerchant", json.dumps(view))

    def test_reject_unsupported_bind_address(self):
        with patch.dict(os.environ, {"BAN_BIND_HOST": "192.0.2.10"}):
            from ban.server import make_server
            with self.assertRaises(ValueError):
                make_server(0, "fixture")

    def test_static_assets(self):
        with urllib.request.urlopen(self.root + "/", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Bisq Agent Network", response.read())


if __name__ == "__main__":
    unittest.main()
