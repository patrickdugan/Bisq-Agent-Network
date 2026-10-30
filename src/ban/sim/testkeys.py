"""SIMULATION ONLY: deterministic BIP340 signing keys derived from public labels.

Every key here is derived from a human-readable label, so anyone can recompute
it. There is deliberately no API that accepts an externally supplied secret
key: these keys can only ever sign synthetic fixtures and simulated actors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256

from ..bip340 import G, N, point_mul, tagged_hash

_LABEL_DOMAIN = b"BAN-SIM-TEST-KEY-V1\x00"


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _sign(secret: int, msg: bytes, aux32: bytes) -> bytes:
    """BIP340 ``Sign``; module-private so callers cannot pass real keys."""
    if not 1 <= secret < N or len(aux32) != 32:
        raise ValueError("invalid signing input")
    point = point_mul(G, secret)
    d = secret if point[1] % 2 == 0 else N - secret
    pub = point[0].to_bytes(32, "big")
    t = _xor(d.to_bytes(32, "big"), tagged_hash("BIP0340/aux", aux32))
    k0 = int.from_bytes(tagged_hash("BIP0340/nonce", t + pub + msg), "big") % N
    if k0 == 0:
        raise ValueError("nonce derivation failed")
    rpoint = point_mul(G, k0)
    k = k0 if rpoint[1] % 2 == 0 else N - k0
    r = rpoint[0].to_bytes(32, "big")
    e = int.from_bytes(tagged_hash("BIP0340/challenge", r + pub + msg), "big") % N
    return r + ((k + e * d) % N).to_bytes(32, "big")


@dataclass(frozen=True)
class TestKey:
    """A synthetic signer. ``label`` is public, so the key is public too."""
    label: str
    _secret: int = field(init=False, repr=False, compare=False)
    pubkey: str = field(init=False, compare=False)

    __test__ = False  # not a unittest/pytest collection target

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not 1 <= len(self.label) <= 100:
            raise ValueError("test key label must be 1..100 characters")
        secret = int.from_bytes(sha256(_LABEL_DOMAIN + self.label.encode("utf-8")).digest(), "big") % N or 1
        object.__setattr__(self, "_secret", secret)
        object.__setattr__(self, "pubkey", point_mul(G, secret)[0].to_bytes(32, "big").hex())

    def sign(self, msg: bytes) -> str:
        # Deterministic aux so fixtures and demo output are reproducible.
        aux = sha256(b"BAN-SIM-AUX-V1\x00" + msg).digest()
        return _sign(self._secret, msg, aux).hex()

    def sign_hex(self, msg32_hex: str) -> str:
        return self.sign(bytes.fromhex(msg32_hex))
