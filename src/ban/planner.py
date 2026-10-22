"""A deterministic, auditable growth planner with no outbound authority."""
from .gtm import market_report
from .ledger import metrics
from .models import Snapshot
from .policy import evaluate


def plan(snapshot: Snapshot, ledger: dict | None = None) -> dict:
    report = market_report(snapshot)
    history = ledger or {"by_event": {}}
    counts = history.get("by_event", {})
    tasks: list[dict] = []

    def add(priority: int, action: str, why: str, next_step: str) -> None:
        tasks.append({
            "priority": priority,
            "action": action,
            "rationale": why,
            "next_step": next_step,
            "policy": evaluate("draft_merchant_advice").decision.value,
            "external_execution": "human_only",
        })

    if report["btc_seller_offers"] < 5:
        add(1, "recruit_btc_sellers",
            "Fewer than five Bitcoin sell offers appear in this snapshot.",
            "Interview willing local merchants about inventory, payment rails and response times.")
    if report["seller_fixed_price_offers"] < 2:
        add(2, "improve_quoted_prices",
            "Few seller offers provide a structured fixed price readable by the agent.",
            "Ask merchants about pricing modes and inspect the live Bisq app before publishing guidance.")
    if len(report["seller_payment_method_coverage"]) < 2:
        add(3, "expand_payment_coverage",
            "Bitcoin seller offers show limited payment method coverage.",
            "Survey local preferences and payment-provider terms with merchants.")
    if counts.get("quote_viewed", 0) > 0 and not counts.get("trade_started", 0):
        add(2, "investigate_buyer_dropoff",
            "Recorded quote views have not produced operator-reported trade starts.",
            "Interview consenting buyers about quote size, merchant responsiveness and onboarding.")
    if not tasks:
        add(3, "review_merchant_retention", "Seller offer coverage has passed the initial thresholds.",
            "Review repeat buyers and real trade completion with participating merchants.")
    return {
        "currency": report["currency"],
        "source": snapshot.source,
        "plan_type": "deterministic_draft",
        "tasks": sorted(tasks, key=lambda task: task["priority"]),
        "guardrails": "No unsolicited outreach, quote publication, fund movement or payment verification.",
        "caveat": "Offer snapshots cannot prove that merchants are available or hold sufficient inventory.",
    }
