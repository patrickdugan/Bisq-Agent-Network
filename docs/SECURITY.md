# Security model and threat register

Bisq 2 pairing codes and session credentials authorize actions within the paired node. Keep the node loopback-only and use the minimum offerbook permissions. Never paste pairing QR codes, account IDs, session tokens or client secrets into public issues or LLM prompts.

1. Financial control: there is no trading write path. Policy denies trade acceptance, release, fiat confirmation and key access.
2. Data provenance: demo offers use SYNTHETIC_FIXTURE and a visible label. The live adapter uses BISQ2_LOCAL.
3. Price correctness: only structured fixed prices are ranked numerically; amount eligibility remains unverified.
4. Local transport: loopback HTTP only, no redirects, short timeout, bounded response size. No remote node addresses.
5. User privacy: incoming GET queries are intentionally not logged; the local dashboard sends no third-party telemetry. The SQLite ledger stores only event names, markets and acquisition channels.
6. Payment fraud: the proof verifier always rejects, as no vetted provider-bound verification is installed. Proof possession does not eliminate chargeback risk.
7. Model isolation: policy results cannot be overwritten by free-form model text; unknown actions are denied.
8. Web: localhost binding, no POST endpoints, frame denial, CSP, no CORS opt-in, no remote assets.

Future high-priority tests should cover malicious upstream payload sizes, redirects, stale sessions, replayed proofs, conflicting offers, partial payment, counterparty impersonation, currency mismatches and over-precision.

Responsible disclosure: submit sensitive vulnerabilities through GitHub's private vulnerability reporting channel if available; otherwise contact the maintainers privately. Never include live credentials or customer data in an issue.

9. Price validation: require consistent quote-side fiat precision 4 and base BTC precision 8. Malformed monetary sides and inverted amount ranges fail closed.
10. Market oracle: local observations expire after 60 seconds; underlying price age remains unknown. None of the prices permit automatic execution.
11. Merchant quoting: bounded 0–2500 bps asks with no Bisq write path.
12. DNS rebinding: local UI API refuses requests whose Host differs from its loopback binding.
