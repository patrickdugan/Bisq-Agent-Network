"""Interface for verifiable payment attestations; fail closed until implemented."""
from dataclasses import dataclass


class VerificationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PaymentClaim:
    provider: str
    rail: str
    commitment: str
    nullifier: str
    proof_bytes: bytes


class PaymentVerifier:
    def verify(self, claim: PaymentClaim) -> bool:
        # A hash, screenshot or LLM classification is not payment verification.
        # Future integration must validate issuer authenticity, binding, freshness,
        # payment finality, nullifier uniqueness, replay protection and revocation.
        raise VerificationUnavailable("no audited provider-bound payment verifier installed")
