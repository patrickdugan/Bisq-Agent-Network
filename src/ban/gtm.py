"""Local draft generation and merchant supply diagnostics."""
from collections import Counter
import re
from .models import Snapshot, currency_code


def market_report(snapshot: Snapshot) -> dict:
    sellers = [o for o in snapshot.offers if o.maker_direction == "SELL"]
    buyers = [o for o in snapshot.offers if o.maker_direction == "BUY"]
    methods = Counter(method for o in sellers for method in set(o.payment_methods))
    return {
        "currency": snapshot.currency,
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at,
        "total_offers": len(snapshot.offers),
        "btc_seller_offers": len(sellers),
        "btc_buyer_offers": len(buyers),
        "seller_fixed_price_offers": sum(o.price_fiat_per_btc is not None for o in sellers),
        "seller_payment_method_coverage": dict(methods.most_common(25)),
        "supply_warning": "Snapshot counts are not independently verified liquidity or executable size.",
    }


def outreach_draft(merchant: str, currency: str, payment_method: str) -> dict:
    name = merchant.strip()
    method = payment_method.strip()
    code = currency_code(currency)
    if not re.fullmatch(r"[^\r\n]{2,80}", name) or not re.fullmatch(r"[^\r\n]{2,80}", method):
        raise ValueError("merchant and payment method must be single-line, 2 to 80 characters")
    return {
        "approval_state": "draft_only",
        "merchant": name,
        "message": (
            "Hi " + name + ", I'm working on an independent open-source Bisq 2 merchant "
            "assistant for " + code + " traders using " + method + ". "
            "We're interviewing merchants about quote reliability and onboarding. "
            "Would you be open to a brief conversation? "
            "This is a research invitation, not a promise of trading volume."
        ),
        "instructions": "Review the merchant's contact preferences and local rules before sending manually.",
    }
