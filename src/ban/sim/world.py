"""SIMULATION ONLY: builders for signed synthetic objects (alias events,
enrollments, intents, inventory attestations). Used by tests and the demo."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence

from .. import canon
from ..alias import (ALIAS_KIND, AliasResolver, FixtureNip05, FixtureRelay, PinStore, Resolution, event_id,
                     payout_commitment, rotation_digest)
from ..evidence import FixtureChain, FixtureEvidence, FundingClaim, synthetic_block_hash
from ..intent import (DestinationEnrollmentV1, EvidencePolicyV1, InventoryAttestationV1, PayoutIntentV1,
                      ProviderQuoteV1, SignedInventory)
from .provider import ReferenceProvider
from .rails import LocalPayoutRail, MockJawwalRail
from .testkeys import TestKey

DEFAULTS: dict[str, Any] = {
    "network": "regtest",
    "now": 1_793_000_000,
    "tip_height": 200,
    "domain": "pay.test",
    "handle": "alice",
    "relays": ["wss://relay-a.test", "wss://relay-b.test"],
    "keys": {"alice": "sim/alice", "alice_recovery": "sim/alice-recovery", "sender": "sim/sender",
             "provider": "sim/provider-p1", "other_provider": "sim/provider-p2", "attacker": "sim/attacker"},
    "alias": {"valid_seconds": 30 * 86400, "rail": "jawwal_pay", "currency": "ILS", "exponent": 2,
              "destination": "SYNTHETIC-JAWWAL-WALLET-0001"},
    "provider": {"float_minor": 500_000, "fee_bps": 100, "rate_minor_per_btc": 38_000_000, "min_conf": 2,
                 "max_tip_lag": 2, "quote_ttl_blocks": 6, "quote_ttl_seconds": 3600},
    "intent": {"amount_mode": "receive_exact", "amount": 5000, "max_total_fee_sats": 5000, "max_fee_bps": 150,
               "min_net_payout_minor": 5000, "refund_script": "0014" + "11" * 20, "expiry_seconds": 7200},
}


def salt_for(label: str) -> bytes:
    return sha256(b"BAN-SIM-SALT-V1\x00" + label.encode("utf-8")).digest()


def alias_event(key: TestKey, *, handle: str, domain: str, seq: int, created_at: int, valid_until: int,
                recovery_pubkey: str, payout: Sequence[dict[str, Any]], prev: str | None = None,
                status: str = "active", rotation: dict | None = None,
                extra_tags: Sequence[list[str]] = (), content_override: dict | None = None) -> dict:
    content = {"v": 1, "handle": handle, "domain": domain, "seq": seq, "prev": prev, "status": status,
               "valid_until": valid_until, "recovery_pubkey": recovery_pubkey, "payout": list(payout),
               "rotation": rotation}
    if content_override is not None:
        content = content_override
    text = canon.dumps(content)
    tags = [["d", handle + "@" + domain], ["expiration", str(valid_until)]] + [list(t) for t in extra_tags]
    eid = event_id(key.pubkey, created_at, ALIAS_KIND, tags, text)
    return {"id": eid, "pubkey": key.pubkey, "created_at": created_at, "kind": ALIAS_KIND, "tags": tags,
            "content": text, "sig": key.sign_hex(eid)}


def rotation(old_key: TestKey, new_key: TestKey, *, name: str, seq: int, prev_record: str) -> dict:
    return {"prev_pubkey": old_key.pubkey, "prev_record": prev_record,
            "prev_sig": old_key.sign(rotation_digest(name, new_key.pubkey, seq, prev_record, recovery=False))}


def recovery_rotation(recovery_key: TestKey, old_pubkey: str, new_key: TestKey, *, name: str, seq: int,
                      prev_record: str) -> dict:
    return {"prev_pubkey": old_pubkey, "prev_record": prev_record,
            "recovery_sig": recovery_key.sign(rotation_digest(name, new_key.pubkey, seq, prev_record, recovery=True))}


def payout_target(rail: str, currency: str, destination: str, salt: bytes, providers: Sequence[str]) -> dict:
    return {"rail": rail, "currency": currency, "commitment": payout_commitment(rail, destination, salt),
            "providers": list(providers)}


def enrollment(key: TestKey, *, record_id: str, seq: int, rail: str, destination: str, salt: bytes,
               provider_pubkey: str, created_at: int) -> tuple[DestinationEnrollmentV1, str]:
    item = DestinationEnrollmentV1(key.pubkey, record_id, seq, rail, destination, salt.hex(),
                                   payout_commitment(rail, destination, salt), provider_pubkey, created_at)
    return item, key.sign_hex(item.digest())


def signed_intent(sender: TestKey, **fields: Any) -> tuple[PayoutIntentV1, str]:
    intent = PayoutIntentV1(sender_pubkey=sender.pubkey, **fields)
    return intent, sender.sign_hex(intent.intent_id())


def signed_inventory(key: TestKey, **fields: Any) -> SignedInventory:
    attestation = InventoryAttestationV1(provider_pubkey=key.pubkey, **fields)
    return SignedInventory(attestation, key.sign_hex(attestation.digest()))


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


class CashoutWorld:
    """A complete synthetic world: domain, two relays, Alice's record, sender and
    provider resolvers with separate pin stores, a fixture chain and provider P1."""

    chain_label = "fixture chain"

    def __init__(self, workdir: str | Path, config: dict | None = None):
        cfg = _merge(DEFAULTS, config or {})
        self.cfg = cfg
        self.workdir = Path(workdir)
        self.network = cfg["network"]
        self.now = cfg["now"]
        self.domain, self.handle = cfg["domain"], cfg["handle"]
        self.name = self.handle + "@" + self.domain
        k = cfg["keys"]
        self.alice, self.recovery = TestKey(k["alice"]), TestKey(k["alice_recovery"])
        self.sender, self.p1 = TestKey(k["sender"]), TestKey(k["provider"])
        self.p2, self.attacker = TestKey(k["other_provider"]), TestKey(k["attacker"])
        a = cfg["alias"]
        self.rail, self.currency, self.exponent = a["rail"], a["currency"], a["exponent"]
        self.destination = a["destination"]
        self.salt = salt_for(k["alice"] + "/" + k["provider"])
        self.target = payout_target(self.rail, self.currency, self.destination, self.salt, [self.p1.pubkey])
        self.relays = [FixtureRelay(url) for url in cfg["relays"]]
        self.nip05 = FixtureNip05(cfg.get("nip05") or {
            self.domain: {"names": {self.handle: self.alice.pubkey}, "relays": {self.alice.pubkey: [cfg["relays"][0]]}}})
        self.record1 = self.record(self.alice, seq=1)
        self.publish(self.record1)
        self.sender_resolver = AliasResolver(self.nip05, self.relays, PinStore(self.workdir / "sender_pins.sqlite3"))
        self.provider_resolver = AliasResolver(self.nip05, self.relays, PinStore(self.workdir / "p1_pins.sqlite3"))
        self.chain = self.make_chain()
        self.policy = EvidencePolicyV1("test", self.network, {
            "own_core_v1": {"approval": {"state": "approved", "by": "SYNTHETIC_FIXTURE operator", "at": self.now},
                            "chain": self.network, "min_conf": 1}})
        self.rail_mock = MockJawwalRail()
        self.provider = self.new_provider()
        self._seq = 0

    # -- alias side
    def record(self, key: TestKey, *, seq: int, prev: str | None = None, status: str = "active",
               payout: Sequence[dict] | None = None, rotation_obj: dict | None = None,
               created_at: int | None = None, valid_seconds: int | None = None,
               recovery_pubkey: str | None = None, **extra: Any) -> dict:
        created = self.now - 60 if created_at is None else created_at
        valid = self.cfg["alias"]["valid_seconds"] if valid_seconds is None else valid_seconds
        return alias_event(key, handle=self.handle, domain=self.domain, seq=seq, created_at=created,
                           valid_until=created + valid,
                           recovery_pubkey=recovery_pubkey or self.recovery.pubkey,
                           payout=[self.target] if payout is None else payout, prev=prev, status=status,
                           rotation=rotation_obj, **extra)

    def publish(self, event: dict, relays: Sequence[int] | None = None) -> None:
        for i, relay in enumerate(self.relays):
            if relays is None or i in relays:
                relay.events.append(event)

    def withdraw_all(self) -> None:
        for relay in self.relays:
            relay.events.clear()

    def resolve(self) -> Resolution:
        return self.sender_resolver.resolve(self.name, now=self.now)

    # -- provider side
    def make_chain(self) -> Any:
        return FixtureChain(self.network, self.cfg["tip_height"])

    def funding_evidence(self) -> Any:
        """The provider's own-node view of this world's chain."""
        return FixtureEvidence(self.chain, self.policy)

    def new_provider(self, *, rail: LocalPayoutRail | None = None, crash_points: frozenset[str] = frozenset(),
                     database: str | Path | None = None, evidence: Any = None) -> ReferenceProvider:
        p = self.cfg["provider"]
        if rail is not None:
            self.rail_mock = rail
        return ReferenceProvider(
            self.p1, database or self.workdir / "p1.sqlite3", self.rail_mock,
            evidence or self.funding_evidence(), self.policy,
            rail_name=self.rail, currency=self.currency, exponent=self.exponent, float_minor=p["float_minor"],
            rate_minor_per_btc=p["rate_minor_per_btc"], fee_bps=p["fee_bps"],
            alias_lookup=lambda handle: self.provider_resolver.resolve(handle, now=self.now).record,
            quote_ttl_blocks=p["quote_ttl_blocks"], quote_ttl_seconds=p["quote_ttl_seconds"],
            min_conf=p["min_conf"], max_tip_lag=p["max_tip_lag"], crash_points=crash_points)

    def enroll(self, provider: ReferenceProvider | None = None):
        record = self.provider_resolver.resolve(self.name, now=self.now).record
        item, sig = enrollment(self.alice, record_id=record.event_id, seq=record.seq, rail=self.rail,
                               destination=self.destination, salt=self.salt, provider_pubkey=self.p1.pubkey,
                               created_at=self.now)
        return (provider or self.provider).enroll(item, sig, record)

    def intent(self, resolution: Resolution | None = None, **overrides: Any) -> tuple[PayoutIntentV1, str]:
        resolution = resolution or self.resolve()
        i = self.cfg["intent"]
        self._seq += 1
        fields = dict(
            network=self.network, alias_pubkey=resolution.record.pubkey, alias_record_id=resolution.record.event_id,
            alias_seq=resolution.record.seq, recipient_commitment=self.target["commitment"], rail=self.rail,
            currency=self.currency, exponent=self.exponent, amount_mode=i["amount_mode"], amount=i["amount"],
            max_total_fee_sats=i["max_total_fee_sats"], max_fee_bps=i["max_fee_bps"],
            min_net_payout_minor=i["min_net_payout_minor"],
            refund_script_sha256=sha256(bytes.fromhex(i["refund_script"])).hexdigest(),
            sender_nonce=sha256(b"BAN-SIM-NONCE\x00" + self._seq.to_bytes(8, "big")).hexdigest(),
            expiry_unix=self.now + i["expiry_seconds"], funding_source="held_btc")
        fields.update(overrides)
        return signed_intent(self.sender, **fields)

    def quote(self, provider: ReferenceProvider | None = None, **intent_overrides: Any):
        intent, intent_sig = self.intent(**intent_overrides)
        quote, sig = (provider or self.provider).quote(intent, intent_sig, tip_height=self.chain.tip_height,
                                                       now=self.now)
        return intent, quote, sig

    # -- chain side
    def fund(self, quote: ProviderQuoteV1, *, value: int | None = None, script: str | None = None,
             height: int | None = None, branch: str = "main", vout: int = 0, tag: str = "") -> FundingClaim:
        outputs = [(5_000, "0014" + "22" * 20)] * vout + [(quote.sats_required if value is None else value,
                                                         quote.deposit_script if script is None else script)]
        at = self.chain.tip_height + 1 if height is None else height
        txid = self.chain.add_tx(outputs, at, branch=branch, tag=tag)
        if at > self.chain.tip_height:
            self.chain.tip_height = at
        return FundingClaim(txid, vout)

    def advance(self, *, blocks: int = 0, seconds: int = 0) -> None:
        self.chain.mine(blocks)
        self.now += seconds

    def orphan_funding(self, claim: FundingClaim) -> None:
        """A reorg takes the funding's block off the active chain (here: onto a fork)."""
        tx = self.chain.txs[claim.txid]
        tx.block_hash = synthetic_block_hash(tx.block_height, "orphaned")

    def reconfirm_funding(self, claim: FundingClaim) -> None:
        """The same transaction confirms again in a new block, ``min_conf`` deep."""
        tx = self.chain.txs[claim.txid]
        tx.block_height = self.chain.tip_height + 1
        tx.block_hash = synthetic_block_hash(tx.block_height)
        self.chain.tip_height += self.cfg["provider"]["min_conf"]


def fixture_inventory(entries: Sequence[dict], keys: dict[str, str], *, now: int, rail: str, currency: str,
                      exponent: int, ttl: int = 600) -> dict[str, SignedInventory]:
    """Provider-signed inventory attestations described by the demo fixture."""
    out = {}
    for entry in entries:
        observed = now - entry["age_seconds"]
        out[entry["offer_id"]] = signed_inventory(
            TestKey(keys[entry["key"]]), offer_id=entry["offer_id"], rail=rail, currency=currency,
            exponent=exponent, available_minor=entry["available_minor"],
            max_per_payout_minor=entry["max_per_payout_minor"], observed_at=observed, valid_until=observed + ttl)
    return out
