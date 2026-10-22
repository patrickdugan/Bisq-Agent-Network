"""Data adapters. Live Bisq connections are loopback-only and read-only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit, quote
from .trade_status import TradeObservation, parse_statuses, validate_id

from .models import Snapshot, currency_code, normalize_snapshot
from .references import MarketReference, decode_reference


class AdapterError(RuntimeError):
    pass


class FixtureAdapter:
    def __init__(self, fixtures_dir: Path | None = None):
        self.fixtures_dir = fixtures_dir or Path(os.getenv("BAN_FIXTURES_DIR", str(Path(__file__).resolve().parents[2] / "fixtures")))

    def offers(self, currency: str) -> Snapshot:
        code = currency_code(currency)
        path = self.fixtures_dir / ("bisq2_" + code + ".json")
        if not path.is_file():
            raise AdapterError("no synthetic fixture for this currency")
        try:
            return normalize_snapshot(json.loads(path.read_text(encoding="utf-8")), code, "SYNTHETIC_FIXTURE")
        except (OSError, ValueError) as exc:
            raise AdapterError("could not load fixture") from exc

    def reference_price(self, currency: str) -> MarketReference:
        code = currency_code(currency)
        path = self.fixtures_dir / ("bisq2_" + code + "_quote.json")
        if not path.is_file():
            raise AdapterError("no synthetic market reference for this currency")
        try:
            return decode_reference(json.loads(path.read_text(encoding="utf-8")), code, "SYNTHETIC_FIXTURE")
        except (OSError, ValueError) as exc:
            raise AdapterError("invalid synthetic market reference") from exc


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Bisq2LocalAdapter:
    """Uses documented GET offerbook endpoint and pre-paired Bisq session.

    This adapter never calls pairing, trading, messaging, or write endpoints.
    It deliberately rejects remote targets even when BISQ_API_URL is configured.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8090/api/v1",
        client_id: str | None = None,
        session_id: str | None = None,
    ):
        url = urlsplit(base_url)
        if (
            url.scheme != "http"
            or url.hostname not in ("127.0.0.1", "::1", "localhost")
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path.rstrip("/") != "/api/v1"
            or not url.port
        ):
            raise AdapterError("Bisq 2 REST endpoint must be a loopback HTTP URL ending /api/v1")
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.session_id = session_id

    def _get_json(self, path: str):
        url = self.base_url + path
        headers = {"Accept": "application/json", "User-Agent": "Bisq-Agent-Network/0.2"}
        if self.client_id and self.session_id:
            headers["Bisq-Client-Id"] = self.client_id
            headers["Bisq-Session-Id"] = self.session_id
        req = urllib.request.Request(url, headers=headers, method="GET")
        # Disable system proxies so Bisq session headers cannot escape localhost.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)
        try:
            with opener.open(req, timeout=5) as response:
                if response.status != 200:
                    raise AdapterError("Bisq node returned an unexpected HTTP status")
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise AdapterError("upstream response exceeds 1 MiB")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AdapterError("Bisq node denied access; pair a read-only client and refresh the session") from None
            raise AdapterError("Bisq node returned HTTP " + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AdapterError("could not connect to local Bisq node") from None
        except (ValueError, UnicodeError):
            raise AdapterError("Bisq node returned invalid JSON") from None

    def offers(self, currency: str) -> Snapshot:
        code = currency_code(currency)
        data = self._get_json("/offerbook/markets/" + code + "/offers")
        try:
            return normalize_snapshot(data, code, "BISQ2_LOCAL")
        except ValueError:
            raise AdapterError("invalid local Bisq offerbook response") from None

    def reference_price(self, currency: str) -> MarketReference:
        code = currency_code(currency)
        data = self._get_json("/market-price/quotes")
        try:
            return decode_reference(data, code, "BISQ2_LOCAL")
        except ValueError:
            raise AdapterError("missing or invalid Bisq market price reference") from None


    def trade_status(self, offer_id: str) -> tuple[TradeObservation, ...]:
        """Experimental read-only GET on a Bisq 2 fork; not used by default.

        Note that the current upstream TRADES permission also allows writes.
        No result, including an empty list, authorizes repeating a trade.
        """
        validate_id(offer_id)
        if not self.client_id or not self.session_id:
            raise AdapterError("paired client and session required for experimental trade observation")
        payload = self._get_json("/trades/status?offerId=" + quote(offer_id, safe=""))
        return parse_statuses(payload, offer_id)


def make_adapter(source: str | None = None) -> FixtureAdapter | Bisq2LocalAdapter:
    selected = source or os.getenv("BAN_SOURCE", "fixture")
    if selected == "fixture":
        return FixtureAdapter()
    if selected == "bisq2":
        return Bisq2LocalAdapter(
            base_url=os.getenv("BAN_BISQ_API_URL", "http://127.0.0.1:8090/api/v1"),
            client_id=os.getenv("BAN_BISQ_CLIENT_ID"),
            session_id=os.getenv("BAN_BISQ_SESSION_ID"),
        )
    raise AdapterError("BAN_SOURCE must be fixture or bisq2")
