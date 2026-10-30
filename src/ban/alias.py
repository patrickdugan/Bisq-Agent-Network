"""Nostr-associated payout aliases (spec C.2-C.8). Offline: sources are injected.

A handle ``local@domain`` is bound to a key by the domain's NIP-05 document and
to its content by a BIP340-signed NIP-01 addressable event (``AliasRecordV1``).
The public record carries per-provider payout *commitments*, never a
destination. The resolver pins the highest seq per handle and fails closed on
rollback, equivocation, expiry, revocation, binding mismatch and thin quorum.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence
import unicodedata

from . import canon
from ._db import immediate, reader
from .bip340 import verify

# Provisional and unassigned: not in the nips@400d157 README kinds table.
ALIAS_KIND = 30875
RECOVERY_WINDOW_SECONDS = 72 * 3600
MAX_VALIDITY_SECONDS = 30 * 86400
MAX_FUTURE_SKEW_SECONDS = 600
MAX_SEQ = 2**32 - 1

_HEX32 = re.compile(r"[0-9a-f]{64}")
_HEX64 = re.compile(r"[0-9a-f]{128}")
_LOCAL = re.compile(r"[a-z0-9_.-]{1,64}")
_LDH = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_RAIL = re.compile(r"[a-z0-9_]{1,32}")
_ISO4217 = re.compile(r"[A-Z]{3}")
_RECORD_KEYS = frozenset({"v", "handle", "domain", "seq", "prev", "status", "valid_until",
                          "recovery_pubkey", "payout", "rotation"})
_TARGET_KEYS = frozenset({"rail", "currency", "commitment", "providers"})
_EVENT_KEYS = frozenset({"id", "pubkey", "created_at", "kind", "tags", "content", "sig"})
_DESTINATION_WORDS = ("destination", "phone", "msisdn", "mobile", "wallet", "account", "iban",
                      "payto", "address", "email", "card")
_PHONE_LIKE = re.compile(r"\+?[0-9][0-9 ()-]{6,18}")
_IBAN_LIKE = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}")


class AliasError(ValueError):
    pass


class InvalidHandle(AliasError):
    pass


class InvalidRecord(AliasError):
    pass


class DestinationLeak(InvalidRecord):
    pass


class BindingMismatch(AliasError):
    pass


class Rollback(AliasError):
    pass


class Equivocation(AliasError):
    def __init__(self, message: str, seq: int | None = None):
        super().__init__(message)
        self.seq = seq


class Expired(AliasError):
    pass


class Revoked(AliasError):
    pass


class RecoveryPending(AliasError):
    pass


class QuorumUnavailable(AliasError):
    pass


class StaleResolution(AliasError):
    pass


# ---------------------------------------------------------------- names (C.7)

def domain_alabel(domain: str) -> str:
    """IDNA A-label form. Stdlib has only IDNA2003, so labels are NFC-normalized,
    lowercased, restricted to letter/mark/digit categories and punycoded (RFC 3492);
    full IDNA2008 contextual and Bidi rules are not implemented."""
    if not isinstance(domain, str) or not 3 <= len(domain) <= 253:
        raise InvalidHandle("domain length")
    labels = domain.split(".")
    if len(labels) < 2:
        raise InvalidHandle("domain needs at least two labels")
    out = []
    for label in labels:
        if label.isascii():
            lowered = label.lower()
            if lowered.startswith("xn--"):
                try:
                    unicode_label = lowered[4:].encode("ascii").decode("punycode")
                except (UnicodeError, ValueError) as exc:
                    raise InvalidHandle("invalid A-label") from exc
                if unicode_label.isascii() or _ulabel_to_alabel(unicode_label) != lowered:
                    raise InvalidHandle("non-canonical A-label")
            elif not _LDH.fullmatch(lowered) or lowered[2:4] == "--":
                raise InvalidHandle("invalid domain label")
            out.append(lowered)
        else:
            out.append(_ulabel_to_alabel(label))
    result = ".".join(out)
    if len(result) > 253:
        raise InvalidHandle("domain too long")
    return result


def _ulabel_to_alabel(label: str) -> str:
    folded = unicodedata.normalize("NFC", label).lower()
    for ch in folded:
        if ch != "-" and unicodedata.category(ch) not in ("Ll", "Lo", "Lm", "Mn", "Mc", "Nd"):
            raise InvalidHandle("disallowed character in domain label")
    alabel = "xn--" + folded.encode("punycode").decode("ascii")
    if len(alabel) > 63:
        raise InvalidHandle("domain label too long")
    return alabel


def normalize_handle(text: str) -> tuple[str, str]:
    """``(local, A-label domain)``. Uppercase ASCII is lowercased; anything else
    outside ``a-z0-9-_.`` in the local part is rejected, never mapped."""
    if not isinstance(text, str) or len(text) > 320 or text.count("@") != 1:
        raise InvalidHandle("expected local@domain")
    local, domain = text.split("@")
    if not local.isascii():
        raise InvalidHandle("local part must be ASCII a-z0-9-_.")
    local = local.lower()
    if not _LOCAL.fullmatch(local):
        raise InvalidHandle("local part must be ASCII a-z0-9-_.")
    return local, domain_alabel(domain)


def display_domain(alabel: str) -> str:
    """U-label only when every letter of each label shares one script (ASCII counts
    as Latin); otherwise the punycode. A coarse stand-in for UTS #39 'highly
    restrictive', using Unicode names because stdlib has no script property."""
    labels = []
    for label in alabel.split("."):
        if not label.startswith("xn--"):
            labels.append(label)
            continue
        text = label[4:].encode("ascii").decode("punycode")
        scripts = {"LATIN" if ch.isascii() else unicodedata.name(ch, "UNKNOWN").split(" ")[0]
                   for ch in text if ch.isalpha()}
        if len(scripts) > 1:
            return alabel
        labels.append(text)
    return ".".join(labels)


_FOLDS = str.maketrans({"0": "o", "1": "l", "i": "l"})


def skeleton(local: str) -> str:
    """ASCII confusable skeleton for registration uniqueness (C.7)."""
    text = local.lower()
    for ch in "-_.":
        text = text.replace(ch, "")
    return text.replace("rn", "m").replace("vv", "w").translate(_FOLDS)


def skeleton_collision(candidate: str, registered: Sequence[str]) -> str | None:
    target = skeleton(candidate)
    for name in registered:
        if name != candidate and skeleton(name) == target:
            return name
    return None


def within_one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


# ----------------------------------------------------------- commitments (C.4)

def payout_commitment(rail: str, canonical_destination: str, salt32: bytes) -> str:
    if not isinstance(rail, str) or not _RAIL.fullmatch(rail):
        raise ValueError("invalid rail identifier")
    if (not isinstance(canonical_destination, str) or not 1 <= len(canonical_destination) <= 256
            or "\x00" in canonical_destination):
        raise ValueError("invalid canonical destination")
    if not isinstance(salt32, (bytes, bytearray)) or len(salt32) != 32:
        raise ValueError("salt must be 32 bytes")
    return sha256(b"CAG-PAYOUT-COMMIT-V1\x00" + rail.encode("ascii") + b"\x00"
                  + canonical_destination.encode("utf-8") + bytes(salt32)).hexdigest()


def rotation_digest(handle: str, new_pubkey: str, seq: int, prev_record: str, *, recovery: bool) -> bytes:
    """Statement the old key (or recovery key) signs. It cannot be the new event
    ID: that ID commits to the content that carries this very signature."""
    domain = b"CAG-ALIAS-RECOVER-V1\x00" if recovery else b"CAG-ALIAS-ROTATE-V1\x00"
    return sha256(domain + handle.encode("ascii") + b"\x00" + bytes.fromhex(new_pubkey)
                  + seq.to_bytes(4, "big") + bytes.fromhex(prev_record)).digest()


# ------------------------------------------------------------ records (C.4/C.5)

@dataclass(frozen=True)
class PayoutTarget:
    rail: str
    currency: str
    commitment: str
    providers: tuple[str, ...]


@dataclass(frozen=True)
class Rotation:
    prev_pubkey: str
    prev_record: str
    prev_sig: str | None
    recovery_sig: str | None


@dataclass(frozen=True)
class AliasRecordV1:
    handle: str
    domain: str
    pubkey: str
    seq: int
    prev: str | None
    status: str
    valid_until: int
    recovery_pubkey: str
    payout: tuple[PayoutTarget, ...]
    rotation: Rotation | None
    event_id: str
    created_at: int

    @property
    def name(self) -> str:
        return self.handle + "@" + self.domain

    def target_for(self, commitment: str) -> PayoutTarget | None:
        return next((t for t in self.payout if t.commitment == commitment), None)


def event_id(pubkey: str, created_at: int, kind: int, tags: list, content: str) -> str:
    """NIP-01 ID. Raw control characters are refused because Python's JSON
    escaping of them differs from NIP-01's verbatim rule."""
    for text in [content] + [item for tag in tags for item in tag]:
        if any(ord(ch) < 0x20 for ch in text):
            raise InvalidRecord("control characters are not allowed")
    serial = json.dumps([0, pubkey, created_at, kind, tags, content],
                        separators=(",", ":"), ensure_ascii=False)
    return sha256(serial.encode("utf-8")).hexdigest()


def lint_public_content(content: Any, tags: Sequence[Sequence[str]] = ()) -> None:
    """Refuse anything destination-like in public data, and any ``payto`` tag."""
    for tag in tags:
        if tag and str(tag[0]).casefold() == "payto":
            raise DestinationLeak("payto tags publish destinations (NIP-A3)")

    def walk(value: Any, key: str = "") -> None:
        if key and any(word in key.casefold() for word in _DESTINATION_WORDS):
            raise DestinationLeak("destination-like field in public content: " + key)
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            if _PHONE_LIKE.fullmatch(value) or _IBAN_LIKE.fullmatch(value) or "payto:" in value.casefold():
                raise DestinationLeak("destination-like value in public content")
    walk(content)


def _hex32(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HEX32.fullmatch(value):
        raise InvalidRecord(name + " must be lowercase 32-byte hex")
    return value


def _int(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise InvalidRecord(name + " out of range")
    return value


def parse_record(event: Mapping[str, Any], *, now: int) -> AliasRecordV1:
    """Verify a NIP-01 event (ID, BIP340 signature) and its AliasRecordV1 content.

    Expiry and revocation are judged by the resolver on the head record, so an
    older record can still be parsed to detect rollback.
    """
    if not isinstance(event, Mapping) or set(event) != _EVENT_KEYS:
        raise InvalidRecord("event must have exactly the NIP-01 fields")
    pubkey = _hex32(event["pubkey"], "pubkey")
    eid = _hex32(event["id"], "id")
    sig = event["sig"]
    if not isinstance(sig, str) or not _HEX64.fullmatch(sig):
        raise InvalidRecord("sig must be lowercase 64-byte hex")
    created_at = _int(event["created_at"], "created_at", 0, canon.MAX_SAFE_INTEGER)
    if event["kind"] != ALIAS_KIND or type(event["kind"]) is not int:
        raise InvalidRecord("not an alias record kind")
    tags = event["tags"]
    if (not isinstance(tags, list) or len(tags) > 8
            or any(not isinstance(t, list) or not t or any(not isinstance(x, str) for x in t) for t in tags)):
        raise InvalidRecord("tags must be arrays of strings")
    content_text = event["content"]
    if not isinstance(content_text, str):
        raise InvalidRecord("content must be a string")
    lint_public_content(None, tags)
    if event_id(pubkey, created_at, ALIAS_KIND, tags, content_text) != eid:
        raise InvalidRecord("event id mismatch")
    if not verify(bytes.fromhex(pubkey), bytes.fromhex(eid), bytes.fromhex(sig)):
        raise InvalidRecord("bad signature")
    if created_at > now + MAX_FUTURE_SKEW_SECONDS:
        raise InvalidRecord("created_at is in the future")
    names = [t[0] for t in tags]
    if sorted(names) != ["d", "expiration"] or any(len(t) != 2 for t in tags):
        raise InvalidRecord("exactly one d tag and one expiration tag are required")
    tag = {t[0]: t[1] for t in tags}
    try:
        content = canon.loads(content_text)
    except canon.CanonError as exc:
        raise InvalidRecord("content is not TL-Canon v1: " + str(exc)) from exc
    if not isinstance(content, dict):
        raise InvalidRecord("content must be an object")
    lint_public_content(content)
    if set(content) != _RECORD_KEYS:
        raise InvalidRecord("content must have exactly the AliasRecordV1 fields")
    if content["v"] != 1 or type(content["v"]) is not int:
        raise InvalidRecord("unsupported record version")
    try:
        local, domain = normalize_handle(str(content["handle"]) + "@" + str(content["domain"]))
    except InvalidHandle as exc:
        raise InvalidRecord("invalid handle: " + str(exc)) from exc
    if (local, domain) != (content["handle"], content["domain"]):
        raise InvalidRecord("handle and domain must be stored normalized (A-label)")
    if tag["d"] != local + "@" + domain:
        raise InvalidRecord("d tag does not name this handle")
    seq = _int(content["seq"], "seq", 1, MAX_SEQ)
    prev = content["prev"]
    if (seq == 1) != (prev is None):
        raise InvalidRecord("prev must be null exactly when seq is 1")
    if prev is not None:
        _hex32(prev, "prev")
    status = content["status"]
    if status not in ("active", "revoked"):
        raise InvalidRecord("unknown status")
    valid_until = _int(content["valid_until"], "valid_until", 1, canon.MAX_SAFE_INTEGER)
    if not created_at < valid_until <= created_at + MAX_VALIDITY_SECONDS:
        raise InvalidRecord("valid_until must be within 30 days after created_at")
    if tag["expiration"] != str(valid_until):
        raise InvalidRecord("expiration tag must equal valid_until")
    recovery = _hex32(content["recovery_pubkey"], "recovery_pubkey")
    if recovery == pubkey:
        raise InvalidRecord("recovery key must differ from the signing key")
    payout = content["payout"]
    if not isinstance(payout, list) or len(payout) > 8:
        raise InvalidRecord("payout must be a list of at most 8 targets")
    targets = []
    for item in payout:
        if not isinstance(item, dict) or set(item) != _TARGET_KEYS:
            raise InvalidRecord("payout target must have exactly rail, currency, commitment, providers")
        if not isinstance(item["rail"], str) or not _RAIL.fullmatch(item["rail"]):
            raise InvalidRecord("invalid rail")
        if not isinstance(item["currency"], str) or not _ISO4217.fullmatch(item["currency"]):
            raise InvalidRecord("invalid currency")
        providers = item["providers"]
        if (not isinstance(providers, list) or not 1 <= len(providers) <= 8
                or len(set(providers)) != len(providers)):
            raise InvalidRecord("providers must be 1..8 distinct keys")
        targets.append(PayoutTarget(item["rail"], item["currency"], _hex32(item["commitment"], "commitment"),
                                    tuple(_hex32(p, "provider") for p in providers)))
    rotation = None
    if content["rotation"] is not None:
        rot = content["rotation"]
        if not isinstance(rot, dict) or set(rot) not in ({"prev_pubkey", "prev_record", "prev_sig"},
                                                         {"prev_pubkey", "prev_record", "recovery_sig"}):
            raise InvalidRecord("rotation needs prev_pubkey, prev_record and one of prev_sig/recovery_sig")
        sig_value = rot.get("prev_sig", rot.get("recovery_sig"))
        if not isinstance(sig_value, str) or not _HEX64.fullmatch(sig_value):
            raise InvalidRecord("rotation signature must be 64-byte hex")
        rotation = Rotation(_hex32(rot["prev_pubkey"], "prev_pubkey"), _hex32(rot["prev_record"], "prev_record"),
                            rot.get("prev_sig"), rot.get("recovery_sig"))
        if rotation.prev_record != prev or rotation.prev_pubkey == pubkey:
            raise InvalidRecord("rotation must link the previous record and change the key")
        if rotation.prev_sig is not None and not verify(
                bytes.fromhex(rotation.prev_pubkey),
                rotation_digest(tag["d"], pubkey, seq, rotation.prev_record, recovery=False),
                bytes.fromhex(rotation.prev_sig)):
            raise InvalidRecord("rotation prev_sig does not verify")
    return AliasRecordV1(local, domain, pubkey, seq, prev, status, valid_until, recovery,
                         tuple(targets), rotation, eid, created_at)


# --------------------------------------------------------------- sources (C.1)

@dataclass(frozen=True)
class Nip05Response:
    url: str
    final_url: str
    status: int
    body: Any


class Nip05Source(Protocol):
    def fetch(self, domain: str, local: str) -> Nip05Response: ...


class RecordSource(Protocol):
    url: str

    def query(self, kind: int, d_tag: str) -> list[dict]: ...


def nip05_url(domain: str, local: str) -> str:
    return "https://" + domain + "/.well-known/nostr.json?name=" + local


class FixtureNip05:
    """Serves ``/.well-known/nostr.json`` documents from a dict; no network.
    A document may set ``_redirect_to`` to model an HTTP redirect."""

    def __init__(self, documents: Mapping[str, Any]):
        self.documents = dict(documents)

    def fetch(self, domain: str, local: str) -> Nip05Response:
        url = nip05_url(domain, local)
        doc = self.documents.get(domain)
        if doc is None:
            return Nip05Response(url, url, 404, None)
        if isinstance(doc, dict) and "_redirect_to" in doc:
            return Nip05Response(url, str(doc["_redirect_to"]), 200, {k: v for k, v in doc.items() if k != "_redirect_to"})
        return Nip05Response(url, url, 200, doc)


class FixtureRelay:
    def __init__(self, url: str, events: Sequence[dict] = ()):
        self.url = url
        self.events = list(events)

    def query(self, kind: int, d_tag: str) -> list[dict]:
        return [e for e in self.events if isinstance(e, dict) and e.get("kind") == kind
                and isinstance(e.get("tags"), list) and ["d", d_tag] in e["tags"]]


# ---------------------------------------------------------------- pins (C.6)

@dataclass(frozen=True)
class Pin:
    handle: str
    pubkey: str
    seq: int
    record_id: str
    recovery_pubkey: str
    first_seen: int
    frozen_seq: int | None


_PIN_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS alias_pins(
      handle TEXT PRIMARY KEY, pubkey TEXT NOT NULL, seq INTEGER NOT NULL,
      record_id TEXT NOT NULL, recovery_pubkey TEXT NOT NULL, first_seen INTEGER NOT NULL,
      frozen_seq INTEGER, updated_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS alias_seen(
      record_id TEXT PRIMARY KEY, handle TEXT NOT NULL, first_seen INTEGER NOT NULL)""",
)


class PinStore:
    """Sender-side monotonic pin per handle: never accepts a lower seq."""

    def __init__(self, database: str | Path):
        self.database = Path(database)
        with immediate(self.database) as db:
            for stmt in _PIN_SCHEMA:
                db.execute(stmt)

    def get(self, handle: str) -> Pin | None:
        with reader(self.database) as db:
            row = db.execute("SELECT handle,pubkey,seq,record_id,recovery_pubkey,first_seen,frozen_seq "
                             "FROM alias_pins WHERE handle=?", (handle,)).fetchone()
        return Pin(*row) if row else None

    def first_seen(self, record_id: str, handle: str, now: int) -> int:
        with immediate(self.database) as db:
            db.execute("INSERT OR IGNORE INTO alias_seen(record_id,handle,first_seen) VALUES(?,?,?)",
                       (record_id, handle, now))
            return db.execute("SELECT first_seen FROM alias_seen WHERE record_id=?", (record_id,)).fetchone()[0]

    def advance(self, record: AliasRecordV1, now: int, *, new_identity: bool = False) -> Pin:
        with immediate(self.database) as db:
            row = db.execute("SELECT seq,record_id,first_seen,frozen_seq FROM alias_pins WHERE handle=?",
                             (record.name,)).fetchone()
            if row is None or new_identity:
                db.execute("INSERT OR REPLACE INTO alias_pins VALUES(?,?,?,?,?,?,NULL,?)",
                           (record.name, record.pubkey, record.seq, record.event_id,
                            record.recovery_pubkey, now, now))
            else:
                seq, record_id, first, frozen = row
                if record.seq < seq:
                    raise Rollback("record seq %d is below pinned seq %d" % (record.seq, seq))
                if record.seq == seq and record.event_id != record_id:
                    raise Equivocation("two records share seq %d" % seq, seq)
                if frozen is not None and record.seq <= frozen:  # checked inside the write transaction
                    raise Equivocation("alias frozen at seq %d until a higher seq resolves it" % frozen, frozen)
                frozen = None
                db.execute("UPDATE alias_pins SET pubkey=?,seq=?,record_id=?,recovery_pubkey=?,frozen_seq=?,"
                           "updated_at=? WHERE handle=?",
                           (record.pubkey, record.seq, record.event_id, record.recovery_pubkey, frozen,
                            now, record.name))
        return self.get(record.name)

    def freeze(self, handle: str, seq: int) -> None:
        with immediate(self.database) as db:
            db.execute("UPDATE alias_pins SET frozen_seq=MAX(COALESCE(frozen_seq,0),?) WHERE handle=?",
                       (seq, handle))


# ------------------------------------------------------------ resolver (C.6)

@dataclass(frozen=True)
class Resolution:
    record: AliasRecordV1
    identity: str  # first_contact | pinned | rotated | recovered | recovery_vetoed | new_identity
    bound_pubkey: str
    relay_heads: tuple[tuple[str, int], ...]
    warnings: tuple[str, ...] = field(default=())

    @property
    def stale_risk(self) -> bool:
        return "relay_disagreement" in self.warnings

    def admit(self, amount_minor: int, *, threshold_minor: int) -> None:
        """Refuse an intent above ``threshold_minor`` while relays disagree."""
        if self.stale_risk and amount_minor > threshold_minor:
            raise StaleResolution("relays disagree on the latest record; amount above threshold")


def _head(records: Sequence[AliasRecordV1]) -> AliasRecordV1:
    top = max(r.seq for r in records)
    ids = {r.event_id for r in records if r.seq == top}
    if len(ids) > 1:
        raise Equivocation("two records share seq %d" % top, top)
    return next(r for r in records if r.seq == top)


class AliasResolver:
    def __init__(self, nip05: Nip05Source, relays: Sequence[RecordSource], pins: PinStore,
                 *, quorum: int = 2):
        if quorum < 2:
            raise ValueError("quorum below 2 relays is not allowed")
        self.nip05 = nip05
        self.relays = list(relays)
        self.pins = pins
        self.quorum = quorum

    def _binding(self, local: str, domain: str) -> tuple[str, list[str]]:
        expected = nip05_url(domain, local)
        response = self.nip05.fetch(domain, local)
        if response.url != expected or response.final_url != expected:
            raise BindingMismatch("NIP-05 redirects are refused")
        if response.status != 200 or not isinstance(response.body, dict):
            raise BindingMismatch("NIP-05 document unavailable")
        names = response.body.get("names")
        key = names.get(local) if isinstance(names, dict) else None
        if not isinstance(key, str) or not _HEX32.fullmatch(key):
            raise BindingMismatch("handle not bound by the domain")
        hints = response.body.get("relays", {})
        hinted = hints.get(key, []) if isinstance(hints, dict) else []
        return key, [h for h in hinted if isinstance(h, str)]

    def resolve(self, name: str, *, now: int) -> Resolution:
        local, domain = normalize_handle(name)
        handle = local + "@" + domain
        bound, hinted = self._binding(local, domain)
        per_relay: list[tuple[str, list[AliasRecordV1]]] = []
        for relay in self.relays:
            records = []
            for event in relay.query(ALIAS_KIND, handle):
                try:
                    record = parse_record(event, now=now)
                except AliasError:
                    continue
                if record.name == handle:
                    records.append(record)
            per_relay.append((relay.url, records))
        if sum(1 for _, recs in per_relay if recs) < self.quorum:
            raise QuorumUnavailable("fewer than %d relays returned a valid record" % self.quorum)
        records = list({r.event_id: r for _, recs in per_relay for r in recs}.values())
        warnings: list[str] = []
        if hinted and not set(hinted) & {url for url, _ in per_relay}:
            warnings.append("relay_hint_unavailable")
        pin = self.pins.get(handle)
        try:
            head, identity = self._select(handle, bound, pin, records, now, warnings)
            # Quorum counts only relays serving the selected identity, not any record under this d tag.
            relay_heads = tuple(
                (url, max((r.seq for r in recs if r.pubkey == head.pubkey), default=0)) for url, recs in per_relay)
            if sum(1 for _, seq in relay_heads if seq) < self.quorum:
                raise QuorumUnavailable("fewer than %d relays serve the selected identity" % self.quorum)
            self.pins.advance(head, now, new_identity=(identity == "new_identity"))
        except Equivocation as exc:
            if pin is not None and exc.seq is not None:
                self.pins.freeze(handle, exc.seq)
            raise
        if len({seq for _, seq in relay_heads}) > 1:
            warnings.append("relay_disagreement")
        if head.status == "revoked":
            raise Revoked("alias revoked at seq %d" % head.seq)
        if head.valid_until <= now:
            raise Expired("alias record expired")
        return Resolution(head, identity, bound, relay_heads, tuple(warnings))

    def _select(self, handle: str, bound: str, pin: Pin | None, records: list[AliasRecordV1],
                now: int, warnings: list[str]) -> tuple[AliasRecordV1, str]:
        bound_records = [r for r in records if r.pubkey == bound]
        if pin is None:
            if not bound_records:
                raise BindingMismatch("no valid record is signed by the NIP-05 key")
            return _head(bound_records), "first_contact"
        own = [r for r in records if r.pubkey == pin.pubkey]
        if bound == pin.pubkey:
            if not own:
                raise BindingMismatch("no valid record is signed by the pinned key")
            head = _head(own)
            self._check_monotonic(pin, head)
            return head, "pinned"
        if not bound_records:
            raise BindingMismatch("NIP-05 key has no valid record")
        rotations = sorted((r for r in bound_records if r.rotation and r.rotation.prev_pubkey == pin.pubkey),
                           key=lambda r: r.seq)
        rot = rotations[0] if rotations else None
        # The rotation must link the pinned record or another record of the pinned key.
        base: tuple[int, str] | None = None  # (seq, recovery key) of the linked record
        if rot is not None:
            if rot.rotation.prev_record == pin.record_id and rot.seq == pin.seq + 1:
                base = (pin.seq, pin.recovery_pubkey)
            else:
                base = next(((r.seq, r.recovery_pubkey) for r in own if r.event_id == rot.rotation.prev_record
                             and r.seq == rot.seq - 1), None)
        if base is not None and rot.rotation.prev_sig is not None and base[0] >= pin.seq:
            return _head([r for r in bound_records if r.seq >= rot.seq]), "rotated"
        recovery_key = base[1] if base is not None else None
        if recovery_key is not None and rot.rotation.recovery_sig is not None and verify(
                bytes.fromhex(recovery_key),
                rotation_digest(handle, rot.pubkey, rot.seq, rot.rotation.prev_record, recovery=True),
                bytes.fromhex(rot.rotation.recovery_sig)):
            started = self.pins.first_seen(rot.event_id, handle, now)
            # The previous key vetoes by publishing at or above the recovery's seq
            # within the window (seen together counts as within).
            vetoes = [r for r in own if r.seq >= rot.seq
                      and self.pins.first_seen(r.event_id, handle, now) < started + RECOVERY_WINDOW_SECONDS]
            if vetoes:
                warnings.append("recovery_vetoed_by_previous_key")
                return _head(own), "recovery_vetoed"
            # A recovery may only extend the pin or later: a link below the pin would revive a
            # recovery key that a newer record already replaced.
            if base[0] >= pin.seq:
                if now < started + RECOVERY_WINDOW_SECONDS:
                    raise RecoveryPending("recovery pending until %d" % (started + RECOVERY_WINDOW_SECONDS))
                return _head([r for r in bound_records if r.seq >= rot.seq]), "recovered"
        # Domain-only rebind: no old-key or recovery signature links the new key.
        warnings.append("new_recipient")
        return _head(bound_records), "new_identity"

    @staticmethod
    def _check_monotonic(pin: Pin, head: AliasRecordV1) -> None:
        if head.seq < pin.seq:
            raise Rollback("relays returned seq %d below pinned seq %d" % (head.seq, pin.seq))
