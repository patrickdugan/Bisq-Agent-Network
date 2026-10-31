# Cash App Global: BTC/ILS to JawwalPay corridor preflight (offline)

**Status: diagnostic and simulated demonstration only.** This branch adds no executor, wallet integration, API credentials, real market access, deposit addresses, fund movement or key handling. BAN's payout policy continues to DENY authorization and submission.

## Usage

    PYTHONPATH=src python -m ban.corridor_preflight \
      --fixture fixtures/cashout_demo.json --usd 25.00 --synthetic-ils-per-usd 3.50
    PYTHONPATH=src python -m unittest discover -s tests -v

The demonstration creates a synthetic receive-exact intent for **ILS 87.50**, which corresponds to **$25 at an explicitly fictitious rate of ILS 3.50/USD**. It uses BAN's existing signed BUY-side shortlist with synthetic provider inventory and a synthetic recipient commitment. Output includes candidate synthetic offer IDs, missing gate fields and execution-disabled flags. A SIMULATION_PASS means the *diagnostic fixture* is coherent. There is **no real payment or completed wallet credit** in this command. The pre-existing cashout-demo covers mock rail credit and reconciliation.

## What this diagnostic checks

- Correct BTC cashout direction: providers must have maker_direction=BUY.
- ILS, JawwalPay, receive_exact, correct currency exponent and matching payout amount.
- Canonical opaque recipient commitment (never reveal the wallet phone/identifier in output).
- Fresh, sufficiently sized **self-asserted** provider inventory from BAN's signed advisory shortlist.
- A finite intent deadline and explicit human confirmation requirement.
- Provider licensing, beneficiary-directed settlement permission, KYC/sanctions process, authorized merchant payouts, idempotency, status query, reliable payout evidence and local-currency float.
- Every non-synthetic market report remains BLOCKED for live execution, even when its capability checkboxes are all filled. Operator-reported compliance is a research claim, not proof of permission.

## Live blocker inventory (as of 2026-11-01)

| Gate | Evidence or unanswered question |
| --- | --- |
| Live BTC/ILS BUY order | **Unverified**. An offerbook snapshot is advisory; one needs a current bid, maker acceptance, signed quote and actual reserved NIS float. |
| Direct payout to a different beneficiary | **Unverified** in Bisq's trade rules and JawwalPay's provider agreement. Use a separate authorized provider agreement; do not assume Bisq permits a third-party fiat payee. |
| JawwalPay merchant disbursement API | **Unverified** authentication, status/query-by-reference, retry/idempotency, webhook authenticity, amount exponent, recipient identifiers, limitations and fees. |
| Legal corridor | Requires direct review by licensed Palestinian counterparties. PMA payment-service rules restrict virtual-asset and FX activity by payment-service companies. Distinguish an authorized local-wallet transfer funded through compliant institutional channels from a payment service accepting USDT directly. |
| Last-mile cash | Wallet credit, merchant acceptance and withdrawable cash are different outcomes. Confirm merchant/agent liquidity and the recipient's actual cash-out accessibility. |
| Source crypto | BAN mock funds with BTC; **USDT-TRC20 is a separate conversion and risk problem**, outside this branch. |
| Distribution | Recipient consent and a privacy-preserving, recoverable payout alias are prerequisite to any live enrollment. |

### Sources and contacts for due diligence

- Bisq fiat payment limitation and participant-account rules: https://docs.bisq.network/payment-methods and https://docs.bisq.network/trading-rules.html
- PMA payment services restrictions, Instruction No. 6/2021, Article 3: https://regalert.today/document/instruction-no-6-of-2021-regarding-the-regulation-of-payment-service-companies-b-77fd0fd0-9422-4399-a225-8a24c8135bc3
- PMA remittance licensing, Instruction No. 3/2022, Article 9: https://regalert.today/document/instructions-no-3-of-2022-concerning-the-issuance-and-receipt-of-financial-remit-09850865-9397-4907-afe4-ef8a0bc64b61
- UNDP partnership with JawwalPay and PalPay (2024-09-26): https://www.undp.org/arab-states/press-releases/undp-partners-leading-digital-payment-service-providers-strengthen-digital-financial-solutions-gaza
- JawwalPay merchant support: info@jawwalpay.ps (public app-store contact). Request documented business API terms and a pilot partner contact, rather than assuming the merchant app exposes payouts.

## Next integration criteria

1. Obtain a written answer on whether an appropriately licensed counterpart may make third-party recipient wallet credits and what beneficiary verification is required.
2. Obtain a provider API specification, sandbox, distinct test credentials, replay-safe idempotency reference, status query, authenticated receipts and reconciliation statements. Where these are absent, leave automated settlement disabled.
3. Validate real buy-side liquidity through a locally observed Bisq BTC/ILS offer *and* a compliant provider's reserved fiat float. Quote must bind recipient commitment, amount, fees and expiry.
4. Keep the recipient experience to wallet enrollment and receipt confirmation only. Do not publish real account details in GitHub fixtures.
5. Only after separate human security/compliance review, design a distinct signed authorization gateway; this branch intentionally provides none.

Existing baseline: feat/cashout-regtest-evidence at 3eb4406 (funding recheck I9). Keep it in the PR ancestry; the earlier v0 branch must never be merged alone.
