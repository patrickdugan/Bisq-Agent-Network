# Live Bisq 2 API contract smoke check

The v0.3 doctor performs GET-only checks against a **locally running, already paired** Bisq 2 REST API. It does not pair a node, authorize a trade or validate any bank payment.

## Preparation

1. Launch a compatible Bisq 2 node with the REST API enabled. Verify the local documentation at http://127.0.0.1:8090/doc/v1/index.html.
2. Use Bisq's own pairing process to authorize the minimum permissions for reading offerbook and market prices.
3. Keep the pairing code, client secret and session ID off GitHub and out of chat. Supply the local client ID and current session ID as environment variables. Refresh expired sessions through Bisq itself.
4. Run from the cloned Bisq-Agent-Network repository.

Example in Windows PowerShell:

    $env:PYTHONPATH="src"
    $env:BAN_SOURCE="bisq2"
    $env:BAN_BISQ_API_URL="http://127.0.0.1:8090/api/v1"
    $env:BAN_BISQ_CLIENT_ID="<paired client ID>"
    $env:BAN_BISQ_SESSION_ID="<current session ID>"
    python -m ban.cli --source bisq2 node-check --currency PHP

## Checks

- GET /config/capabilities: reads apiVersion and number of reported features when available. The capabilities endpoint can be absent on older nodes, and that absence is described rather than inventing a version.
- GET /offerbook/markets/PHP/offers: validates the envelope and counts decoded offers and rejected rows. A missing market is reported separately from a schema violation.
- GET /market-price/quotes: validates the PHP Fiat/BTC PriceQuote value, monetary sides and precision.
- Output has no client IDs, session tokens, user profiles or payment account fields. Local requests use loopback only, bypass proxy settings, reject redirects and limit response bytes.
- "passed_locally" means the inspected API responses match this sidecar's contract. It is not a Bisq certification, a guarantee of quote freshness or a trading permission.

Bisq's current market-price endpoint does not expose a verified timestamp from the oracle itself. The sidecar therefore labels oracle_publication_age_verified=false even if the local request succeeded. Changes to Bisq's versioned DTOs can cause the strict decoder to reject records. Do not loosen validation to make a bad node appear healthy.

## Tests and production boundary

CI exercises a local fake Bisq node with representative synthetic DTOs and authentication headers. **Real mainnet integration testing must be performed with an actual locally paired Bisq 2 node**, since one is not available in the repository's automation environment.

No payment rails, merchant inventory, fiat settlement, trading limits, pricing validity or order completion are independently validated by the doctor. All live financial operations stay in Bisq with direct human approval.
