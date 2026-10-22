import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from ban.adapters import AdapterError, Bisq2LocalAdapter, FixtureAdapter
from ban.nodecheck import check_local_node


class FakeBisq(BaseHTTPRequestHandler):
    last_headers = None
    def do_GET(self):
        FakeBisq.last_headers = dict(self.headers)
        if self.path == "/api/v1/config/capabilities":
            payload = json.dumps({"apiVersion": "2.1.14", "features": ["offerbook", "market-price"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        fixtures = {
            "/api/v1/offerbook/markets/PHP/offers": "bisq2_PHP.json",
            "/api/v1/market-price/quotes": "bisq2_PHP_quote.json",
        }
        if self.path not in fixtures:
            self.send_response(404)
            self.end_headers()
            return
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / fixtures[self.path]
        payload = fixture.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


class AdapterHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBisq)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def test_documented_endpoint_and_headers(self):
        url = "http://127.0.0.1:" + str(self.server.server_port) + "/api/v1"
        client = Bisq2LocalAdapter(url, "test-client", "test-session")
        snapshot = client.offers("php")
        self.assertEqual(snapshot.source, "BISQ2_LOCAL")
        self.assertEqual(len(snapshot.offers), 4)
        self.assertEqual(FakeBisq.last_headers["Bisq-Client-Id"], "test-client")
        self.assertEqual(FakeBisq.last_headers["Bisq-Session-Id"], "test-session")
        ref = client.reference_price("PHP")
        self.assertEqual(str(ref.fiat_per_btc), "4150000.0000")
        self.assertEqual(ref.source, "BISQ2_LOCAL")
        self.assertFalse(ref.upstream_age_verified)
        self.assertEqual(FakeBisq.last_headers["Bisq-Client-Id"], "test-client")

    def test_node_contract_doctor(self):
        url = "http://127.0.0.1:" + str(self.server.server_port) + "/api/v1"
        client = Bisq2LocalAdapter(url, "test-client", "test-session")
        report = check_local_node(client, "PHP")
        self.assertEqual(report["contract_checks"], "passed_locally")
        self.assertEqual(report["capabilities"]["api_version"], "2.1.14")
        self.assertEqual(report["offerbook"]["decoded_offers"], 4)
        self.assertFalse(report["market_reference"]["oracle_publication_age_verified"])
        self.assertEqual(report["financial_execution"], "disabled")
        self.assertNotIn("test-session", json.dumps(report))
        self.assertNotIn("test-client", json.dumps(report))

    def test_node_doctor_requires_real_adapter_and_session(self):
        with self.assertRaises(ValueError):
            check_local_node(FixtureAdapter(), "PHP")
        with self.assertRaises(AdapterError):
            check_local_node(Bisq2LocalAdapter(), "PHP")


if __name__ == "__main__":
    unittest.main()
