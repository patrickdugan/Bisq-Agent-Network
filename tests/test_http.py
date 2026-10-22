import json
import threading
import unittest
import urllib.error
import urllib.request
from ban.server import make_server


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0, "fixture")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.root = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

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

    def test_static_assets(self):
        with urllib.request.urlopen(self.root + "/", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Bisq Agent Network", response.read())


if __name__ == "__main__":
    unittest.main()
