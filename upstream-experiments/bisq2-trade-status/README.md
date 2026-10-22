# Bisq 2 fork-first trade-state experiment

This is a **research patch** to the Bisq 2 API, staged in
Bisq-Agent-Network (BAN) and now applied to [the personal Bisq 2 fork](https://github.com/patrickdugan/bisq2/pull/1).
It is *not* a Bisq 2 release and does not run against mainnet.

Upstream baseline (pinned): `7767b8b3196b9e8be0a1413c2237f4f595b86cc0`
(October 21, 2026).
Upstream repository: https://github.com/bisq-network/bisq2

Patch: [0001-read-only-trade-status.patch](0001-read-only-trade-status.patch)
Verified fork branch: https://github.com/patrickdugan/bisq2/tree/experiment/agent-trade-state-recovery
Upstream contribution compare: https://github.com/bisq-network/bisq2/compare/main...patrickdugan:bisq2:experiment/agent-trade-state-recovery?expand=1

## Intent

An agent which receives a timeout from `POST /trades` cannot safely assume
the action failed. This experiment adds a *minimal read-only observation*
endpoint in an upstream fork:

`GET /api/v1/trades/status?offerId=<id>`

The response contains an array of these four values only:

```json
[{"tradeId":"trade-1","offerId":"offer-1",
  "tradeState":"INIT","lifecycle":"OPEN"}]
```

The query includes open and closed trade stores, deduplicates by trade ID
with CLOSED taking precedence, and orders by trade ID. It returns no raw
account, identity, chat, session, payment or private key material.

An empty response is **NOT** confirmation the POST failed; this endpoint is
a local, non-atomic observation of state. It establishes neither global P2P
consensus nor durable receipt of an ACK. Bisq 2's network/persistence race
tracked in https://github.com/bisq-network/bisq2/issues/5126 is out of scope.

## Companion BAN implementation

- `src/ban/trade_status.py`: strict four-field DTO validation
- `src/ban/recovery.py`: durable SQLite records for uncertain operations,
  no replay path and no `safe_to_retry=true` outcome
- `Bisq2LocalAdapter.trade_status(offer_id)`: GET-only, loopback-only
  experimental client method, not wired into production, dashboard or
  automatic trading
- `tests/test_recovery.py`: off-line validation, restart, duplicate-ID,
  proxy-isolation and malicious-payload tests

The existing BAN offer matching, pricing, GTM and mock-source workflows
remain unchanged. The experimental adapter *requires* existing Bisq 2
client/session credentials; there is no embedded authority escalation.

### Important limitation of current Bisq permissions

The existing `TRADES` grant covers both reads and state-changing requests.
This patch **does not** add a read-only-scoped credential. Do not expose
these credentials to any model or use the experimental client on a node
holding live funds. A future separately reviewed PR would need method-aware
grant enforcement and client-pairing compatibility.

## Experimental checks

An [experimental GitHub Actions workflow](../../.github/workflows/bisq2-upstream-experiment.yml)
checks out the exact upstream commit, applies this patch on a disposable CI
checkout, runs `git diff --check`, and executes the targeted Java API test.
The [targeted Java test passed](https://github.com/patrickdugan/Bisq-Agent-Network/actions/runs/39761101957), as did the [BAN Python tests](https://github.com/patrickdugan/Bisq-Agent-Network/actions/runs/39761238803).

To reproduce in a **personal fork**:

```sh
git clone https://github.com/YOUR_USER/bisq2.git
cd bisq2
git checkout -b experiment/agent-trade-state 7767b8b3196b9e8be0a1413c2237f4f595b86cc0
git apply --check ../Bisq-Agent-Network/upstream-experiments/bisq2-trade-status/0001-read-only-trade-status.patch
git apply ../Bisq-Agent-Network/upstream-experiments/bisq2-trade-status/0001-read-only-trade-status.patch
./gradlew :api:test --tests bisq.api.rest_api.endpoints.trades.TradeStatusQueryTest
git diff --check
```

Upstream review is the next step. The connected GitHub integration could not
create the cross-fork PR (HTTP 403); use the upstream compare link above. This
is still a proposal subject to maintainer review, not a merged Bisq feature.
Nothing here changes the existing Bisq main branch.

## Proposed follow-up PR after verifying the experiment

Only after a real fork, pinned integration tests, maintainer feedback and a
clear authority model: propose narrow read-only API permissions or
idempotency/status semantics. The observer is a diagnostic stepping stone,
not a justification for unattended trading.

Bisq code changes remain subject to the upstream AGPL-3.0 license.
