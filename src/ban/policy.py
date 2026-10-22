"""Finite policy vocabulary. LLM output has no authority to change it."""
from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "human_approval_required"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str

    def as_dict(self):
        return {"decision": self.decision.value, "reason": self.reason}


_READ = frozenset({"read_offers", "rank_offers", "read_metrics", "draft_outreach", "draft_merchant_advice"})
_REVIEW = frozenset({"contact_merchant", "publish_offer", "send_chat", "change_quote"})
_DENY = frozenset({"take_offer", "release_btc", "confirm_fiat", "spend_funds",
                   "access_private_keys", "verify_payment_proof", "import_private_contacts"})


def evaluate(action: str) -> PolicyResult:
    if action in _READ:
        return PolicyResult(Decision.ALLOW, "read-only or local draft")
    if action in _REVIEW:
        return PolicyResult(Decision.APPROVAL_REQUIRED, "human review plus a separate executor is required")
    if action in _DENY:
        return PolicyResult(Decision.DENY, "unimplemented or forbidden financial authority")
    return PolicyResult(Decision.DENY, "unknown capability; fail closed")
