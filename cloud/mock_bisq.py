"""SYNTHETIC REST node for cloud integration tests. Not a real Bisq node."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROUTES = {
    "/api/v1/offerbook/markets/PHP/offers": FIXTURES / "bisq2_PHP.json",
    "/api/v1/market-price/quotes": FIXTURES / "bisq2_PHP_quote.json",
}
EXPECTED_CLIENT = "ci-mock-client"
EXPECTED_SESSION = "ci-mock-session"


class FakeBisqHandler(BaseHTTPRequestHandler):
    server_version = "SyntheticBisq2/0.0"
    sys_version = ""

    def log_message(self, *args):
        return

    def _respond(self, status, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if (self.headers.get("Bisq-Client-Id") != EXPECTED_CLIENT
                or self.headers.get("Bisq-Session-Id") != EXPECTED_SESSION):
            self._respond(401, {"error": "synthetic auth headers missing"})
            return
        if self.path == "/api/v1/config/capabilities":
            self._respond(200, {
                "apiVersion": "SYNTHETIC-2.1.14",
                "features": ["synthetic-offerbook", "synthetic-market-price"],
            })
            return
        fixture = ROUTES.get(self.path)
        if fixture is None:
            self._respond(404, {"error": "unknown synthetic endpoint"})
            return
        self._respond(200, json.loads(fixture.read_text(encoding="utf-8")))

    def do_POST(self):
        self._respond(405, {"error": "synthetic node is read only"})


def main():
    port = int(os.getenv("BAN_MOCK_BISQ_PORT", "8090"))
    if not 0 < port < 65536:
        raise SystemExit("invalid port")
    with ThreadingHTTPServer(("127.0.0.1", port), FakeBisqHandler) as server:
        print("SYNTHETIC Bisq mock listening on loopback", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
