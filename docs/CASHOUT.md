# Cashout slice v0: alias, intent, BUY-side quote, simulated ledger

**Status: simulation only.** Nothing in this slice moves funds, calls a payout rail, contacts a
relay or NIP-05 domain, or verifies a proof. `ban.policy` keeps `authorize_local_payout` and
`submit_local_payout` at **DENY**, and there is no executor. The payout engine is a *simulated
reference provider* under `src/ban/sim/` with a mock rail. Every key in the simulation derives
from a public label (`ban.sim.testkeys`), so it can sign synthetic fixtures only.

**Jawwal Pay is the first candidate rail, and every Jawwal capability is unverified**: its API,
authentication, idempotency or merchant reference, status query, webhook signing, currency and
units, limits and KYC terms. `MockJawwalRail` assumes an idempotency key used as the merchant
reference and a query-by-reference, because the ledger needs both. If the real rail offers
neither, the production gate fails and payouts need manual reconciliation.

Design source: `Spiral/paper/cash_app_global_architecture.md` (sections B.3, B.4, C.4–C.8, D.2,
D.5, E, F). Run the demo:

    PYTHONPATH=src python -m ban.cli cashout-demo --fixture fixtures/cashout_demo.json

It prints each B.3 transition (T0a–T9) against a synthetic world, then replays 30 attacks, and
writes P2P-Settlement-Arena score lines to `out/arena_cashout.jsonl` (`--arena-out` to move it).
It exits non-zero if any attack is not refused or any score line misses its oracle.

## Flow

1. **Alias (T0a, T0b, T1).** `alice@pay.test` is bound to a key by the domain's NIP-05 document
   (fixture source; redirects refused) and to content by a BIP340-signed NIP-01 addressable event
   carrying `AliasRecordV1` in TL-Canon v1. The record holds per-provider payout
   **commitments**, never a destination. A lint refuses `payto` tags and destination-like fields
   or values.
2. **Enrollment (T0c).** Alice privately sends P1 a signed `DestinationEnrollmentV1` that opens
   the commitment. P1 stores the destination in its vault keyed by (alias key, commitment) and
   replies with a signed `EnrollmentAckV1`.
3. **Selection.** `cashout.cashout_shortlist` keeps only makers **BUYING** BTC that carry
   a fresh `InventoryAttestationV1` signed by a provider key listed in Alice's record, with enough
   capacity. `concierge.shortlist` is unchanged and still keeps only makers SELLING BTC.
4. **Intent and quote (T2, T3).** The sender signs `PayoutIntentV1` with an ephemeral key. P1
   reserves float and signs `ProviderQuoteV1` with a fresh deposit script. The sender runs
   `intent.check_quote`.
5. **Funding (T4, T5).** P1 judges the deposit from its own node's view (`evidence.FixtureEvidence`
   in the demo, or `CoreRpcEvidence` against a loopback regtest node; see
   [Regtest evidence](#regtest-evidence-spec-g5)).
6. **Payout (T6–T9).** P1 re-judges the accepted outpoint on its node's current active chain (a
   reorg below `min_conf` sends the quote back to `FUNDING_SEEN`), re-checks the alias, writes
   `PayoutAuthorizationV1` before any rail call, writes the single rail attempt, calls the mock
   rail, issues a signed `PayoutReceiptV1` and reconciles against the rail statement.

## Trust boundaries (from spec E, the rows that apply here)

| Boundary | What establishes it | What does **not** | State in this slice |
| --- | --- | --- | --- |
| Name binding | Domain NIP-05 (TLS/DNS) plus R's signature plus the sender's pubkey pin | Relays; the handle string | Built against fixtures: resolver with SQLite pins, quorum of 2, rollback/equivocation/expiry/revocation, rotation and 72 h recovery with veto |
| Destination privacy | Commitments in public; enrollment only to P | Gift wrap alone if destinations are published elsewhere (NIP-A3) | Commitments and lint built; enrollment travels in-process (NIP-17/44/59 deferred) |
| Provider liquidity | Funded float plus a reservation honored, attested by P | `InventoryAttestationV1` (self-asserted), BAN advertised bounds | Reservations built in the simulated ledger; attestations only narrow the shortlist |
| Freshness | Node tip versus evidence tip (`max_tip_lag`) and record `valid_until` | In-proof `confirmations` | Enforced by `evidence.judge` and the resolver |
| Active-chain acceptance | The node's active chain at lookup | Proven chainwork alone | Fixture chain, and `CoreRpcEvidence` on a private regtest node (reorgs by `invalidateblock`) |
| Unspentness | The node's view (`scantxoutset`) | Any inclusion proof | `CoreRpcEvidence` only; not needed by P for its own deposit |
| Proof validity | A pinned verifier binary actually run | A JSON `verified:true`, `proof_verified:true`, a receipt hash | No verifier configured: SP1/Nova evidence is refused; `verified` flags are refused |
| Actual fiat delivery | Rail-signed status (if Jawwal provides one), recipient acknowledgment, reconciliation | Any Bitcoin proof; P's receipt alone (`provider_attested_only`) | Mock rail only; receipts say `api_status` or `provider_attested_only` |

## Ledger (spec B.4)

`ban.sim.ledger.PayoutLedger` is SQLite with one `BEGIN IMMEDIATE` transaction per change:

- `UNIQUE(outpoint)` (I2), `UNIQUE(deposit_script_sha256)` (I3) and one authorization row per
  quote (I1) are schema constraints.
- One rail attempt per idempotency key, written before the rail call.
- `PAYOUT_UNKNOWN` leaves only with an observed rail status (I4), and is resolved only by
  `status()`. Webhooks are unauthenticated triggers: the provider re-queries status and never
  trusts the body.
- Reservations are released on `EXPIRED`, `REFUNDED` and `RECONCILED` (I5). Funding cannot be
  accepted once the reservation is released.
- Amounts are fixed at the quote (I6). Funding below `quote_created_height` is refused (I7).
  The alias seq at authorization must be at least the quote's, and a revoked alias or a changed
  commitment sends the payout to `HOLD` (I8).
- Crash recovery: an authorized-but-unsent row is sent once with the same key; a sent row with
  no recorded response becomes `PAYOUT_UNKNOWN`.

## Contracts

All D.2 binary contracts live in `src/ban/intent.py`: fixed-width big-endian encodings, a version
byte, the `claim_v1.py` network discriminants (mainnet 0, testnet4 1, signet 2, regtest 3) and a
domain-separated SHA-256 per object. Golden vectors for the intent and quote on all four networks
are in `fixtures/cashout_golden_vectors.json`. BIP340 verification (`src/ban/bip340.py`) is
checked against the vendored upstream vectors in `fixtures/bip340_test_vectors.csv` (source and
SHA-256 in `fixtures/bip340_test_vectors.SOURCE`).

## P2P-Settlement-Arena contract (spec D.5)

`src/ban/arena_contract.py` (contract `ban-arena-cashout/1`) lets the arena
([P2P-Settlement-Arena](https://github.com/patrickdugan/P2P-Settlement-Arena/tree/0f8262723d761b34890c49e04e80e5e2254f5387)
@`0f826272`) model the cashout direction and lets BAN report in its vocabulary. It keeps the
arena's distinction: `passed` means the oracle matched, `attack_success` means a forbidden
economic outcome happened.

- **`ROLE_MAP`**: provider → `buyer`, sender → `seller`, recipient → `fiat_payee` (new). The module
  refuses to import if the provider is not the buyer.
- **Two profiles, not merged.** `cashout-btc-first` (this design: the sender funds first and bears
  the exposure) and `cashout-bisq-easy` (the provider pays the recipient first; the sender releases
  BTC on `RecipientAckV1` or rail evidence). The party that moves first bears the second mover's
  misbehavior; misbehavior by the first mover, the environment or an outside attacker is blocked
  by the other side's checks. `BEHAVIORS` records the expected oracle for each behavior under
  each profile.
- **`ArenaScenarioV1`**: a TL-Canon v1 export with `scenario_id = SHA256("BAN-ARENA-SCENARIO-V1\0" ‖
  bytes)`. Golden scenarios for all 12 behaviors under both profiles are in
  `fixtures/arena/cashout_scenarios.jsonl`, with IDs in `cashout_scenario_ids.json`. The arena
  scores USD only ($6–$600), so every other currency exports `arena_supported: false`; the demo's
  ILS scenarios are all unsupported until the arena adds currencies.
- **`parse_arena_line`**: exact key sets and types for the arena's `bisq-easy` and
  `bisq-musig-model` scenario lines, its `control_state` line, and the two cashout protocols.
- **`ledger_trace_to_arena_lines`** scores a trace read from three separate sources: the fixture
  chain (sats the sender sent), the provider ledger (B.4 history, refunds, receipts) and the mock
  rail (the only credit to the recipient). An unmitigated `provider_withholds` exports
  `attack_success=true` with `sender_btc_at_risk_sats > 0`: the custodial risk, shown honestly.
- **`check_conservation`**: sats and fiat minor units are each conserved, and the recipient is
  credited only by a modeled rail event; a receipt without a rail credit is a violation.

BAN simulates only `cashout-btc-first`. Implementing both profiles in the Rust arena and importing
`ArenaScenarioV1` there is the arena owner's task (spec G.12); this branch does not touch the arena.

## Acceptance tests (spec F.3)

Every F.3 item has a test in `tests/test_cashout_*.py`:

| F.3 item | Test |
| --- | --- |
| `cashout_shortlist` never returns a SELL maker | `test_cashout_direction.test_cashout_shortlist_never_returns_a_sell_maker` |
| Buyer `shortlist` still returns only SELL makers | `test_cashout_direction.test_buyer_shortlist_still_returns_only_sell_makers` |
| Missing, stale, insufficient, wrong-key inventory excluded | `test_cashout_direction.test_{missing,stale,insufficient,wrong_key}_inventory_is_excluded` |
| Receive-exact and send-exact priced in Decimal | `test_cashout_direction.test_receive_exact_and_send_exact_use_decimal_with_explicit_rounding` |
| `nostr.json` key differs from the signer | `test_cashout_alias.test_nip05_key_differs_from_record_signer_is_binding_mismatch` |
| Bad signature, bad event ID | `test_cashout_alias.test_bad_signature_and_bad_event_id_are_rejected` |
| Seq below the pin | `test_cashout_alias.test_seq_lower_than_pin_is_rollback` |
| Same seq, different ID (frozen) | `test_cashout_alias.test_same_seq_different_id_is_equivocation_and_freezes` |
| `valid_until` passed | `test_cashout_alias.test_expired_record_is_refused` |
| Revoked: intent refused | `test_cashout_alias.test_revoked_alias_refuses_the_intent` |
| Rotation without `prev_sig` is a new identity | `test_cashout_alias.test_rotation_without_prev_sig_is_a_new_identity` |
| Recovery pending 72 h; old-key veto | `test_cashout_alias.test_recovery_rotation_is_pending_for_72_hours`, `test_old_key_veto_during_recovery_is_honored` |
| Non-ASCII local part; IDN as A-label | `test_cashout_alias.test_local_part_is_ascii_and_domain_is_compared_as_a_label` |
| Skeleton collision | `test_cashout_alias.test_skeleton_collisions` |
| `payto` tag or destination-like field | `test_cashout_alias.test_payto_tag_and_destination_fields_are_rejected` |
| Relay disagreement: warning or refusal | `test_cashout_alias.test_relay_disagreement_warns_and_refuses_above_threshold` |
| Golden vectors, four networks | `test_cashout_intent.test_intent_and_quote_encodings_match_golden_vectors_on_all_networks` |
| Wrong network; wrong commitment | `test_cashout_intent.test_wrong_network`, `test_wrong_recipient_commitment` |
| Substituted provider key | `test_cashout_intent.test_substituted_provider_key` |
| Quote for another `intent_id` | `test_cashout_intent.test_quote_for_a_different_intent` |
| Fee above cap; net below minimum | `test_cashout_intent.test_fee_above_cap_and_net_below_minimum` |
| Expired quote (height and time) | `test_cashout_intent.test_expired_quote_by_height_and_by_time` |
| Deposit script reused | `test_cashout_intent.test_deposit_script_reused_across_quotes`, `test_cashout_ledger.test_deposit_script_is_never_reused` |
| Wrong output (script, vout, value) | `test_cashout_evidence.test_wrong_output_script_vout_and_value` |
| Funding below `quote_created_height` | `test_cashout_evidence.test_funding_below_quote_created_height_is_a_replayed_old_output` |
| Late funding never paid at the old rate | `test_cashout_evidence.test_funding_after_expiry_is_late_and_never_paid_at_the_old_rate` |
| Stale chain (tip lag, off active chain) | `test_cashout_evidence.test_stale_chain_tip_lag_and_off_active_chain` |
| Confirmations below `min_conf` | `test_cashout_evidence.test_core_confirmations_below_min_conf` |
| `"verified": true` without a verifier | `test_cashout_evidence.test_verified_true_without_a_configured_verifier_is_rejected` |
| Candidate keys under production | `test_cashout_evidence.test_candidate_keys_under_production_mode_are_rejected` |
| Unpinned program words | `test_cashout_evidence.test_unpinned_program_words_are_rejected` |
| Same outpoint for two quotes | `test_cashout_ledger.test_same_outpoint_for_two_quotes_is_rejected` |
| Reorg below `min_conf` before authorization: FUNDING_SEEN | `test_cashout_evidence.test_reorg_before_authorization_returns_to_funding_seen` (fixture), `test_cashout_regtest` (regtest, below) |
| Same quote authorized twice: one `submit` | `test_cashout_ledger.test_same_quote_authorized_twice_submits_once` |
| Rail timeout: UNKNOWN, resolved by `status()` | `test_cashout_ledger.test_rail_timeout_is_unknown_never_resubmitted_and_resolved_only_by_status` |
| Rail failure: REFUND_DUE | `test_cashout_ledger.test_rail_failure_is_refund_due` |
| Duplicate webhook is a no-op | `test_cashout_ledger.test_duplicate_and_forged_webhooks_are_no_ops` |
| Crash between authorize/submit and submit/record | `test_cashout_ledger.test_crash_between_authorize_and_submit`, `test_crash_between_submit_and_recording_the_response` |
| Reservation released on expiry | `test_cashout_ledger.test_reservation_held_until_expiry_and_no_quote_on_unreserved_inventory` |
| Alias revoked or commitment changed: HOLD | `test_cashout_ledger.test_alias_revoked_between_quote_and_payout_holds`, `test_commitment_changed_between_quote_and_payout_holds`, `test_alias_revoked_after_authorization_holds_before_the_rail_call` |
| Receipt bound to the key, signed by P | `test_cashout_ledger.test_receipt_is_bound_to_the_idempotency_key_and_signed_by_the_provider` |
| `provider_attested_only` labelling | `test_cashout_ledger.test_provider_attested_only_when_rail_evidence_is_absent` |
| Policy DENY | `test_cashout_direction.test_cashout_policy_actions_are_denied` |
| Arena: `ROLE_MAP` provider is the `buyer` | `test_cashout_arena.test_role_map_keeps_the_provider_as_the_btc_buyer` |
| Arena: golden `scenario_id`, every behavior, both profiles | `test_cashout_arena.test_golden_scenario_ids_for_every_behavior_and_both_profiles` |
| Arena: `parse_arena_line` accepts real lines, rejects bad fields | `test_cashout_arena.test_parse_accepts_real_arena_lines`, `test_parse_rejects_unknown_missing_and_non_integer_fields` |
| Arena: every refused attack exports `passed=true, attack_success=false` | `test_cashout_demo.test_cli_demo_exits_zero_and_prints_every_refused_attack` |
| Arena: unmitigated `provider_withholds` is `attack_success=true`, sats at risk | `test_cashout_arena.test_unmitigated_provider_withholding_is_an_attack_success_with_sender_btc_at_risk`, `test_simulated_provider_withholding_and_claim_without_credit` |
| Arena: `claim_without_credit` never reaches RECEIPTED | `test_cashout_arena.test_simulated_provider_withholding_and_claim_without_credit` |
| Arena: conservation over every exported trace | `test_cashout_arena.test_conservation` (and every export runs `check_conservation`) |
| Arena: non-USD flagged `arena_supported: false` | `test_cashout_arena.test_non_usd_scenarios_are_flagged_unsupported` |
| BIP340 vectors, source and SHA-256 | `test_cashout_bip340.test_vendored_csv_matches_recorded_source_hash`, `test_every_vector_verifies_as_expected_and_signs_exactly` |

## Regtest evidence (spec G.5)

The acceptance predicate also runs against a real Bitcoin Core node. `ban.sim.regtest` starts a
private `bitcoind -regtest` (tested with Bitcoin Core 31.1) in a fresh throwaway datadir, with RPC
on 127.0.0.1 at a random port, cookie auth, no P2P listening and no outbound connections. Before
any wallet action, mining or broadcast (any RPC outside a short read-only list), it checks that
`getblockchaininfo.chain` is `regtest` and that block 0 is the regtest genesis, and refuses
otherwise. When it finishes it stops the node and deletes the datadir. Regtest coins have no
value, and nothing here can reach testnet, signet or mainnet.

    BAN_REGTEST=1 BAN_BITCOIND=/path/to/bitcoind PYTHONPATH=src python -m unittest tests.test_cashout_regtest
    PYTHONPATH=src python -m ban.cli cashout-demo --regtest --bitcoind /path/to/bitcoind

`BAN_REGTEST_ROOT` (or `--regtest-root`) picks the parent folder of the throwaway datadir; the
default is the system temp folder. Without `BAN_REGTEST=1` the node tests skip, so CI stays
offline. The guard and the RPC transport have unit tests that always run.

`RegtestWorld` is a `CashoutWorld` whose funding is a real wallet payment to the quote's deposit
script. The provider judges it with `CoreRpcEvidence`, from `scantxoutset` and block reads. The
`--regtest` demo runs the B.3 flow (T0a–T9) on the node, exports the honest arena line, and then
runs these cases, which the tests also assert:

| Case | On the regtest node | Outcome |
| --- | --- | --- |
| `accepted_at_min_conf` | Pay the script, mine 1, then 1 more | `FUNDING_SEEN` at 1 conf, `FUNDING_ACCEPTED` at `min_conf` = 2, one rail call |
| `underpay` | Pay `sats_required - 1` | `WrongOutput`; quote stays `QUOTED`, nothing bound |
| `overpay` | Pay `sats_required + 25,000` | Accepted at that value; the rail is paid only the quoted net (I6) |
| `late_funding` | Mine to the expiry height, then fund | Confirms at expiry + 1: `LATE_FUNDING`, authorize refused. A second quote funded in the block at its expiry height is accepted |
| `replayed_output` | Pre-fund the next quote's script, mine 2, then quote | `ReplayedOutput` (I7); quote stays `QUOTED` |
| `reorg_below_min_conf` | Accept, then `invalidateblock` the confirming block | Authorize re-judges: `FUNDING_SEEN`, no authorization; after `reconsiderblock`, accepted and paid once |
| `reorg_orphans_funding_block` | Accept, then `invalidateblock` the funding block | The tx returns to the mempool; `FUNDING_SEEN`; it confirms again in a new block, then accepted and paid once |
| `rbf_replacement` | Pay, `bumpfee`, mine | Only the replacement is unspent; exactly one `FUNDING_ACCEPTED`, bound to the replacement outpoint; the original outpoint is a `WrongOutput`; one rail call after three pay attempts |
| `outpoint_for_two_quotes` | Offer quote A's funding outpoint for quote B | Evidence refuses it (`WrongOutput`: wrong script); the ledger refuses it too (`ReplayRejected`, I2) |

The node tests also check that a real Core RPC error is an `EvidenceRejected`, and that a reorg
between `scantxoutset` and the block reads is a `StaleChain` (the reorg is triggered after each
read).

Found while running on a real node:

- **Fixed: Core RPC errors escaped as `HTTPError`.** Bitcoin Core answers JSON-RPC 1.0 errors with
  HTTP 500 and the error object in the body. `CoreRpcEvidence._call` used `urlopen`, which raised
  `urllib.error.HTTPError`, so the "not in the active-chain block" `StaleChain` branch could never
  fire. It now reads the body and raises `EvidenceRejected`. A reply with no error body (401 for a
  wrong cookie) still raises, because that is a node problem, not a rejection.
- **Fixed: loopback RPC honoured proxy variables.** With `HTTP_PROXY` set, `urlopen` would have sent
  the cookie to the proxy. `CoreRpcEvidence` and the harness now use a proxy-free opener.
- **Fixed: no reorg re-check before authorization.** The ledger had the B.4 edge
  `FUNDING_ACCEPTED -> FUNDING_SEEN`, but no code used it. `ReferenceProvider.authorize` now
  re-judges the accepted outpoint first. Any `EvidenceRejected` sends the quote back to
  `FUNDING_SEEN`. Other node errors propagate, and nothing is paid. A quote in `HOLD` is not
  re-judged when it is re-authorized, because B.4 has no `HOLD -> FUNDING_SEEN` edge.
- **Open: scan mode rejects on the lowest output.** Without a claim, `CoreRpcEvidence` (like
  `FixtureEvidence`) judges only the lowest-height unspent output that pays the script. A stale
  or short output to the script therefore masks a valid later payment until someone submits a
  claim. B.3 T4 says to accept "one qualifying output".
- **Open: underpay and overpay policies are recorded in the quote but not enforced.** An underpayment
  is refused, but no ledger row or `REFUND_DUE` is made. An overpayment is accepted, but the excess
  is not refunded.
- **By design: zero-conf is invisible.** `scantxoutset` reads the UTXO set, so the provider does
  not see a payment before its first confirmation.

## Departures from the spec

- **No earlier patch existed.** The `cashout.py` / `arena_contract.py` patch was searched for and
  confirmed absent on every repo, so both modules are new: `src/ban/cashout.py` (BUY-side
  selection) and `src/ban/arena_contract.py` (D.5).
- **Arena oracle `settled`.** D.5 names the oracles `blocked` and `exposure_observed`; the honest
  scenario needs a third, `settled` (the arena's "Settlement").
- **`claim_without_credit` is `exposure_observed` in `cashout-btc-first`.** The false claim is
  refused (no receipt, and the recipient is never credited by it), but the sender's BTC stays held
  unpaid, which is the same exposure as withholding. In `cashout-bisq-easy` it is `blocked`.
- **Arena `control_state` fixtures are instantiated from the format string** at
  `src/control.rs:401-402` with illustrative values; only the README:31 line is a verbatim output.
  Building and running the arena binary to capture real lines was not done.
- **Rotation signatures sign a rotation statement, not the new event ID.** The event ID commits to
  the content that carries `prev_sig`, so signing it is circular. The old key (or the recovery
  key) signs `SHA256("CAG-ALIAS-ROTATE-V1\0" | "CAG-ALIAS-RECOVER-V1\0" ‖ name ‖ 0 ‖ new key ‖ seq ‖ prev record)`.
- **The recovery window starts when the resolver first sees the recovery record**, not at its
  signer-chosen `created_at`. A veto is a record by the previous key at or above the recovery's
  seq, seen within the window.
- **`PayoutIntentV1` carries `sender_pubkey`**, so the provider can check the ephemeral signature.
- **`InventoryAttestationV1` carries the Bisq `offer_id`** (hashed in the encoding), so an
  attestation binds one offer. The maker's key is a provider key from Alice's record for the
  intent's commitment (`providers=`), because BAN's offer model carries no maker key.
- **`EvidencePolicyV1` is named by a digest of its TL-Canon JSON**, not a binary encoding.
- **IDNA:** the standard library has only IDNA 2003. Labels are NFC-normalized, lowercased,
  limited to letter, mark and digit categories, and punycoded; full IDNA 2008 contextual and Bidi
  rules and UTS #39 confusable tables are not implemented. The skeleton helper covers the ASCII
  folds of spec C.7, which suffice because local parts are ASCII.
- **Signing lives in `ban.sim.testkeys`**, not `tests/`, because the simulated provider signs
  quotes and receipts at runtime in the demo. Modules outside `ban.sim` only verify; a test
  enforces that they never import `ban.sim`.
- **Ledger edge `EXPIRED -> FUNDING_SEEN -> LATE_FUNDING`** is added for funding first seen after
  the expiry sweep. The sweep scans for funding before expiring, so funding confirmed at or below
  `quote_expiry_height` is in time whichever runs first. `LATE_FUNDING -> REQUOTED` is a terminal marker only: re-quoting would bind
  the same outpoint to a new quote, which I2 forbids, so it needs its own design.
- **The provider's destination vault** seals destinations with a simulation-only HMAC-SHA256
  keystream under a label-derived key. It is not reviewed encryption.
- **The default demo uses fixture funding.** `cashout-demo --regtest` funds on a private regtest
  node instead (see [Regtest evidence](#regtest-evidence-spec-g5)). The older env-gated test against
  a user-run node (`BAN_REGTEST_RPC_URL`) is kept.
- **Not built:** `RecipientAckV1`, `RefundRecordV1` and `ReconciliationRecordV1` as binary
  contracts (reconciliation is a ledger transition with the statement line as evidence), NIP-44/59
  transport, live relays, real Jawwal calls, the zk-rusto client, BitVM bonds and Cashu.
