"""CLI: demos, local Bisq market data, merchant drafts and local-only dashboard."""
import argparse
import json
import os
from .adapters import AdapterError, make_adapter
from .concierge import shortlist
from .gtm import market_report, outreach_draft
from .ledger import metrics, record
from .models import InvalidData
from .planner import plan
from .merchant import draft_merchant_quote
from .policy import evaluate
from .cohort import (CohortError, enroll, list_merchants, update_status, withdraw, renew_consent, purge_expired, cohort_report)
from .nodecheck import check_local_node


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ban", description="Bisq Agent Network, local-first v0.3")
    p.add_argument("--source", choices=("fixture", "bisq2"), default=None,
                   help="fixture (synthetic) or bisq2 (local paired node)")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("offers", "report", "plan"):
        sp = sub.add_parser(name)
        sp.add_argument("--currency", default="PHP")
    sp = sub.add_parser("quote")
    sp.add_argument("--currency", default="PHP")
    sp.add_argument("--amount", required=True)
    sp.add_argument("--payment-method", default="")
    sp = sub.add_parser("merchant-quote")
    sp.add_argument("--currency", default="PHP")
    sp.add_argument("--margin-bps", required=True)
    sp.add_argument("--fiat-amount", required=True)
    sp = sub.add_parser("outreach")
    sp.add_argument("--merchant", required=True)
    sp.add_argument("--currency", default="PHP")
    sp.add_argument("--payment-method", required=True)
    sp = sub.add_parser("policy")
    sp.add_argument("action")
    sp = sub.add_parser("record")
    sp.add_argument("--event", required=True)
    sp.add_argument("--currency", default="PHP")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("metrics")
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("node-check")
    sp.add_argument("--currency", default="PHP")
    sp = sub.add_parser("merchant-enroll")
    sp.add_argument("--alias", required=True)
    sp.add_argument("--currency", default="PHP")
    sp.add_argument("--method", action="append", required=True)
    sp.add_argument("--channel", choices=("direct","community","merchant_referral","wallet_partner","event","other"), required=True)
    sp.add_argument("--attest-consent", action="store_true",
                    help="operator attests merchant explicitly consented to local 90-day cohort storage")
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("merchant-list")
    sp.add_argument("--currency", default=None)
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("merchant-status")
    sp.add_argument("--merchant-id", required=True)
    sp.add_argument("--state", choices=("onboarded","active","paused"), required=True)
    sp.add_argument("--attest-availability", action="store_true")
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("merchant-withdraw")
    sp.add_argument("--merchant-id", required=True)
    sp.add_argument("--attest-request", action="store_true")
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("merchant-renew")
    sp.add_argument("--merchant-id", required=True)
    sp.add_argument("--attest-consent", action="store_true")
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("merchant-purge-expired")
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("cohort")
    sp.add_argument("--currency", default="PHP")
    sp.add_argument("--db", default=os.getenv("BAN_DB", "ban.sqlite3"))
    sp = sub.add_parser("serve")
    sp.add_argument("--port", type=int, default=8088)
    return p


def run(args: argparse.Namespace) -> dict:
    if args.command == "policy":
        return evaluate(args.action).as_dict()
    if args.command == "outreach":
        return outreach_draft(args.merchant, args.currency, args.payment_method)
    if args.command == "record":
        record(args.db, args.event, args.currency, args.channel)
        return {"recorded": True, "event": args.event, "verification": "operator_reported"}
    if args.command == "metrics":
        return metrics(args.db)
    if args.command == "merchant-enroll":
        return enroll(args.db, alias=args.alias, currency=args.currency,
                      payment_methods=args.method, channel=args.channel,
                      consent_attested=args.attest_consent)
    if args.command == "merchant-list":
        return list_merchants(args.db, args.currency)
    if args.command == "merchant-status":
        return update_status(args.db, merchant_id=args.merchant_id, state=args.state,
                             availability_attested=args.attest_availability)
    if args.command == "merchant-withdraw":
        return withdraw(args.db, merchant_id=args.merchant_id, request_attested=args.attest_request)
    if args.command == "merchant-renew":
        return renew_consent(args.db, merchant_id=args.merchant_id,
                             consent_attested=args.attest_consent)
    if args.command == "merchant-purge-expired":
        return purge_expired(args.db)
    if args.command == "cohort":
        return cohort_report(args.db, args.currency)
    adapter = make_adapter(args.source)
    if args.command == "node-check":
        return check_local_node(adapter, args.currency)
    if args.command == "merchant-quote":
        return draft_merchant_quote(adapter.reference_price(args.currency),
                                    margin_bps=args.margin_bps, fiat_amount=args.fiat_amount)
    snapshot = adapter.offers(args.currency)
    if args.command == "offers":
        return snapshot.as_dict()
    if args.command == "report":
        return market_report(snapshot)
    if args.command == "plan":
        return plan(snapshot, metrics(os.getenv("BAN_DB", "ban.sqlite3")))
    if args.command == "quote":
        try:
            reference = adapter.reference_price(args.currency)
        except AdapterError:
            reference = None
        return shortlist(snapshot, args.amount, args.payment_method, reference)
    raise ValueError("unsupported command")


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "serve":
            from .server import serve
            serve(args.port, args.source)
            return
        print(json.dumps(run(args), indent=2, ensure_ascii=False))
    except (AdapterError, InvalidData, CohortError, ValueError) as exc:
        raise SystemExit("Error: " + str(exc)) from None


if __name__ == "__main__":
    main()
