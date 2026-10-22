# Merchant-first GTM playbook

The long-term ambition is broad distribution through self-custodial wallets and localized merchant software. Adoption should be measured in completed, consented trades and returning customers, while observed offer counts serve only as early supply indicators.

## Initial experiment: one country, one fiat market, one payment-method cohort

- Days 1–15: interview 30 permissioned merchants and recruit 5 to maintain actual inventory.
- Days 16–30: aim for 20 active merchants with quote monitoring, support and marketplace operating guidance.
- Days 31–60: activate willing wallet/community distribution partners, publish accurate local onboarding guidance and study abandoned buyer journeys.
- Days 61–90: measure repeat use, completed trade rates and merchant service costs; add paid merchant subscriptions only after demonstrated usefulness.

Philippines is a proposed pilot due to local access for qualitative fieldwork. The live offerbook and payment-method support must be verified before launching any campaign. Market suitability includes applicable platform rules, remittance/payment requirements, exchange registration and sanctions review.

## Agent responsibilities

Merchant Scout: organize opt-in research and draft personalized outreach for review. It does not harvest private contacts or send mass messages.

Liquidity Pilot: analyze sell-side count, payment method coverage and visible price modes. Numeric executable quotes require current pricing, availability and size bounds.

Buyer Concierge: shortlist public maker SELL offers by fiat and selected method. It explicitly warns users that offer limits and fiat settlement must be confirmed in Bisq.

Merchant Copilot: develop approved response drafts for routine education. Future permissions for trades or communications require separate security review.

Growth Analyst: read de-identified event counts and propose conversion experiments. Operator-reported completions are never presented as independently settled trades.

## Metrics

- Offer coverage by currency/payment method
- Approached merchant to opted-in interview conversion
- Activated merchants with genuinely available offers
- Quote view to verified, consented trade completion
- 30-day repeat buyer rate
- Merchant retention, fraud losses and refunds after dispute windows
- Contribution margin per merchant and per distribution partner

## Illustrative economics

At 20 paying merchants and a hypothetical $79/month software subscription, recurring gross subscription receipts are $1,580/month before servicing and acquisition costs. Bisq Easy does not charge protocol trading fees, so subscriptions and approved paid tooling need an independently useful value proposition. All targets are experimental assumptions.

## Limits on acquisition

No deceptive volume claims, fictitious liquidity, unsolicited contact harvesting, fake referrals, hidden paid endorsements or evasion of exchange controls. Inform users that available payment methods and legal requirements differ by jurisdiction. Conflict-area trading corridors require particular care with payment reliability, local access, user safety and sanctions exposure.

## Cohort implementation (v0.3)

Merchant operations now have a separate local consent-based SQLite roster. Record a pseudonymous merchant using `merchant-enroll --attest-consent`; switch to operator-reported active only after independent confirmation; honor withdrawal immediately with `merchant-withdraw --attest-request`. Profiles expire after 90 days unless renewed and are excluded from the dashboard once expired. Dashboard summaries contain only aggregated counts.

Treat the live Bisq offerbook, the self-reported merchant roster and operator-entered activity events as distinct populations. A merchant counted as active in the roster is not evidence of a live Bisq offer or completed trade. The first measurable experiment is five independently interviewed, consenting merchants and genuine, separately verified offers in one fiat market.
