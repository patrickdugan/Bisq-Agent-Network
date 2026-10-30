import json
import tempfile
import unittest

from ban.alias import (ALIAS_KIND, RECOVERY_WINDOW_SECONDS, AliasResolver, BindingMismatch, DestinationLeak,
                       Equivocation, Expired, FixtureNip05, InvalidHandle, InvalidRecord, PinStore,
                       QuorumUnavailable, RecoveryPending, Revoked, Rollback, StaleResolution, display_domain,
                       event_id, lint_public_content, normalize_handle, parse_record, skeleton, skeleton_collision,
                       within_one_edit)
from ban.intent import ProviderQuoteV1, QuoteRejected, check_quote
from ban.sim.testkeys import TestKey
from ban.sim.world import CashoutWorld, recovery_rotation, rotation


class AliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = CashoutWorld(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def rebind(self, pubkey):
        self.w.nip05.documents[self.w.domain] = {"names": {self.w.handle: pubkey}}

    def seq2(self, **kw):
        return self.w.record(self.w.alice, seq=2, prev=self.w.record1["id"], **kw)

    # -- binding and event integrity
    def test_first_contact_resolves_and_pins(self):
        res = self.w.resolve()
        self.assertEqual((res.identity, res.record.seq, res.record.pubkey), ("first_contact", 1, self.w.alice.pubkey))
        self.assertEqual(self.w.sender_resolver.pins.get(self.w.name).seq, 1)

    def test_nip05_key_differs_from_record_signer_is_binding_mismatch(self):
        self.rebind(self.w.attacker.pubkey)
        with self.assertRaises(BindingMismatch):
            self.w.resolve()

    def test_nip05_redirect_is_refused(self):
        self.w.nip05.documents[self.w.domain] = {"names": {self.w.handle: self.w.alice.pubkey},
                                                 "_redirect_to": "https://evil.test/.well-known/nostr.json?name=alice"}
        with self.assertRaises(BindingMismatch):
            self.w.resolve()

    def test_bad_signature_and_bad_event_id_are_rejected(self):
        event = dict(self.w.record1)
        event["sig"] = ("0" if event["sig"][0] != "0" else "1") + event["sig"][1:]
        with self.assertRaisesRegex(InvalidRecord, "bad signature"):
            parse_record(event, now=self.w.now)
        event = dict(self.w.record1, id="ab" * 32)
        with self.assertRaisesRegex(InvalidRecord, "event id mismatch"):
            parse_record(event, now=self.w.now)
        event = dict(self.w.record1, content=self.w.record1["content"].replace('"seq":1', '"seq":9'))
        with self.assertRaisesRegex(InvalidRecord, "event id mismatch"):
            parse_record(event, now=self.w.now)

    def test_non_canonical_content_is_rejected(self):
        bad = self.w.record(self.w.alice, seq=1)
        bad_text = bad["content"].replace(",", ", ", 1)
        eid = event_id(bad["pubkey"], bad["created_at"], ALIAS_KIND, bad["tags"], bad_text)
        forged = dict(bad, content=bad_text, id=eid, sig=self.w.alice.sign_hex(eid))
        with self.assertRaisesRegex(InvalidRecord, "TL-Canon"):
            parse_record(forged, now=self.w.now)

    # -- monotonic pin, equivocation, expiry, revocation
    def test_seq_lower_than_pin_is_rollback(self):
        self.w.publish(self.seq2())
        self.assertEqual(self.w.resolve().record.seq, 2)
        self.w.withdraw_all()
        self.w.publish(self.w.record1)
        with self.assertRaises(Rollback):
            self.w.resolve()

    def test_same_seq_different_id_is_equivocation_and_freezes(self):
        self.w.resolve()
        a = self.seq2()
        b = self.seq2(valid_seconds=29 * 86400)
        self.w.publish(a, relays=[0])
        self.w.publish(b, relays=[1])
        with self.assertRaises(Equivocation):
            self.w.resolve()
        self.assertEqual(self.w.sender_resolver.pins.get(self.w.name).frozen_seq, 2)
        self.w.relays[1].events.remove(b)
        self.w.publish(a, relays=[1])
        with self.assertRaises(Equivocation):  # still frozen though relays now agree on seq 2
            self.w.resolve()
        self.w.publish(self.w.record(self.w.alice, seq=3, prev=a["id"]))
        res = self.w.resolve()
        self.assertEqual(res.record.seq, 3)
        self.assertIsNone(self.w.sender_resolver.pins.get(self.w.name).frozen_seq)

    def test_expired_record_is_refused(self):
        self.w.withdraw_all()
        self.w.publish(self.w.record(self.w.alice, seq=1, created_at=self.w.now - 10 * 86400,
                                     valid_seconds=5 * 86400))
        with self.assertRaises(Expired):
            self.w.resolve()

    def test_revoked_alias_refuses_the_intent(self):
        res = self.w.resolve()
        self.w.publish(self.seq2(status="revoked"))
        with self.assertRaises(Revoked):
            self.w.resolve()
        self.assertEqual(self.w.sender_resolver.pins.get(self.w.name).seq, 2)  # pinned: no rollback to seq 1
        self.w.withdraw_all()
        self.w.publish(self.w.record1)
        with self.assertRaises(Rollback):
            self.w.resolve()
        revoked = parse_record(self.seq2(status="revoked"), now=self.w.now)
        stale = type(res)(revoked, res.identity, res.bound_pubkey, res.relay_heads)
        intent, _ = self.w.intent(stale)
        with self.assertRaisesRegex(QuoteRejected, "not active"):
            self._check_against(intent, stale)

    def _check_against(self, intent, resolution):
        # A syntactically valid quote; the record status check fires before anything quote-specific.
        quote = ProviderQuoteV1(intent.network, intent.intent_id(), self.w.p1.pubkey, "00" * 32, "5120" + "33" * 32,
                                1, "refund_excess", "refund_minus_fee", 1, "ILS", 2, 0, 0, 1, 1, 2, self.w.now + 1, 1,
                                0, "00" * 32)
        check_quote(quote, "00" * 64, intent, resolution, tip_height=1, now=self.w.now)

    # -- rotation and recovery
    def test_rotation_with_old_key_signature_is_accepted(self):
        self.w.resolve()
        new = TestKey("sim/alice-v2")
        rot = rotation(self.w.alice, new, name=self.w.name, seq=2, prev_record=self.w.record1["id"])
        self.w.publish(self.w.record(new, seq=2, prev=self.w.record1["id"], rotation_obj=rot))
        self.rebind(new.pubkey)
        res = self.w.resolve()
        self.assertEqual((res.identity, res.record.pubkey), ("rotated", new.pubkey))

    def test_rotation_without_prev_sig_is_a_new_identity(self):
        self.w.resolve()
        self.w.publish(self.w.record(self.w.attacker, seq=2, prev=self.w.record1["id"]))  # domain-only rebind
        self.rebind(self.w.attacker.pubkey)
        res = self.w.resolve()
        self.assertEqual(res.identity, "new_identity")
        self.assertIn("new_recipient", res.warnings)
        self.assertEqual(self.w.sender_resolver.pins.get(self.w.name).pubkey, self.w.attacker.pubkey)
        # A rotation whose prev_sig is not by the pinned key does not even parse.
        forged = rotation(self.w.attacker, self.w.attacker, name=self.w.name, seq=2, prev_record=self.w.record1["id"])
        forged["prev_pubkey"] = self.w.alice.pubkey
        with self.assertRaisesRegex(InvalidRecord, "prev_sig"):
            parse_record(self.w.record(TestKey("sim/x"), seq=2, prev=self.w.record1["id"], rotation_obj=forged),
                         now=self.w.now)

    def test_recovery_rotation_is_pending_for_72_hours(self):
        self.w.resolve()
        new = TestKey("sim/alice-recovered")
        rot = recovery_rotation(self.w.recovery, self.w.alice.pubkey, new, name=self.w.name, seq=2,
                                prev_record=self.w.record1["id"])
        self.w.publish(self.w.record(new, seq=2, prev=self.w.record1["id"], rotation_obj=rot))
        self.rebind(new.pubkey)
        with self.assertRaises(RecoveryPending):
            self.w.resolve()
        self.w.now += RECOVERY_WINDOW_SECONDS - 1
        with self.assertRaises(RecoveryPending):
            self.w.resolve()
        self.w.now += 1
        res = self.w.resolve()
        self.assertEqual((res.identity, res.record.pubkey), ("recovered", new.pubkey))

    def test_old_key_veto_during_recovery_is_honored(self):
        self.w.resolve()
        new = TestKey("sim/alice-thief")
        rot = recovery_rotation(self.w.recovery, self.w.alice.pubkey, new, name=self.w.name, seq=2,
                                prev_record=self.w.record1["id"])
        self.w.publish(self.w.record(new, seq=2, prev=self.w.record1["id"], rotation_obj=rot))
        self.rebind(new.pubkey)
        with self.assertRaises(RecoveryPending):
            self.w.resolve()
        self.w.now += 3600
        veto = self.w.record(self.w.alice, seq=3, prev=self.w.record1["id"])
        self.w.publish(veto)
        res = self.w.resolve()
        self.assertEqual((res.identity, res.record.pubkey, res.record.seq), ("recovery_vetoed", self.w.alice.pubkey, 3))
        self.w.now += RECOVERY_WINDOW_SECONDS
        self.assertEqual(self.w.resolve().identity, "recovery_vetoed")

    def test_recovery_cannot_link_below_the_pin_with_a_retired_recovery_key(self):
        rotated = TestKey("sim/alice-recovery-2")
        self.w.publish(self.seq2(recovery_pubkey=rotated.pubkey))
        self.assertEqual(self.w.resolve().record.recovery_pubkey, rotated.pubkey)
        self.w.withdraw_all()
        self.w.publish(self.w.record1)  # hide seq 2; seq 1 still names the retired recovery key
        thief = TestKey("sim/thief")
        rot = recovery_rotation(self.w.recovery, self.w.alice.pubkey, thief, name=self.w.name, seq=2,
                                prev_record=self.w.record1["id"])
        self.w.publish(self.w.record(thief, seq=2, prev=self.w.record1["id"], rotation_obj=rot))
        self.w.publish(self.w.record(thief, seq=11, prev="ab" * 32))
        self.rebind(thief.pubkey)
        self.w.now += RECOVERY_WINDOW_SECONDS + 1
        res = self.w.resolve()
        self.assertEqual(res.identity, "new_identity")  # not "recovered": the sender is warned
        self.assertIn("new_recipient", res.warnings)

    def test_freeze_is_checked_inside_the_pin_transaction(self):
        pins = PinStore(self.temp.name + "/race.sqlite3")
        pins.advance(parse_record(self.w.record1, now=self.w.now), self.w.now)
        pins.freeze(self.w.name, 2)  # another resolver saw two seq-2 records meanwhile
        with self.assertRaises(Equivocation):
            pins.advance(parse_record(self.seq2(), now=self.w.now), self.w.now)
        pins.advance(parse_record(self.w.record(self.w.alice, seq=3, prev="ab" * 32), now=self.w.now), self.w.now)
        self.assertIsNone(pins.get(self.w.name).frozen_seq)

    # -- names
    def test_local_part_is_ascii_and_domain_is_compared_as_a_label(self):
        for bad in ("alicé@pay.test", "аlice@pay.test", "al ice@pay.test", "alice@@pay.test", "alice", "a+b@pay.test"):
            with self.subTest(bad=bad), self.assertRaises(InvalidHandle):
                normalize_handle(bad)
        self.assertEqual(normalize_handle("Alice@Pay.Test"), ("alice", "pay.test"))
        self.assertEqual(normalize_handle("alice@bücher.example"), ("alice", "xn--bcher-kva.example"))
        self.assertEqual(normalize_handle("alice@XN--BCHER-KVA.example"), normalize_handle("alice@BÜCHER.example"))
        with self.assertRaises(InvalidHandle):
            normalize_handle("alice@xn--abc-.example")
        self.assertEqual(display_domain("xn--bcher-kva.example"), "bücher.example")
        mixed = normalize_handle("x@аpple.com")[1]  # Cyrillic a + Latin "pple"
        self.assertTrue(mixed.startswith("xn--"))
        self.assertEqual(display_domain(mixed), mixed)

    def test_skeleton_collisions(self):
        self.assertEqual(skeleton("a1ice"), skeleton("alice"))
        self.assertEqual(skeleton_collision("a1ice", ["bob", "alice"]), "alice")
        self.assertEqual(skeleton_collision("al_ice", ["alice"]), "alice")
        self.assertEqual(skeleton_collision("rnallory", ["mallory"]), "mallory")
        self.assertEqual(skeleton_collision("vvalt", ["walt"]), "walt")
        self.assertIsNone(skeleton_collision("bob", ["alice"]))
        self.assertTrue(within_one_edit("alice", "alicf"))
        self.assertFalse(within_one_edit("alice", "bob"))

    # -- no destination in public data
    def test_payto_tag_and_destination_fields_are_rejected(self):
        with self.assertRaises(DestinationLeak):
            parse_record(self.w.record(self.w.alice, seq=1, extra_tags=[["payto", "revolut", "@alice"]]),
                         now=self.w.now)
        leaky = {"v": 1, "handle": "alice", "domain": "pay.test", "seq": 1, "prev": None, "status": "active",
                 "valid_until": self.w.now + 86400, "recovery_pubkey": self.w.recovery.pubkey,
                 "payout": [dict(self.w.target, destination="SYNTHETIC-JAWWAL-WALLET-0001")], "rotation": None}
        with self.assertRaises(DestinationLeak):
            parse_record(self.w.record(self.w.alice, seq=1, content_override=leaky), now=self.w.now)
        for value in ({"note": "+970 599 123 456"}, {"wallet_id": "x"}, {"memo": "DE89370400440532013000"},
                      {"x": ["payto://iban/DE89"]}):
            with self.subTest(value=value), self.assertRaises(DestinationLeak):
                lint_public_content(value)
        lint_public_content(json.loads(self.w.record1["content"]))  # the honest record passes

    # -- relay quorum
    def test_relay_disagreement_warns_and_refuses_above_threshold(self):
        self.w.publish(self.seq2(), relays=[0])
        res = self.w.resolve()
        self.assertEqual(res.record.seq, 2)
        self.assertIn("relay_disagreement", res.warnings)
        res.admit(5000, threshold_minor=100_000)
        with self.assertRaises(StaleResolution):
            res.admit(500_000, threshold_minor=100_000)
        self.w.publish(self.seq2(), relays=[1])
        self.assertNotIn("relay_disagreement", self.w.resolve().warnings)

    def test_single_relay_is_below_quorum(self):
        self.w.relays[1].events.clear()
        with self.assertRaises(QuorumUnavailable):
            self.w.resolve()
        # A record from another key on the second relay does not count toward Alice's quorum.
        self.w.publish(self.w.record(self.w.attacker, seq=1), relays=[1])
        with self.assertRaises(QuorumUnavailable):
            self.w.resolve()
        self.assertIsNone(self.w.sender_resolver.pins.get(self.w.name))  # nothing pinned on a failed quorum
        with self.assertRaises(ValueError):
            AliasResolver(FixtureNip05({}), self.w.relays, PinStore(self.temp.name + "/x.sqlite3"), quorum=1)


if __name__ == "__main__":
    unittest.main()
