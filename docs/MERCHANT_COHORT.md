# Merchant cohort: local pilot operations

v0.3 introduces an operator-controlled, consent-based merchant roster for one-market pilot experiments. It is a separate software tool from Bisq's public identity and trading records.

## Enroll merchants

Only enroll a merchant after receiving explicit consent for this local research roster. Give the merchant a pseudonymous alias rather than a legal name. Use generic payment rail names and never include payment account identifiers or contact information.

Example on Windows PowerShell:

    $env:PYTHONPATH="src"
    python -m ban.cli merchant-enroll --alias MerchantDemo01 --currency PHP --method GCash --method "Bank Transfer" --channel community --attest-consent
    python -m ban.cli merchant-list --currency PHP
    python -m ban.cli cohort --currency PHP

The --attest-consent flag records only the operator's attestation. The application does not independently prove that consent occurred. The output includes a random merchant ID used for status changes and withdrawal.

New merchants start in an **onboarded** state. To mark an actual local merchant self-reported active, verify they have recently confirmed availability, then run:

    python -m ban.cli merchant-status --merchant-id <32-character-id> --state active --attest-availability

The active state remains **operator-reported**. It does not imply that Bisq has accepted an offer or that a merchant has usable cash or BTC liquidity.

## Consent renewal and withdrawal

Consent is valid for a 90-day pilot window. Records past that window are excluded from local dashboards. Renewal requires a fresh operator attestation of consent.

    python -m ban.cli merchant-renew --merchant-id <id> --attest-consent

On a merchant's withdrawal request, delete their profile and source information. Deletion is irreversible:

    python -m ban.cli merchant-withdraw --merchant-id <id> --attest-request

To delete expired profiles from the SQLite file:

    python -m ban.cli merchant-purge-expired

The operator must also remove any exports or copies they created outside this tool.

## Storage and privacy

- Stored: random merchant ID, pseudonymous alias, fiat currency, generic payment rails, operator-reported state, acquisition channel and timestamps.
- Excluded: wallet keys, identity documents, bank/e-wallet account numbers, addresses, telephone numbers, email addresses, messages and P2P order histories.
- SQLite lives in BAN_DB (default ban.sqlite3). The program attempts private owner-only file permissions at creation on POSIX. Windows security follows directory ACLs.
- Local dashboard GET /api/cohort returns aggregate counts and channel/rail distributions. Merchant IDs and aliases are available only through the CLI.
- The software never posts merchant profiles to Bisq, wallets, third-party analytics or model services.
- The independent event ledger tracks acquisition channel/event counts, not merchant identities; operator-reported trade completions are not independently verified.

## Launch gate

Before soliciting trades, check legal and payment-rail requirements for each market. The cohort tracker is for planning and interviewing merchants, with no exchange or settlement authority. A merchant being registered in this roster never implies platform eligibility.

Initial targets: five consented merchants for qualitative interviews, then twenty merchants to evaluate onboarding and response quality. Distinguish interviews, self-reported activity, actual offer coverage and completed trades in every external metric.
