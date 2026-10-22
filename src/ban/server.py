"""Local-only HTTP dashboard. GET routes read market data and never trade."""
from __future__ import annotations

import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .adapters import AdapterError, make_adapter
from .concierge import shortlist
from .gtm import market_report
from .ledger import metrics
from .models import InvalidData, currency_code
from .planner import plan
from .merchant import draft_merchant_quote
from .cohort import cohort_report

STATIC = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def handler_class(source: str | None = None, db_path: str | None = None):
    adapter = make_adapter(source)
    ledger_path = db_path or os.getenv("BAN_DB", "ban.sqlite3")

    class Handler(BaseHTTPRequestHandler):
        server_version = "BAN/0.3"
        sys_version = ""

        def log_message(self, format, *args):
            # Avoid recording URLs (which can contain sensitive trading intent).
            return

        def reply(self, status: int, content: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def json(self, status: int, data):
            self.reply(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_POST(self):
            self.json(405, {"error": "read_only", "message": "This service accepts GET only"})

        def do_GET(self):
            port = self.server.server_port
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self.json(403, {"error": "invalid_host"})
                return
            url = urlsplit(self.path)
            if url.path in STATIC_FILES:
                name, content_type = STATIC_FILES[url.path]
                try:
                    self.reply(200, (STATIC / name).read_bytes(), content_type)
                except OSError:
                    self.json(500, {"error": "missing_static_asset"})
                return
            if url.path == "/api/health":
                self.json(200, {"ok": True, "service": "bisq-agent-network",
                                "source": "SYNTHETIC_FIXTURE" if adapter.__class__.__name__ == "FixtureAdapter"
                                else "BISQ2_LOCAL", "mode": "read_only"})
                return
            query = parse_qs(url.query, keep_blank_values=True)
            currency = query.get("currency", ["PHP"])[0]
            try:
                currency = currency_code(currency)
                if url.path == "/api/metrics":
                    self.json(200, metrics(ledger_path))
                    return
                if url.path == "/api/cohort":
                    # Aggregate counts only, never pseudonyms, contact info or merchant IDs.
                    self.json(200, cohort_report(ledger_path, currency))
                    return
                if url.path == "/api/merchant-quote":
                    result = draft_merchant_quote(
                        adapter.reference_price(currency),
                        fiat_amount=query.get("fiat_amount", [""])[0],
                        margin_bps=query.get("margin_bps", [""])[0],
                    )
                    self.json(200, result)
                    return
                if url.path in ("/api/offers", "/api/report", "/api/quote", "/api/plan"):
                    snapshot = adapter.offers(currency)
                    if url.path == "/api/offers":
                        result = snapshot.as_dict()
                    elif url.path == "/api/report":
                        result = market_report(snapshot)
                    elif url.path == "/api/plan":
                        result = plan(snapshot, metrics(ledger_path))
                    else:
                        amount = query.get("amount", [""])[0]
                        payment = query.get("payment", [""])[0]
                        try:
                            reference = adapter.reference_price(currency)
                        except AdapterError:
                            reference = None
                        result = shortlist(snapshot, amount, payment, reference)
                    self.json(200, result)
                    return
                self.json(404, {"error": "unknown_route"})
            except (ValueError, InvalidData) as exc:
                self.json(400, {"error": "invalid_input", "message": str(exc)})
            except AdapterError as exc:
                self.json(502, {"error": "upstream_unavailable", "message": str(exc)})
            except Exception:
                # The response intentionally omits stack traces and credential-bearing data.
                self.json(500, {"error": "internal_error"})

    return Handler


def make_server(port: int = 8088, source: str | None = None, db_path: str | None = None):
    if not (0 <= port <= 65535):
        raise ValueError("port must be in 0..65535")
    host = os.getenv("BAN_BIND_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError("BAN_BIND_HOST must be 127.0.0.1 or 0.0.0.0")
    return ThreadingHTTPServer((host, port), handler_class(source, db_path))


def serve(port: int = 8088, source: str | None = None) -> None:
    with make_server(port, source) as server:
        print("BAN read-only dashboard at http://127.0.0.1:" + str(server.server_port), flush=True)
        print("Source: " + ("BAN_SOURCE environment" if source is None else source))
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    serve()
