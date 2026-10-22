import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from ban.adapters import AdapterError, Bisq2LocalAdapter


class FakeBisq(BaseHTTPRequestHandler):
    last_headers = None
    def do_GET(self):
        FakeBisq.last_headers = dict(self.headers)
        if self.path != "/api/v1/offerbook/markets/PHP/offers":
            self.send_response(404)
            self.end_headers()
            return
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "bisq2_PHP.json"
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


if __name__ == "__main__":
    unittest.main()
