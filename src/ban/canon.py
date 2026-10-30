"""TL-Canon v1 canonical JSON (tl-collator spec/schemas/tl-canon-v1.md @a184247).

Sorted keys, no whitespace, arrays in order, safe integers only, no floats.
Keys are restricted to ASCII so Python's code-point sort equals the spec's
JavaScript ``Object.keys().sort()`` (UTF-16) order.
"""
from __future__ import annotations

import json
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


class CanonError(ValueError):
    pass


def _check(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise CanonError("nesting too deep")
    if value is None or isinstance(value, (bool, str)):
        return
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonError("integer outside the safe range")
        return
    if isinstance(value, float):
        raise CanonError("floats are not canonical; use strings for money")
    if isinstance(value, (list, tuple)):
        for item in value:
            _check(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                raise CanonError("object keys must be ASCII strings")
            _check(item, depth + 1)
        return
    raise CanonError("unsupported JSON type")


def dumps(value: Any) -> str:
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, item in pairs:
        if key in out:
            raise CanonError("duplicate object key")
        out[key] = item
    return out


def _no_float(text: str) -> Any:
    raise CanonError("floats are not canonical")


def loads(text: str) -> Any:
    """Parse and require that ``text`` is already in canonical form."""
    if not isinstance(text, str) or len(text) > 65536:
        raise CanonError("canonical text must be a string of at most 64 KiB")
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates,
                           parse_float=_no_float, parse_constant=_no_float)
    except CanonError:
        raise
    except ValueError as exc:
        raise CanonError("invalid JSON") from exc
    if dumps(value) != text:
        raise CanonError("content is not TL-Canon v1")
    return value
