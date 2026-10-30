"""BIP340 Schnorr verification over secp256k1, standard library only.

Verify only. Signing lives in ``ban.sim.testkeys`` for the simulation and is
never imported by non-simulation modules. Not constant time; public inputs only.
"""
from __future__ import annotations

from hashlib import sha256

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)

Point = tuple[int, int] | None


def tagged_hash(tag: str, data: bytes) -> bytes:
    tag_digest = sha256(tag.encode("ascii")).digest()
    return sha256(tag_digest + tag_digest + data).digest()


def point_add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and a[1] != b[1]:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


_Jac = tuple[int, int, int]  # (X, Y, Z) with x = X/Z^2, y = Y/Z^3; Z == 0 is infinity


def _jac_double(p: _Jac) -> _Jac:
    x, y, z = p
    if z == 0 or y == 0:
        return 0, 1, 0
    ysq = y * y % P
    s = 4 * x * ysq % P
    m = 3 * x * x % P
    nx = (m * m - 2 * s) % P
    return nx, (m * (s - nx) - 8 * ysq * ysq) % P, 2 * y * z % P


def _jac_add(p: _Jac, q: _Jac) -> _Jac:
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    z1z1, z2z2 = p[2] * p[2] % P, q[2] * q[2] % P
    u1, u2 = p[0] * z2z2 % P, q[0] * z1z1 % P
    s1, s2 = p[1] * q[2] * z2z2 % P, q[1] * p[2] * z1z1 % P
    if u1 == u2:
        return _jac_double(p) if s1 == s2 else (0, 1, 0)
    h, r = (u2 - u1) % P, (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    nx = (r * r - hhh - 2 * v) % P
    return nx, (r * (v - nx) - s1 * hhh) % P, h * p[2] * q[2] % P


def point_mul(point: Point, scalar: int) -> Point:
    if point is None:
        return None
    result: _Jac = (0, 1, 0)
    addend: _Jac = (point[0], point[1], 1)
    while scalar:
        if scalar & 1:
            result = _jac_add(result, addend)
        addend = _jac_double(addend)
        scalar >>= 1
    if result[2] == 0:
        return None
    zinv = pow(result[2], -1, P)
    zinv2 = zinv * zinv % P
    return result[0] * zinv2 % P, result[1] * zinv2 * zinv % P


def lift_x(x: int) -> Point:
    if not 0 <= x < P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        return None
    return x, y if y % 2 == 0 else P - y


def verify(pubkey32: bytes, msg: bytes, sig64: bytes) -> bool:
    """BIP340 ``Verify``; any malformed input is False, never an exception."""
    if not all(isinstance(v, (bytes, bytearray)) for v in (pubkey32, msg, sig64)):
        return False
    if len(pubkey32) != 32 or len(sig64) != 64:
        return False
    point = lift_x(int.from_bytes(pubkey32, "big"))
    if point is None:
        return False
    r = int.from_bytes(sig64[:32], "big")
    s = int.from_bytes(sig64[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(tagged_hash("BIP0340/challenge", sig64[:32] + bytes(pubkey32) + bytes(msg)), "big") % N
    rpoint = point_add(point_mul(G, s), point_mul(point, N - e))
    return rpoint is not None and rpoint[1] % 2 == 0 and rpoint[0] == r


def verify_hex(pubkey_hex: str, msg32_hex: str, sig_hex: str) -> bool:
    """Hex wrapper for lowercase canonical hex; non-canonical hex is False."""
    # Strict hex: bytes.fromhex would silently skip whitespace.
    if any(not isinstance(v, str) or len(v) % 2 or v.strip("0123456789abcdef") for v in (pubkey_hex, msg32_hex, sig_hex)):
        return False
    return verify(bytes.fromhex(pubkey_hex), bytes.fromhex(msg32_hex), bytes.fromhex(sig_hex))
