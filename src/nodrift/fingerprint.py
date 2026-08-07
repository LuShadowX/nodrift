"""Structural fingerprinting of Python values.

The comparison engine cannot use `==`: outcomes are produced in separate
processes, many objects do not define __eq__, and default reprs embed memory
addresses that differ run to run. Instead every outcome is reduced to a
JSON-serialisable structural description that is stable across processes.
"""

from __future__ import annotations

import re
import math

_ADDR = re.compile(r"0x[0-9a-fA-F]{4,}")
MAX_DEPTH = 8
MAX_ITEMS = 256

_PRIMITIVES = (type(None), bool, int, str, bytes)


def _scrub(text: str) -> str:
    return _ADDR.sub("0xADDR", text)


def _type_name(obj: object) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def fingerprint(obj: object, depth: int = 0, seen: frozenset[int] | None = None):
    """Reduce `obj` to a stable, comparable, JSON-serialisable structure."""
    seen = seen or frozenset()

    if depth > MAX_DEPTH:
        return ["truncated"]

    if isinstance(obj, _PRIMITIVES):
        if isinstance(obj, bytes):
            return ["bytes", obj.hex()]
        if isinstance(obj, str):
            # Strings routinely embed repr() of other objects, which carries a
            # per-process memory address. Two runs of identical code would
            # otherwise disagree on any message built from a default repr.
            return ["prim", _type_name(obj), _scrub(obj)]
        return ["prim", _type_name(obj), obj]

    if isinstance(obj, float):
        # repr round-trips exactly; nan/inf need explicit handling for JSON.
        if math.isnan(obj):
            return ["float", "nan"]
        if math.isinf(obj):
            return ["float", "inf" if obj > 0 else "-inf"]
        return ["float", repr(obj)]

    # Cycle guard. Identity is only meaningful within a single process, which
    # is exactly the scope we need it for.
    if id(obj) in seen:
        return ["cycle"]
    seen = seen | {id(obj)}

    if isinstance(obj, (list, tuple)):
        kind = "list" if isinstance(obj, list) else "tuple"
        items = [fingerprint(x, depth + 1, seen) for x in obj[:MAX_ITEMS]]
        if len(obj) > MAX_ITEMS:
            items.append(["truncated", len(obj)])
        return [kind, items]

    if isinstance(obj, (set, frozenset)):
        kind = "set" if isinstance(obj, set) else "frozenset"
        # Sets are unordered; sort the fingerprints themselves so the result is
        # deterministic regardless of iteration order.
        items = sorted(
            (_stable_key(fingerprint(x, depth + 1, seen)) for x in obj),
        )[:MAX_ITEMS]
        return [kind, items]

    if isinstance(obj, dict):
        pairs = []
        for key, value in obj.items():
            pairs.append(
                (
                    _stable_key(fingerprint(key, depth + 1, seen)),
                    fingerprint(value, depth + 1, seen),
                )
            )
        pairs.sort(key=lambda kv: kv[0])
        return ["dict", pairs[:MAX_ITEMS]]

    if isinstance(obj, BaseException):
        return [
            "exception",
            _type_name(obj),
            _scrub(str(obj)),
            [fingerprint(a, depth + 1, seen) for a in obj.args[:MAX_ITEMS]],
        ]

    if isinstance(obj, type):
        return ["class", f"{obj.__module__}.{obj.__qualname__}"]

    if callable(obj) and hasattr(obj, "__qualname__"):
        return ["callable", getattr(obj, "__module__", "?"), obj.__qualname__]

    # numpy.ndarray: shape + dtype + values with float tolerance (lazy import).
    try:
        import numpy as np  # type: ignore
    except ImportError:  # pragma: no cover
        np = None  # type: ignore
    if np is not None and isinstance(obj, np.ndarray):
        # Object arrays fall through to generic handling of each element.
        if obj.dtype == object:
            flat = [fingerprint(x, depth + 1, seen) for x in obj.flat[:MAX_ITEMS]]
            return ["ndarray", list(obj.shape), "object", flat]
        if np.issubdtype(obj.dtype, np.floating):
            # Round to a stable decimal so bit-noise does not fail checks.
            data = np.round(obj.astype(np.float64), decimals=12)
            return [
                "ndarray",
                list(obj.shape),
                str(obj.dtype),
                data.reshape(-1)[:MAX_ITEMS].tolist(),
            ]
        return [
            "ndarray",
            list(obj.shape),
            str(obj.dtype),
            obj.reshape(-1)[:MAX_ITEMS].tolist(),
        ]

    # Instances: prefer their own state over repr, since a custom __repr__ may
    # hide fields and a default __repr__ carries an address.
    state = _instance_state(obj)
    if state is not None:
        return [
            "object",
            _type_name(obj),
            fingerprint(state, depth + 1, seen),
        ]

    return ["repr", _type_name(obj), _scrub(repr(obj))]


def _instance_state(obj: object) -> dict | None:
    """Best-effort extraction of an instance's own attributes."""
    data: dict = {}
    found = False

    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        data.update(d)
        found = True

    for cls in type(obj).__mro__:
        slots = cls.__dict__.get("__slots__")
        if slots is None:
            continue
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name == "__dict__" or name == "__weakref__":
                continue
            try:
                data[name] = getattr(obj, name)
            except AttributeError:
                continue
            found = True

    return data if found else None


def _stable_key(fp) -> str:
    """Deterministic string form of a fingerprint, for sorting."""
    import json

    return json.dumps(fp, sort_keys=True, default=str)


def digest(fp) -> str:
    import hashlib
    import json

    blob = json.dumps(fp, sort_keys=True, default=str).encode("utf-8", "replace")
    return hashlib.blake2b(blob, digest_size=16).hexdigest()
