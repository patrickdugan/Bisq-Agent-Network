"""Synthetic-only $25-equivalent command for cashout corridor preflight."""
from ..corridor_preflight import RailCapabilities, illustrative_ils_minor, qualify_corridor



def simulate_mock_delivery(world, intent, intent_sig, resolution, shortlist) -> dict:
    """Complete a synthetic BTC-first payout using the existing reference provider.

    The fixture chain, provider keys, ledger and Jawwal rail all stay in the
    simulation package. This function can never submit a real transfer.
    """
    from ..intent import check_quote, verify_receipt
    from .ledger import State

    if world.cfg.get("source") != "SYNTHETIC_FIXTURE":
        raise ValueError("mock settlement requires a synthetic world")
    offers = shortlist.get("offers", [])
    if not offers:
        raise ValueError("there is no eligible synthetic BUY-side provider")
    selected = offers[0]
    if (selected["provider_pubkey"] != world.provider.pubkey or
            selected["rate_minor_per_btc"] != world.provider.rate_minor_per_btc or
            selected["maker_direction"] != "BUY"):
        raise ValueError("selected liquidity is not the fixture provider")
    if world.provider.rail is not world.rail_mock:
        raise ValueError("provider is not connected to the mock rail")

    quote, signature = world.provider.quote(intent, intent_sig,
                                             tip_height=world.chain.tip_height, now=world.now)
    check_quote(quote, signature, intent, resolution,
                tip_height=world.chain.tip_height, now=world.now)
    if (quote.net_payout_minor != intent.amount or
            quote.provider_pubkey != selected["provider_pubkey"]):
        raise ValueError("quote differs from selected beneficiary payout")
    qid = quote.quote_id()
    world.fund(quote)
    world.advance(blocks=world.cfg["provider"]["min_conf"] - 1, seconds=600)
    if world.provider.on_funding(qid, now=world.now) is not State.FUNDING_ACCEPTED:
        raise RuntimeError("synthetic funding was not accepted")
    if world.provider.authorize(qid, now=world.now) is not State.PAYOUT_AUTHORIZED:
        raise RuntimeError("synthetic payout was not authorized")
    state = world.provider.submit(qid, now=world.now)
    if state not in (State.PAYOUT_SUBMITTED, State.PAYOUT_CONFIRMED):
        raise RuntimeError("mock rail submission failed")
    if world.provider.poll(qid, now=world.now) is not State.PAYOUT_CONFIRMED:
        raise RuntimeError("mock wallet was not credited")
    receipt, receipt_signature = world.provider.issue_receipt(qid, now=world.now)
    verify_receipt(receipt, receipt_signature, quote)

    key = quote.idempotency_key()
    mock_status = world.rail_mock.status(key)
    calls = world.rail_mock.submit_calls
    if (len(calls) != 1 or calls[0][0] != key or calls[0][2] != quote.net_payout_minor or
            calls[0][3] != intent.currency or mock_status.status != "delivered" or
            mock_status.reference != world.rail_mock.reference(key)):
        raise RuntimeError("mock rail amount, reference or delivery mismatch")
    statement = {key: (mock_status.status, calls[0][2])}
    reconciled = world.provider.reconcile(statement, now=world.now)
    if reconciled.get(qid) != State.RECONCILED.value:
        raise RuntimeError("mock payout did not reconcile")

    return {
        "source": "SYNTHETIC_FIXTURE",
        "protocol": "cashout-btc-first",
        "selected_offer_id": selected["offer_id"],
        "quote_id": qid,
        "receipt_id": receipt.receipt_id(),
        "receipt_status": receipt.status,
        "mock_rail_credit_minor": calls[0][2],
        "mock_rail_submissions": len(calls),
        "reconciled_state": reconciled[qid],
        "btc_sats_funded": quote.sats_required,
        "local_fee_minor": quote.fee_minor,
        "provider_payout_to": "opaque_private_enrollment",
        "wallet_destination_exposed": False,
        "real_wallet_credited": False,
        "live_liquidity_verified": False,
    }

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
    p.add_argument("--simulate-settlement", action="store_true",
                   help="fund, credit and reconcile against mock chain and mock Jawwal rail only")
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
        world.enroll()  # signed private destination enrollment, synthetic only
        resolution = world.resolve()
        intent, intent_sig = world.intent(resolution, amount_mode="receive_exact", amount=target,
                                          min_net_payout_minor=target)
        snapshot = normalize_snapshot(cfg["offerbook"], "ILS", "SYNTHETIC_FIXTURE")
        inventory = fixture_inventory(cfg["inventory"], cfg["keys"], now=world.now,
                                      rail=world.rail, currency=world.currency, exponent=world.exponent)
        shortlisted = cashout_shortlist(snapshot, intent, inventory,
                                        providers=[world.p1.pubkey], now=world.now)
        result = qualify_corridor(shortlisted, intent, RailCapabilities.synthetic(), now=world.now)
        if args.simulate_settlement and result["status"] == "SIMULATION_PASS":
            result["mock_settlement"] = simulate_mock_delivery(world, intent, intent_sig,
                                                                resolution, shortlisted)
    result["illustrative_usd"] = args.usd
    result["illustrative_ils_per_usd"] = args.synthetic_ils_per_usd
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "SIMULATION_PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
