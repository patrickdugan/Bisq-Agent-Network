"""CLI: demos, local Bisq market data, merchant drafts and local-only dashboard."""
import argparse
import json
import os
from .adapters import AdapterError, make_adapter
from .concierge import shortlist
from .gtm import market_report, outreach_draft
from .ledger import metrics, record
from .models import InvalidData
from .policy import evaluate


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ban", description="Bisq Agent Network, read-only v0.1")
    p.add_argument("--source", choices=("fixture", "bisq2"), default=None,
                   help="fixture (synthetic) or bisq2 (local paired node)")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("offers", "report"):
        sp = sub.add_parser(name)
        sp.add_argument("--currency", default="PHP")
    sp = sub.add_parser("quote")
    sp.add_argument("--currency", default="PHP")
    sp.add_argument("--amount", required=True)
    sp.add_argument("--payment-method", default="")
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
    snapshot = make_adapter(args.source).offers(args.currency)
    if args.command == "offers":
        return snapshot.as_dict()
    if args.command == "report":
        return market_report(snapshot)
    if args.command == "quote":
        return shortlist(snapshot, args.amount, args.payment_method)
    raise ValueError("unsupported command")


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "serve":
            from .server import serve
            serve(args.port, args.source)
            return
        print(json.dumps(run(args), indent=2, ensure_ascii=False))
    except (AdapterError, InvalidData, ValueError) as exc:
        raise SystemExit("Error: " + str(exc)) from None


if __name__ == "__main__":
    main()
