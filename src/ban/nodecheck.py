"""Run a bounded local Bisq 2 REST contract smoke check with no credential output."""
from __future__ import annotations

from .adapters import AdapterError, Bisq2LocalAdapter
from .models import currency_code


def check_local_node(adapter: Bisq2LocalAdapter, currency: str = "PHP") -> dict:
    if not isinstance(adapter, Bisq2LocalAdapter):
        raise ValueError("node-check requires a local Bisq 2 REST adapter")
    if not (adapter.client_id and adapter.session_id):
        raise AdapterError("pair a read-only Bisq client and set both client/session IDs")
    code = currency_code(currency)
    result: dict = {
        "source": "BISQ2_LOCAL",
        "currency": code,
        "capabilities": {"status": "unavailable"},
        "offerbook": {"status": "unavailable"},
        "market_reference": {"status": "unavailable"},
        "contract_checks": "needs_attention",
        "financial_execution": "disabled",
    }
    # This public metadata is absent on some older Bisq 2 nodes.
    try:
        caps = adapter._get_json("/config/capabilities")
        if not isinstance(caps, dict) or not isinstance(caps.get("apiVersion"), str):
            result["capabilities"] = {"status": "invalid_schema"}
        else:
            result["capabilities"] = {
                "status": "ok",
                "api_version": caps["apiVersion"][:40],
                "reported_features_count": len(caps.get("features", []))
                if isinstance(caps.get("features"), list) else 0,
            }
    except AdapterError as exc:
        result["capabilities"] = {
            "status": "optional_on_older_nodes" if str(exc) == "Bisq node returned HTTP 404"
            else "unavailable"
        }

    try:
        snapshot = adapter.offers(code)
        result["offerbook"] = {
            "status": "ok",
            "decoded_offers": len(snapshot.offers),
            "rejected_rows": snapshot.rejected_rows,
            "schema_clean": snapshot.rejected_rows == 0,
        }
    except AdapterError as exc:
        result["offerbook"] = {
            "status": "market_not_exposed" if str(exc) == "Bisq node returned HTTP 404"
            else "connection_or_schema_error"
        }
    try:
        reference = adapter.reference_price(code)
        result["market_reference"] = {
            "status": "ok",
            "fiat_per_btc": str(reference.fiat_per_btc),
            "oracle_publication_age_verified": reference.upstream_age_verified,
            "note": "A local receipt timestamp cannot establish oracle publication freshness.",
        }
    except AdapterError as exc:
        result["market_reference"] = {
            "status": "no_reference" if str(exc) == "Bisq node returned HTTP 404"
            else "connection_or_schema_error"
        }

    if (result["offerbook"]["status"] == "ok"
            and result["offerbook"]["schema_clean"]
            and result["market_reference"]["status"] == "ok"
            and result["capabilities"]["status"] in ("ok", "optional_on_older_nodes")):
        result["contract_checks"] = "passed_locally"
    result["next_step"] = (
        "Review version compatibility, payment-method availability and price age in the Bisq UI. "
        "This result verifies the local API contract only and never authorizes a trade."
    )
    return result
