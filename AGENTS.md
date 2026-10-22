# Agent instructions for Bisq Agent Network

Purpose: independent, local-first integrations that help real merchants and buyers use Bisq safely. This is a research prototype. A source adapter is not a license to move funds.

## Non-negotiable execution boundary
- No LLM or unattended process may release BTC, take offers, assert that fiat settled, export keys, import private contact lists, or verify payment proofs without a separately reviewed design.
- Outbound communication, offer publication and price changes require a separately implemented human approval gateway. The current code only generates local drafts.
- Never accept synthetic fixture data as observed market conditions.
- Keep read-only API credentials on localhost, out of the repository and out of model prompts/logs.
- Unknown capabilities and verification providers fail closed.
- Do not infer executable liquidity from offerbook snapshots or displayed quote amount ranges.
- Prefer explicit versioned contract tests over guessed upstream schemas.

## Commands
- Run: PYTHONPATH=src python -m ban.cli serve
- Test: PYTHONPATH=src python -m unittest discover -s tests -v
- Compile: python -m compileall -q src tests

## Design
- Python 3.11 stdlib for v0.3; keep runtime portable.
- Use Decimal for monetary calculations.
- Maintain source labels (SYNTHETIC_FIXTURE versus BISQ2_LOCAL).
- Keep personal data out of analytics and URLs.
- Update tests, docs and API contracts together with any functional change.
- This repository does not modify Bisq upstream. Follow the AGPL terms if adding actual Bisq source.

## Pilot privacy

Merchant cohort writes are CLI/operator actions only and require an explicit attestation of merchant consent. Never add contact details, wallet credentials, customer payment data or a remotely writable signup route. The dashboard must expose only aggregate cohort metrics, clearly labeled operator-reported. Withdrawing consent deletes local merchant profiles. Real node integration tests require a user-controlled paired Bisq node; fake-node CI is contract simulation only.
