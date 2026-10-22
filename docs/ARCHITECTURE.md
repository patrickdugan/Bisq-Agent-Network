# Architecture

The application is a read-only integration sidecar. It is not a Bisq fork.

Architecture:

    [Bisq 2 local node] --GET--> [Adapter] --> [Validated Snapshot]
                                                 |-- [Buyer Concierge]
                                                 |-- [Supply/GTM Planner]
                                                 |-- [Local HTTP Dashboard]
    [Operator event] --> [SQLite counts] ---------+
    [Agent request] --> [Finite policy map] --> allow/read, draft/review, deny
    [Payment proof] --> [Unavailable verifier] --> raise / reject

## Upstream boundaries

The Bisq 2 REST node exposes resources under /api/v1, including GET /offerbook/markets/{currencyCode}/offers. The upstream response includes an offer DTO and presentation fields such as quoted payment methods and formatted amounts. The current adapter uses only structured fixed-price values for numerical rankings.

Incoming maker SELL offers can be displayed to prospective BTC buyers. Monetary values are handled with Python Decimal. No inference is made from locale-formatted strings. Floating-price and market-price offers remain unpriced until a safe live-price implementation exists. Amount ranges and counterparty availability remain unknown until verified in the actual trading client.

The API client supplies Bisq-Client-Id and Bisq-Session-Id headers from a locally authorized Bisq session. The adapter limits its upstream to loopback HTTP, denies redirects, times out requests after five seconds and caps response data at 1 MiB.

The UI binds to loopback, has no third-party script dependencies and accepts no writes. Static security headers restrict script loading and framing. Any intended remotely accessible wallet widget will require its own authorization and security review.

## Trust and data classifications

- Synthetic fixture: test data. Always marked SYNTHETIC_FIXTURE.
- Bisq 2 local: observed offer data. It is indicative and may already be stale.
- GTM events: manual operator reports without counterparty identities.
- Agent plans: advisory task proposals that require human action.
- Payment proofs: all rejected until an independently reviewed verifier is implemented.

## Agent runtime

The prototype uses deterministic planners; an LLM can be inserted later behind the same policy vocabulary. Model-generated plans are untrusted and must never be used to confer permissions. The policy allows local reads/drafts, flags outbound or publishing actions for approval, and denies fund movement and purported fiat verification. An APPROVAL_REQUIRED decision has no executor in v0.1.

## Future ZK research

An attestation must bind an issuer-authenticated statement to a specific rail, amount, currency, payment reference, recipient commitment, trade intent, validity window and domain-separated nullifier. Verify signatures/proofs, freshness, replay protection and provider revocation. Handle refunds or reversals separately. Passing a ZK proof does not by itself establish fiat finality, merchant solvency or legal permission to operate.

## Next integration work

Pin an upstream Bisq 2 version, capture sanitized endpoint schemas, implement authenticated session renewal with explicit operator consent, verify structured price units against upstream code, and add deterministic tests for stale data and conflicting price/amount formats. Keep every trading mutation off until its invariants and human approval path have been separately designed and tested.

## v0.2 quote and amount boundary

Strict decoder: fiat price precision 4, BTC precision 8, verified monetary sides, and four types of structured amount bounds. The buyer shortlist excludes offers outside advertised ranges. Fixed BTC amounts need final validation in Bisq.

GET /market-price/quotes supplies a reference observed at local read time, without an authenticated upstream price timestamp. Dynamic estimates are labeled upstream_age_verified=false. FloatPriceSpec percentage is a fractional markup (0.025 = 2.5%).

GET /api/merchant-quote creates an advisory merchant ask price from a recent local reference and supervised margin in basis points. It never publishes an offer.
