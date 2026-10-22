"""End-to-end assertions for cloud Docker pod and synthetic Bisq node."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error
import urllib.request

HOST = "http://127.0.0.1:8088"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path: str, method: str = "GET", override_host: str | None = None):
    headers = {"Accept": "application/json"}
    if override_host is not None:
        headers["Host"] = override_host
    req = urllib.request.Request(HOST + path, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=6) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def assert_status(path, expected=200, method="GET", override_host=None):
    status, raw, headers = request(path, method, override_host)
    assert status == expected, f"{path}: expected HTTP {expected}, got {status}"
    return raw, headers


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--report", required=True)
    args = p.parse_args()

    raw, headers = assert_status("/")
    assert b"Bisq Agent Network" in raw
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert_status("/styles.css")
    assert_status("/app.js")
    health = json.loads(assert_status("/api/health")[0])
    assert health["source"] == "BISQ2_LOCAL"
    assert health["mode"] == "read_only"

    offers = json.loads(assert_status("/api/offers?currency=PHP")[0])
    assert offers["source"] == "BISQ2_LOCAL"
    assert offers["offer_count"] == 4
    assert offers["rejected_rows"] == 0
    assert all(item["offer_id"].startswith("synthetic-") for item in offers["offers"])

    market = json.loads(assert_status("/api/report?currency=PHP")[0])
    assert market["btc_seller_offers"] == 3

    quote = json.loads(assert_status("/api/quote?currency=PHP&amount=1000&payment=GCash")[0])
    assert quote["requires_human_trade_confirmation"] is True
    assert quote["matched_count"] == 2
    floating = next(x for x in quote["offers"] if x["pricing_type"] == "FloatPriceSpec")
    assert floating["fiat_per_btc"] == "4253750.0000"
    assert floating["price_provenance"] == "floating_derived_unverified_age"

    merchant = json.loads(assert_status(
        "/api/merchant-quote?currency=PHP&fiat_amount=1000&margin_bps=150"
    )[0])
    assert merchant["approval_state"] == "draft_only"
    assert merchant["route"] == "none"
    assert merchant["suggested_fiat_per_btc"] == "4212250.0000"

    cohort = json.loads(assert_status("/api/cohort?currency=PHP")[0])
    assert cohort["enrolled"] == 0
    assert cohort["visibility"] == "local_only"

    plan = json.loads(assert_status("/api/plan?currency=PHP")[0])
    assert all(task["external_execution"] == "human_only" for task in plan["tasks"])

    assert_status("/api/trades", 404)
    assert_status("/api/offers", 405, method="POST")
    assert_status("/api/health", 403, override_host="untrusted.example")

    result = {
        "status": "passed",
        "type": "ephemeral_cloud_container_end_to_end",
        "test_data": "synthetic_bisq2_rest_mock",
        "real_bisq_node_connected": False,
        "financial_execution_enabled": False,
        "dashboard_exposure": "localhost_only",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "dashboard_html": True,
            "static_assets": True,
            "security_headers": True,
            "local_bisq_rest_headers": True,
            "market_offers": offers["offer_count"],
            "buyer_quote_matching": quote["matched_count"],
            "merchant_draft_quote": True,
            "privacy_preserving_cohort": True,
            "human_controlled_planner": True,
            "mutation_methods_denied": True,
            "invalid_host_denied": True,
        },
        "note": "This run exercises the container against a synthetic locally authenticated API. Real Bisq 2 integration still needs a separately paired node.",
    }
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Cloud pod smoke test PASSED (synthetic node).")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
