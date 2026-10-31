"""Synthetic-only qualification tests; no authenticated node or real wallet."""
from dataclasses import replace
from types import SimpleNamespace
import json
import unittest

from ban.corridor_preflight import RailCapabilities, illustrative_ils_minor, qualify_corridor

NOW = 1793000000
TARGET = 8750  # SYNTHETIC: $25.00 * 3.50 ILS/USD * 100


def intent(**changes):
    obj = dict(currency="ILS", rail="jawwal_pay", exponent=2, amount_mode="receive_exact",
               amount=TARGET, recipient_commitment="ab" * 32, expiry_unix=NOW + 1200)
    obj.update(changes)
    return SimpleNamespace(**obj)


def shortlist(**changes):
    offer = dict(offer_id="SYNTHETIC-ILS-BUY-001", maker_direction="BUY", currency="ILS",
                 provider_pubkey="cd" * 32, amount_eligibility="within_advertised_bounds",
                 indicative_net_payout_minor=TARGET,
                 inventory=dict(source="provider_self_asserted", available_minor=20000,
                                max_per_payout_minor=12000, observed_at=NOW - 10,
                                valid_until=NOW + 120))
    obj = dict(source="SYNTHETIC_FIXTURE", direction="cashout_maker_buys_btc", rail="jawwal_pay",
               currency="ILS", amount_mode="receive_exact", amount=TARGET,
               requires_human_trade_confirmation=True, offers=[offer])
    obj.update(changes)
    return obj


class CorridorPreflightTests(unittest.TestCase):
    def assess(self, s=None, i=None, r=None, now=NOW):
        return qualify_corridor(s if s is not None else shortlist(), i or intent(),
                                r or RailCapabilities.synthetic(), now=now)

    def test_25_usd_synthetic_equivalent_and_status(self):
        self.assertEqual(illustrative_ils_minor("25.00", "3.50"), TARGET)
        result = self.assess()
        self.assertEqual(result["status"], "SIMULATION_PASS")
        self.assertEqual(result["indicative_offer_count"], 1)
        self.assertEqual(result["net_payout_minor"], TARGET)
        self.assertFalse(result["payment_execution_enabled"])
        self.assertFalse(result["live_rail_access_verified"])
        self.assertFalse(result["executable_liquidity_verified"])
        self.assertNotIn("recipient_commitment", result)
        self.assertNotIn("phone", json.dumps(result).lower())

    def test_money_validation_rejects_floats_nan_negative_and_overflow(self):
        for amount, rate in [(25.0, "3.5"), ("NaN", "3.5"), ("-1", "3.5"),
                             ("25", "0"), ("Infinity", "1"), ("9999999999999999999", "3.5")]:
            with self.subTest(amount=amount, rate=rate), self.assertRaises(ValueError):
                illustrative_ils_minor(amount, rate)
        self.assertEqual(illustrative_ils_minor("0.01", "3.50"), 4)

    def test_no_offers_is_blocked(self):
        result = self.assess(shortlist(offers=[]))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("no_qualified_provider_self_attested_inventory", result["blockers"])

    def test_sell_direction_never_qualifies(self):
        s = shortlist()
        s["offers"][0]["maker_direction"] = "SELL"
        self.assertEqual(self.assess(s)["indicative_offer_count"], 0)

    def test_stale_expired_or_forged_float_is_blocked(self):
        for key, value in [("available_minor", 100), ("max_per_payout_minor", 100),
                           ("observed_at", NOW-800), ("valid_until", NOW-1),
                           ("source", "UNVERIFIED")]:
            with self.subTest(key=key):
                s = shortlist()
                s["offers"][0]["inventory"][key] = value
                self.assertEqual(self.assess(s)["indicative_offer_count"], 0)
        s = shortlist()
        s["offers"][0]["inventory"]["available_minor"] = True
        self.assertEqual(self.assess(s)["indicative_offer_count"], 0)

    def test_any_real_market_source_remains_nonexecutable(self):
        result = self.assess(shortlist(source="BISQ2_LOCAL"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("no_executable_live_buy_bid_verified", result["blockers"])
        self.assertFalse(result["payment_execution_enabled"])

    def test_self_asserted_live_capabilities_are_not_approval(self):
        caps = replace(RailCapabilities.synthetic(), source="OPERATOR_REPORTED")
        result = self.assess(r=caps)
        self.assertIn("rail_authorization_requires_independent_review", result["blockers"])
        self.assertFalse(result["live_rail_access_verified"])

    def test_missing_rail_capabilities_are_explicit(self):
        result = self.assess(r=RailCapabilities())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(len([b for b in result["blockers"] if b.startswith("rail_")]), 9)

    def test_third_party_payment_not_approved(self):
        caps = replace(RailCapabilities.synthetic(), third_party_recipient_allowed=False)
        result = self.assess(r=caps)
        self.assertIn("rail_third_party_recipient_allowed_unconfirmed", result["blockers"])

    def test_expired_intent_rejected(self):
        self.assertIn("intent_expired", self.assess(i=intent(expiry_unix=NOW))["blockers"])

    def test_wrong_rail_currency_or_amount_refused(self):
        for changes in ({"rail":"palpay"}, {"currency":"USD"}, {"amount_mode":"send_exact"},
                        {"exponent":4}):
            with self.subTest(changes=changes):
                self.assertIn("requires_ils_jawwal_receive_exact_intent",
                              self.assess(i=intent(**changes))["blockers"])
        self.assertIn("shortlist_intent_mismatch", self.assess(shortlist(amount=1))["blockers"])
        self.assertIn("shortlist_intent_mismatch", self.assess(shortlist(direction="buyer_sells_btc"))["blockers"])

    def test_commitment_must_be_canonical_and_is_never_output(self):
        for c in ("0" * 63, "ZZ" * 32, "wallet-123456"):
            self.assertIn("recipient_commitment_not_canonical",
                          self.assess(i=intent(recipient_commitment=c))["blockers"])
        self.assertNotIn("ab" * 32, json.dumps(self.assess()))

    def test_unknown_market_and_no_human_confirmation_fail_closed(self):
        result = self.assess(shortlist(source="USER_SUPPLIED", requires_human_trade_confirmation=False))
        self.assertIn("human_trade_confirmation_required", result["blockers"])
        self.assertIn("no_executable_live_buy_bid_verified", result["blockers"])

    def test_usdt_trc20_not_implicitly_supported(self):
        self.assertIn("USDT_TRC20_requires_separate_authorized_conversion", self.assess()["funding_path"])

    def test_noninteger_time_refused(self):
        with self.assertRaises(ValueError):
            self.assess(now=True)


if __name__ == "__main__":
    unittest.main()
