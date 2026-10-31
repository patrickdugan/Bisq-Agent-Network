"""Synthetic-only $25-equivalent command for cashout corridor preflight."""
from ..corridor_preflight import RailCapabilities, illustrative_ils_minor, qualify_corridor

def main() -> None:
    """Demonstrate a synthetic $25-equivalent payout without network or funds."""
    import argparse
    import json
    from pathlib import Path
    import tempfile
    from ..cashout import cashout_shortlist
    from ..models import normalize_snapshot
    from .world import CashoutWorld, fixture_inventory

    p = argparse.ArgumentParser(description="Offline BTC->ILS/JawwalPay corridor preflight")
    p.add_argument("--fixture", default="fixtures/cashout_demo.json")
    p.add_argument("--usd", default="25.00")
    p.add_argument("--synthetic-ils-per-usd", default="3.50")
    args = p.parse_args()
    raw = Path(args.fixture).read_bytes()
    if len(raw) > 1_000_000:
        p.error("fixture too large")
    cfg = json.loads(raw)
    if cfg.get("source") != "SYNTHETIC_FIXTURE":
        p.error("only SYNTHETIC_FIXTURE data are supported")
    target = illustrative_ils_minor(args.usd, args.synthetic_ils_per_usd)
    with tempfile.TemporaryDirectory() as temp:
        world = CashoutWorld(temp, cfg)
        intent, _ = world.intent(amount_mode="receive_exact", amount=target, min_net_payout_minor=target)
        snapshot = normalize_snapshot(cfg["offerbook"], "ILS", "SYNTHETIC_FIXTURE")
        inventory = fixture_inventory(cfg["inventory"], cfg["keys"], now=world.now,
                                      rail=world.rail, currency=world.currency, exponent=world.exponent)
        shortlisted = cashout_shortlist(snapshot, intent, inventory,
                                        providers=[world.p1.pubkey], now=world.now)
        result = qualify_corridor(shortlisted, intent, RailCapabilities.synthetic(), now=world.now)
    result["illustrative_usd"] = args.usd
    result["illustrative_ils_per_usd"] = args.synthetic_ils_per_usd
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "SIMULATION_PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
