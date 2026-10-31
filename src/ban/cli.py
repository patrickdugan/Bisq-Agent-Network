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
    sp = sub.add_parser("cashout-market", help="read-only BTC/ILS BUY bids and JawwalPay text mentions")
    sp.add_argument("--target-ils", default="87.50", help="recipient amount in ILS, two decimals")
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
    sp = sub.add_parser("cashout-demo", help="simulated cashout flow and refused attacks (synthetic fixture only)")
    sp.add_argument("--fixture", default="fixtures/cashout_demo.json")
    sp.add_argument("--arena-out", default=None,
                    help="where to write the P2P-Settlement-Arena score lines (default out/arena_cashout.jsonl, "
                         "or out/arena_cashout_regtest.jsonl with --regtest)")
    sp.add_argument("--regtest", action="store_true",
                    help="fund and judge on a private throwaway bitcoind -regtest (started, then deleted)")
    sp.add_argument("--bitcoind", default=os.getenv("BAN_BITCOIND"), help="bitcoind binary for --regtest")
    sp.add_argument("--regtest-root", default=os.getenv("BAN_REGTEST_ROOT"),
                    help="parent directory for the throwaway regtest datadir (default: the system temp dir)")
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
    if args.command == "cashout-demo" and args.regtest:
        from .sim.regtest import run_regtest_demo  # private regtest node only; never reached by other commands
        return run_regtest_demo(args.fixture, bitcoind=args.bitcoind, root=args.regtest_root,
                                arena_out=args.arena_out or "out/arena_cashout_regtest.jsonl")
    if args.command == "cashout-demo":
        from .sim.demo import run_demo  # simulation only; never reached by other commands
        return run_demo(args.fixture, arena_out=args.arena_out or "out/arena_cashout.jsonl")
    adapter = make_adapter(args.source)
    if args.command == "cashout-market":
        from .cashout_market import inspect_ils_buy_orders
        snapshot = adapter.offers("ILS")
        try:
            reference = adapter.reference_price("ILS")
        except AdapterError:
            reference = None
        return inspect_ils_buy_orders(snapshot, args.target_ils, reference)
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
        result = run(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (AdapterError, InvalidData, CohortError, ValueError) as exc:
        raise SystemExit("Error: " + str(exc)) from None
    if args.command == "cashout-demo" and args.regtest and not result["all_cases_held"]:
        raise SystemExit("Error: a regtest funding case did not hold")
    if args.command == "cashout-demo" and not args.regtest and not result["all_attacks_refused"]:
        raise SystemExit("Error: an attack was not refused")
    if args.command == "cashout-demo" and not result["arena"]["all_passed"]:
        raise SystemExit("Error: an arena score line did not match its oracle")


if __name__ == "__main__":
    main()
