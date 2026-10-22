"""Offline regression tests for tentative Bisq 2 trade-state observations."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ban.adapters import AdapterError, Bisq2LocalAdapter
from ban.recovery import RecoveryJournal
from ban.trade_status import ObservationError, parse_statuses, recovery_outcome

OFFER = "offer-123"
COMMIT = "a" * 64


def statuses(*rows):
    return parse_statuses(list(rows), OFFER)


def item(trade="trade-1", offer=OFFER, lifecycle="OPEN", state="INIT"):
    return {"tradeId": trade, "offerId": offer, "tradeState": state, "lifecycle": lifecycle}


class StatusContractTests(unittest.TestCase):
    def test_full_status_contract_is_narrow(self):
        self.assertEqual(len(statuses(item())), 1)
        self.assertEqual(recovery_outcome(statuses()), "UNKNOWN")
        self.assertEqual(recovery_outcome(statuses(item())), "OBSERVED")
        self.assertEqual([x.trade_id for x in statuses(item("b"), item("a"))], ["a", "b"])

    def test_reject_sensitive_or_mismatched_fields(self):
        for payload in (
            [dict(item(), privateKey="SECRET")],
            [item(offer="other")],
            [item(state="any unknown")],
            [item(lifecycle="AWAITING")],
            [item(), item()],
            {"tradeId": "trade-1"},
            [item(trade="hello\nworld")],
            [item()] * 1001,
        ):
            with self.subTest(payload=str(payload)[:100]), self.assertRaises(ObservationError):
                parse_statuses(payload, OFFER)

    def test_bad_ids_fail_closed(self):
        for bad in ("", "../other", "has space", "x" * 257, "a?b", "foo/bar"):
            with self.subTest(id=bad), self.assertRaises(ObservationError):
                parse_statuses([], bad)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "recovery.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_remains_unknown_when_query_is_empty(self):
        j = RecoveryJournal(self.db)
        record = j.record_ambiguous("intent-1", OFFER, COMMIT)
        self.assertEqual(record.result, "UNKNOWN")
        self.assertFalse(record.safe_to_retry)
        record = j.reconcile("intent-1", statuses())
        self.assertEqual(record.result, "UNKNOWN")
        self.assertFalse(record.safe_to_retry)
        restarted = RecoveryJournal(self.db).read("intent-1")
        self.assertEqual(restarted.result, "UNKNOWN")
        self.assertFalse(restarted.safe_to_retry)

    def test_observation_persists_without_linking_trade_to_intent(self):
        j = RecoveryJournal(self.db)
        j.record_ambiguous("intent-2", OFFER, COMMIT)
        r = j.reconcile("intent-2", statuses(item("trade-a", state="BTC_CONFIRMED")))
        self.assertEqual(r.result, "OBSERVED")
        self.assertEqual(r.observed_trade_ids, ("trade-a",))
        self.assertFalse(r.safe_to_retry)
        self.assertEqual(RecoveryJournal(self.db).reconcile("intent-2", statuses()).result, "OBSERVED")
        # Does not assert causality: an observed local trade may predate the intent.

    def test_different_terms_cannot_reuse_intent_identifier(self):
        j = RecoveryJournal(self.db)
        j.record_ambiguous("same", OFFER, COMMIT)
        j.record_ambiguous("same", OFFER, COMMIT)
        with self.assertRaises(ObservationError):
            j.record_ambiguous("same", OFFER, "b" * 64)
        with self.assertRaises(ObservationError):
            j.reconcile("same", (parse_statuses([item(offer="other")], "other")[0],))
        self.assertEqual(j.read("same").result, "UNKNOWN")


class FakeBisqTradeEndpoint(BaseHTTPRequestHandler):
    payload = [item()]
    last_headers = None
    last_path = None

    def do_GET(self):
        FakeBisqTradeEndpoint.last_headers = dict(self.headers)
        FakeBisqTradeEndpoint.last_path = self.path
        body = json.dumps(FakeBisqTradeEndpoint.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raise AssertionError("read-only adapter must not use POST")

    def log_message(self, *args):
        return


class ObserverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBisqTradeEndpoint)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def make_adapter(self, session="session-1"):
        url = "http://127.0.0.1:" + str(self.server.server_port) + "/api/v1"
        return Bisq2LocalAdapter(url, "client-1" if session else None, session)

    def test_trade_status_uses_local_get_with_existing_session(self):
        FakeBisqTradeEndpoint.payload = [item()]
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}):
            results = self.make_adapter().trade_status(OFFER)
        self.assertEqual(results[0].trade_id, "trade-1")
        self.assertEqual(FakeBisqTradeEndpoint.last_path, "/api/v1/trades/status?offerId=offer-123")
        self.assertEqual(FakeBisqTradeEndpoint.last_headers["Bisq-Client-Id"], "client-1")
        self.assertEqual(FakeBisqTradeEndpoint.last_headers["Bisq-Session-Id"], "session-1")

    def test_session_required(self):
        with self.assertRaises(AdapterError):
            self.make_adapter(session="").trade_status(OFFER)

    def test_invalid_upstream_payload_fails(self):
        FakeBisqTradeEndpoint.payload = [dict(item(), paymentAccountData="sensitive")]
        with self.assertRaises(ObservationError):
            self.make_adapter().trade_status(OFFER)
        FakeBisqTradeEndpoint.payload = [item()]


if __name__ == "__main__":
    unittest.main()
