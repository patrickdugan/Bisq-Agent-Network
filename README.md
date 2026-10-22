# Bisq Agent Network

**Open-source market intelligence, merchant GTM tooling, and a buyer concierge for decentralized Bitcoin trading.**

The long-term ambition is a distribution network that can make self-custodial P2P trading available through many wallets and communities. This repository implements the first verifiable slice: a read-only Bisq 2 adapter, deterministic buyer matching, a merchant-supply planner, human-reviewed outreach drafts, event counts and a local web dashboard.

**Status: v0.3 research prototype.** This project is independent and unaffiliated with the Bisq DAO. The MIT license covers the original adapter and application code; Bisq 2 itself is AGPL-3.0.

## Run it

Requires Python 3.11+. The demo has zero third-party runtime dependencies. From the repository root:

    PYTHONPATH=src python -m ban.cli report --currency PHP
    PYTHONPATH=src python -m ban.cli quote --currency PHP --amount 1000 --payment-method GCash
    PYTHONPATH=src python -m ban.cli plan --currency PHP
    PYTHONPATH=src python -m ban.cli merchant-quote --currency PHP --fiat-amount 1000 --margin-bps 150
    PYTHONPATH=src python -m ban.cli serve --port 8088
    PYTHONPATH=src python -m ban.cli cohort --currency PHP

Then open http://127.0.0.1:8088.

On Windows PowerShell:

    $env:PYTHONPATH="src"
    python -m ban.cli serve

The default Philippine-market offer and price data is **entirely synthetic**. Local merchant cohort records, if added using the CLI, are operator-reported and kept separately from that demo fixture. It supports reproducible demos and unit tests without connecting to Bisq.

## Connect your own local Bisq 2 node

Bisq 2 documents a local REST API, usually at http://127.0.0.1:8090/api/v1. Pair a client on the Bisq side and grant only the permissions required to read offerbook data. Supply the resulting client ID and live session ID as environment variables. Pairing codes, QR credentials and client secrets must remain on the local machine.

macOS/Linux example:

    export BAN_SOURCE=bisq2
    export BAN_BISQ_API_URL=http://127.0.0.1:8090/api/v1
    export BAN_BISQ_CLIENT_ID=YOUR_PAIRED_CLIENT_ID
    export BAN_BISQ_SESSION_ID=YOUR_CURRENT_SESSION_ID
    PYTHONPATH=src python -m ban.cli offers --currency PHP

The upstream endpoints are GET /offerbook/markets/{currencyCode}/offers and GET /market-price/quotes. The latter exposes a locally fetched reference whose original source age is unverified. Bisq 2 session headers are Bisq-Client-Id and Bisq-Session-Id. Sessions expire; refresh through Bisq's authorized pairing/session flow. The adapter intentionally refuses remote hosts and redirects, limits response size and never exposes a method that writes to Bisq.

Once paired, run the local contract smoke test using `PYTHONPATH=src python -m ban.cli --source bisq2 node-check --currency PHP`. The tool reports the API version (if exposed), parsing results and price quote availability without echoing pairing credentials. See docs/LIVE_NODE_CHECK.md.

If your node does not expose the endpoint or returns 401/403, consult the version-specific API documentation in Bisq. Upstream source references are in docs/SOURCES.md.

## Read-only workflows

    PYTHONPATH=src python -m ban.cli offers --currency PHP
    PYTHONPATH=src python -m ban.cli report --currency PHP
    PYTHONPATH=src python -m ban.cli plan --currency PHP
    PYTHONPATH=src python -m ban.cli outreach --merchant "Example Shop" --currency PHP --payment-method GCash
    PYTHONPATH=src python -m ban.cli policy publish_offer
    PYTHONPATH=src python -m ban.cli policy release_btc

The concierge ranks makers explicitly selling BTC. Fiat prices use four decimal places, and fractional markups are applied to locally fetched reference prices. Advertised amount bounds are checked, while executable size, reputation ceilings and payment settlement require confirmation in Bisq.

## Opt-in local merchant cohort

Merchants participate through explicit consent, attested by the operator. Records use pseudonymous aliases, generic payment rails, random local IDs and 90-day consent windows. The web dashboard shows only aggregate cohort metrics.

    PYTHONPATH=src python -m ban.cli merchant-enroll --alias DemoMerchant01 --currency PHP --method GCash --channel community --attest-consent
    PYTHONPATH=src python -m ban.cli merchant-list --currency PHP
    PYTHONPATH=src python -m ban.cli cohort --currency PHP

The software cannot independently verify the operator's consent attestation. Profile updates, withdrawal, deletion of expired records and privacy details are documented in docs/MERCHANT_COHORT.md.

## Optional local GTM ledger

    PYTHONPATH=src python -m ban.cli record --event merchant_interviewed --currency PHP --channel community
    PYTHONPATH=src python -m ban.cli record --event quote_viewed --currency PHP --channel community
    PYTHONPATH=src python -m ban.cli metrics

Events are aggregate, manually reported counts, recorded in a local SQLite database. They carry no names, payer details or communications. The default database filename is ignored by git.

## Tests

    PYTHONPATH=src python -m unittest discover -s tests -v

GitHub Actions runs the offline suite for each push and pull request.

## Current capabilities

- Bisq 2 local REST GET adapter with paired-session headers
- Strict four-decimal fiat and eight-decimal BTC price contracts
- Advertised BaseSide/QuoteSide fixed and range amounts
- Optional dynamic estimates from the local reference feed (upstream age unknown)
- Read-only merchant ask-price drafts with bounded basis-point spreads
- Exact decimal conversion of structured fixed-price offers
- Buyer offer matching by maker direction and payment method
- Deterministic supply-gap planner and merchant outreach drafts
- Immutable policy decisions distinguishing reads, approval requirements and forbidden financial actions
- Fail-closed payment-proof verification interface
- Local HTTP dashboard and aggregate GTM ledger
- Opt-in merchant cohort CLI with explicit consent, 90-day retention, status and withdrawal
- Aggregate cohort dashboard, segregated from Bisq offerbook liquidity
- Local Bisq 2 node contract smoke test against capabilities, offerbook and market price APIs

## Next milestones

1. Run the local node-check against a real paired Bisq 2 release and capture carefully sanitized contract fixtures.
2. Verify market oracle freshness and reputation/trade limits against a pinned released Bisq 2 node.
3. Optional merchant quote publication through a scoped capability and approval gateway, with upstream integration tests.
4. Audited, provider-bound payment attestations using selective disclosure and replay-safe nullifiers.
5. Wallet SDK, a separately consented public merchant directory and localized GTM experiments.

See docs/ARCHITECTURE.md, docs/GTM_PLAYBOOK.md, docs/MERCHANT_COHORT.md, docs/LIVE_NODE_CHECK.md and docs/SECURITY.md.

## Safety

The local dashboard binds to 127.0.0.1 only. It supports GET requests and never asks for payment credentials, account data or private keys. No trade is executed, funds are moved, contacts are scraped, merchant messages are sent or payment proofs are accepted. ZK payment verification is a research interface and throws an explicit exception until an audited verifier is installed. Never expose the local Bisq pairing service to the public internet.
