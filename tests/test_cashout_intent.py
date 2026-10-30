from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from ban.intent import (NETWORKS, ContractError, FundingAcceptanceV1, PayoutIntentV1, ProviderQuoteV1,
                        QuoteRejected, check_quote, standard_script_type)
from ban.sim.world import CashoutWorld

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "cashout_golden_vectors.json"


def h(label: str) -> str:
    return sha256(label.encode("ascii")).hexdigest()


def sample_intent(network: str) -> PayoutIntentV1:
    return PayoutIntentV1(
        network=network, sender_pubkey=h("sender"), alias_pubkey=h("alias"), alias_record_id=h("record"),
        alias_seq=7, recipient_commitment=h("commitment"), rail="jawwal_pay", currency="ILS", exponent=2,
        amount_mode="receive_exact", amount=5000, max_total_fee_sats=5000, max_fee_bps=150,
        min_net_payout_minor=5000, refund_script_sha256=h("refund"), sender_nonce=h("nonce"),
        expiry_unix=1_793_007_200, funding_source="held_btc")


def sample_quote(network: str) -> ProviderQuoteV1:
    return ProviderQuoteV1(
        network=network, intent_id=sample_intent(network).intent_id(), provider_pubkey=h("provider"),
        reservation_id=h("reservation"), deposit_script="5120" + h("deposit"), sats_required=13293,
        overpay_policy="refund_excess", underpay_policy="refund_minus_fee", rate_minor_per_btc=38_000_000,
        currency="ILS", exponent=2, fee_sats=135, fee_minor=51, net_payout_minor=5000,
        quote_created_height=200, quote_expiry_height=206, quote_expiry_unix=1_793_003_600, min_conf=2,
        max_tip_lag=2, evidence_policy_digest=h("policy"))


def golden_vectors() -> dict:
    out = {}
    for network in NETWORKS:
        intent, quote = sample_intent(network), sample_quote(network)
        out[network] = {"intent_hex": intent.encode().hex(), "intent_id": intent.intent_id(),
                        "quote_hex": quote.encode().hex(), "quote_id": quote.quote_id(),
                        "idempotency_key": quote.idempotency_key()}
    return out


class GoldenVectorTests(unittest.TestCase):
    def test_intent_and_quote_encodings_match_golden_vectors_on_all_networks(self):
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(expected["source"], "SYNTHETIC_FIXTURE")
        self.assertEqual(set(expected["vectors"]), {"mainnet", "testnet4", "signet", "regtest"})
        self.assertEqual(golden_vectors(), expected["vectors"])
        for network, v in expected["vectors"].items():
            with self.subTest(network=network):
                self.assertEqual(bytes.fromhex(v["intent_hex"])[1], NETWORKS[network])
                self.assertEqual(PayoutIntentV1.decode(bytes.fromhex(v["intent_hex"]), "jawwal_pay"),
                                 sample_intent(network))
                self.assertEqual(ProviderQuoteV1.decode(bytes.fromhex(v["quote_hex"])), sample_quote(network))
        ids = {v["intent_id"] for v in expected["vectors"].values()} | {v["quote_id"] for v in expected["vectors"].values()}
        self.assertEqual(len(ids), 8)  # the network byte separates every ID

    def test_encodings_fail_closed(self):
        with self.assertRaises(ContractError):
            replace(sample_intent("regtest"), network="litecoin").encode()
        with self.assertRaises(ContractError):
            replace(sample_intent("regtest"), alias_pubkey=h("x").upper()).encode()
        with self.assertRaises(ContractError):
            replace(sample_intent("regtest"), amount=0).encode()
        with self.assertRaises(ContractError):
            replace(sample_intent("regtest"), currency="ils").encode()
        with self.assertRaises(ContractError):
            replace(sample_quote("regtest"), quote_expiry_height=200).encode()
        data = sample_quote("regtest").encode()
        with self.assertRaises(ContractError):
            ProviderQuoteV1.decode(data + b"\x00")
        with self.assertRaises(ContractError):
            PayoutIntentV1.decode(sample_intent("regtest").encode(), "other_rail")
        self.assertEqual(standard_script_type(bytes.fromhex("0014" + "00" * 20)), "p2wpkh")
        self.assertIsNone(standard_script_type(bytes.fromhex("76a914" + "00" * 20 + "88ac")))

    def test_funding_acceptance_binds_network_and_outpoint(self):
        base = FundingAcceptanceV1("regtest", h("q"), h("tx"), 0, 13293, h("s"), h("b"), 201, 2, h("tip"),
                                   "own_core_v1", h("e"))
        self.assertNotEqual(base.digest(), replace(base, network="signet").digest())
        self.assertNotEqual(base.digest(), replace(base, vout=1).digest())
        self.assertEqual(base.outpoint, h("tx") + ":0")


class QuoteCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = CashoutWorld(self.temp.name)
        self.w.enroll()
        self.res = self.w.resolve()
        self.intent, self.intent_sig = self.w.intent(self.res)
        self.quote, self.sig = self.w.provider.quote(self.intent, self.intent_sig, tip_height=self.w.chain.tip_height,
                                                     now=self.w.now)

    def tearDown(self):
        self.temp.cleanup()

    def check(self, quote=None, sig=None, intent=None, **kw):
        quote = quote or self.quote
        args = dict(tip_height=self.w.chain.tip_height, now=self.w.now)
        args.update(kw)
        check_quote(quote, sig or self.w.p1.sign_hex(quote.quote_id()), intent or self.intent, self.res, **args)

    def assertRejected(self, pattern, **kw):
        with self.assertRaisesRegex(QuoteRejected, pattern):
            self.check(**kw)

    def test_honest_quote_passes_and_same_intent_gets_same_quote(self):
        self.check(sig=self.sig)
        again = self.w.provider.quote(self.intent, self.intent_sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.assertEqual(again, (self.quote, self.sig))

    def test_wrong_network(self):
        self.assertRejected("wrong network", quote=replace(self.quote, network="mainnet"))

    def test_wrong_recipient_commitment(self):
        other, _ = self.w.intent(self.res, recipient_commitment=h("someone-else"))
        self.assertRejected("wrong recipient commitment", intent=other,
                            quote=replace(self.quote, intent_id=other.intent_id()))

    def test_substituted_provider_key(self):
        forged = replace(self.quote, provider_pubkey=self.w.attacker.pubkey)
        self.assertRejected("substituted provider key", quote=forged, sig=self.w.attacker.sign_hex(forged.quote_id()))
        # A record-listed key cannot be borrowed either: the signature must be by that key.
        self.assertRejected("bad quote signature", sig=self.w.attacker.sign_hex(self.quote.quote_id()))

    def test_quote_for_a_different_intent(self):
        other, _ = self.w.intent(self.res)
        self.assertRejected("different intent_id", intent=other)

    def test_fee_above_cap_and_net_below_minimum(self):
        capped, capped_sig = self.w.intent(self.res, max_total_fee_sats=100)
        quote, sig = self.w.provider.quote(capped, capped_sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.assertRejected("fee above", quote=quote, sig=sig, intent=capped)
        bps, bps_sig = self.w.intent(self.res, max_fee_bps=50)
        quote, sig = self.w.provider.quote(bps, bps_sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.assertRejected("fee above", quote=quote, sig=sig, intent=bps)
        send, send_sig = self.w.intent(self.res, amount_mode="send_exact", amount=13_000, min_net_payout_minor=5000)
        quote, sig = self.w.provider.quote(send, send_sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.assertLess(quote.net_payout_minor, 5000)
        self.assertRejected("net payout below", quote=quote, sig=sig, intent=send)

    def test_expired_quote_by_height_and_by_time(self):
        self.assertRejected(r"expired quote \(height\)", tip_height=self.quote.quote_expiry_height)
        self.assertRejected(r"expired quote \(time\)", now=self.quote.quote_expiry_unix)

    def test_deposit_script_reused_across_quotes(self):
        self.assertRejected("reused", seen_deposit_scripts={self.quote.deposit_script_sha256})
        other, other_sig = self.w.intent(self.res)
        quote2, _ = self.w.provider.quote(other, other_sig, tip_height=self.w.chain.tip_height, now=self.w.now)
        self.assertNotEqual(quote2.deposit_script, self.quote.deposit_script)

    def test_amounts_must_match_the_stated_rate(self):
        self.assertRejected("disagree", quote=replace(self.quote, sats_required=self.quote.sats_required + 1))
        self.assertRejected("non-standard", quote=replace(self.quote, deposit_script="76a914" + "00" * 20 + "88ac"))


if __name__ == "__main__":
    unittest.main()
