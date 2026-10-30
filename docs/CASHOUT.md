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
E, F). Run the demo:

    PYTHONPATH=src python -m ban.cli cashout-demo --fixture fixtures/cashout_demo.json

It prints each B.3 transition (T0a–T9) against a synthetic world, then replays 30 attacks. It
exits non-zero if any attack is not refused.

## Flow

1. **Alias (T0a, T0b, T1).** `alice@pay.test` is bound to a key by the domain's NIP-05 document
   (fixture source; redirects refused) and to content by a BIP340-signed NIP-01 addressable event
   carrying `AliasRecordV1` in TL-Canon v1. The record holds per-provider payout
   **commitments**, never a destination. A lint refuses `payto` tags and destination-like fields
   or values.
2. **Enrollment (T0c).** Alice privately sends P1 a signed `DestinationEnrollmentV1` that opens
   the commitment. P1 stores the destination in its vault keyed by (alias key, commitment) and
   replies with a signed `EnrollmentAckV1`.
3. **Selection.** `cashout_select.cashout_shortlist` keeps only makers **BUYING** BTC that carry
   a fresh `InventoryAttestationV1` signed by a provider key listed in Alice's record, with enough
   capacity. `concierge.shortlist` is unchanged and still keeps only makers SELLING BTC.
4. **Intent and quote (T2, T3).** The sender signs `PayoutIntentV1` with an ephemeral key. P1
   reserves float and signs `ProviderQuoteV1` with a fresh deposit script. The sender runs
   `intent.check_quote`.
5. **Funding (T4, T5).** P1 judges the deposit from its own node's view (`evidence.FixtureEvidence`
   in the demo, or `CoreRpcEvidence` against a loopback regtest node).
6. **Payout (T6–T9).** P1 re-checks the alias, writes `PayoutAuthorizationV1` before any rail
   call, writes the single rail attempt, calls the mock rail, issues a signed `PayoutReceiptV1`
   and reconciles against the rail statement.

## Trust boundaries (from spec E, the rows that apply here)

| Boundary | What establishes it | What does **not** | State in this slice |
| --- | --- | --- | --- |
| Name binding | Domain NIP-05 (TLS/DNS) plus R's signature plus the sender's pubkey pin | Relays; the handle string | Built against fixtures: resolver with SQLite pins, quorum of 2, rollback/equivocation/expiry/revocation, rotation and 72 h recovery with veto |
| Destination privacy | Commitments in public; enrollment only to P | Gift wrap alone if destinations are published elsewhere (NIP-A3) | Commitments and lint built; enrollment travels in-process (NIP-17/44/59 deferred) |
| Provider liquidity | Funded float plus a reservation honored, attested by P | `InventoryAttestationV1` (self-asserted), BAN advertised bounds | Reservations built in the simulated ledger; attestations only narrow the shortlist |
| Freshness | Node tip versus evidence tip (`max_tip_lag`) and record `valid_until` | In-proof `confirmations` | Enforced by `evidence.judge` and the resolver |
| Active-chain acceptance | The node's active chain at lookup | Proven chainwork alone | Fixture chain only; `CoreRpcEvidence` reads a regtest node |
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
| BIP340 vectors, source and SHA-256 | `test_cashout_bip340.test_vendored_csv_matches_recorded_source_hash`, `test_every_vector_verifies_as_expected_and_signs_exactly` |

## Departures from the spec

- **BUY-side selection is `src/ban/cashout_select.py`.** The local `cashout.py` /
  `arena_contract.py` patch was searched for and not found, so H step 1's fallback applies.
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
- **Ledger edge `EXPIRED -> FUNDING_SEEN -> LATE_FUNDING`** is added for funding that arrives after
  the expiry sweep. `LATE_FUNDING -> REQUOTED` is a terminal marker only: re-quoting would bind
  the same outpoint to a new quote, which I2 forbids, so it needs its own design.
- **The provider's destination vault** seals destinations with a simulation-only HMAC-SHA256
  keystream under a label-derived key. It is not reviewed encryption.
- **The demo uses fixture funding only.** Real regtest funding when `BAN_REGTEST_RPC_URL` is set
  is not wired into the demo. `CoreRpcEvidence` exists, and an env-gated test checks it against a
  user-run node.
- **Not built:** `RecipientAckV1`, `RefundRecordV1` and `ReconciliationRecordV1` as binary
  contracts (reconciliation is a ledger transition with the statement line as evidence), NIP-44/59
  transport, live relays, real Jawwal calls, the zk-rusto client, BitVM bonds and Cashu.
