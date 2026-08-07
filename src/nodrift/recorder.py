"""Record the real inputs a package's functions receive during a test run.

The premise of the tool: a project's existing test suite already exercises its
functions with realistic inputs. Rather than synthesising inputs (which needs
good type annotations that real code rarely has), we harvest the ones that are
already flowing through.
"""

from __future__ import annotations

import functools
import gzip
import pickle
import sys
import threading
import types
from collections import defaultdict

from .fingerprint import digest, fingerprint

_local = threading.local()

_GZIP_MAGIC = b"\x1f\x8b"
# Measured on sqlparse: 68.8 MB -> 3.0 MB (23x) for 0.6s. lzma reaches 116x
# but costs 6.5s to write and 5x more to read, and `check` reads the recording
# three times per run — so the cheap codec wins on the axis that repeats.
_COMPRESS_LEVEL = 6


def write_recording(path: str, records: list[dict], abandoned: list[str]) -> None:
    with gzip.open(path, "wb", compresslevel=_COMPRESS_LEVEL) as fh:
        pickle.dump(
            {"version": 1, "records": records, "abandoned": abandoned},
            fh, protocol=pickle.HIGHEST_PROTOCOL,
        )


def load_recording(path: str) -> dict:
    """Read a recording, whatever shape or encoding it was written in.

    Both the payload wrapper and compression arrived after 0.1.0, so the file
    is sniffed rather than trusted: recordings already on disk keep working,
    and a bare list simply has no abandoned functions to report.
    """
    with open(path, "rb") as fh:
        compressed = fh.read(2) == _GZIP_MAGIC
    opener = gzip.open if compressed else open
    with opener(path, "rb") as fh:
        payload = pickle.load(fh)
    if isinstance(payload, dict):
        return payload
    return {"version": 1, "records": payload, "abandoned": []}

# Dunders that carry observable behaviour and are safe to intercept.
DUNDER_ALLOW = frozenset({
    "__call__", "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
    "__hash__", "__repr__", "__str__", "__format__", "__len__", "__bool__",
    "__contains__", "__getitem__", "__iter__", "__next__", "__add__",
    "__sub__", "__mul__", "__and__", "__or__", "__xor__", "__invert__",
    "__neg__", "__abs__", "__int__", "__float__", "__index__",
    "__getstate__", "__setstate__",
})

# Intercepting these would recurse through our own machinery or fire during
# object construction, when the instance cannot be safely pickled.
DUNDER_DENY = frozenset({
    "__getattr__", "__getattribute__", "__setattr__", "__delattr__",
    "__new__", "__init__", "__del__", "__reduce__", "__reduce_ex__",
    "__class_getitem__", "__init_subclass__", "__subclasshook__",
    "__instancecheck__", "__subclasscheck__", "__set_name__",
})


class Recorder:
    def __init__(
        self,
        packages: list[str],
        max_per_target: int = 600,
        max_blob_bytes: int = 32 * 1024,
    ):
        self.packages = tuple(packages)
        self.max_per_target = max_per_target
        # Recursive parsers hand whole syntax trees to every call. Pickling
        # those repeatedly is what turns a 4k-LOC library into a multi-GB
        # recording, so oversized inputs are counted and dropped.
        self.max_blob_bytes = max_blob_bytes
        self.abandon_after = 3
        self._oversized_streak: dict[str, int] = defaultdict(int)
        self._abandoned: set[str] = set()
        # Adaptive sampling: how many consecutive duplicates before backing
        # off, and how far back-off is allowed to go.
        self.backoff_after = 4
        self.max_skip = 1024
        self._dup_streak: dict[str, int] = defaultdict(int)
        self._skip: dict[str, int] = defaultdict(lambda: 1)
        self._countdown: dict[str, int] = defaultdict(int)
        self.calls: dict[str, dict[str, bytes]] = defaultdict(dict)
        self.recorded_outcome: dict[str, dict[str, list]] = defaultdict(dict)
        self.stats = defaultdict(int)
        # Guards every counter below. A threaded suite would otherwise exceed
        # max_per_target, lose stat counts, and make back-off erratic — the
        # numbers the user is shown have to be true.
        self._lock = threading.Lock()
        self._patched: list[tuple[object, str, object]] = []
        self._wrapped_ids: set[int] = set()

    @property
    def patched_count(self) -> int:
        return len(self._patched)

    # -- installation ---------------------------------------------------

    def _owns(self, obj) -> bool:
        mod = getattr(obj, "__module__", None) or ""
        return any(mod == p or mod.startswith(p + ".") for p in self.packages)

    def install(self) -> None:
        for name in list(sys.modules):
            # Recording our own machinery means every capture re-enters the
            # recorder; the run never finishes.
            if name == __package__ or name.startswith(__package__ + "."):
                continue
            if not any(name == p or name.startswith(p + ".") for p in self.packages):
                continue
            module = sys.modules.get(name)
            if module is None:
                continue
            self._patch_module(module)

    def _patch_module(self, module: types.ModuleType) -> None:
        for attr, obj in list(vars(module).items()):
            if isinstance(obj, types.FunctionType) and self._owns(obj):
                self._patch(module, attr, obj, f"{module.__name__}:{obj.__qualname__}")
            elif isinstance(obj, type) and self._owns(obj):
                self._patch_class(module, obj)

    def _patch_class(self, module: types.ModuleType, cls: type) -> None:
        for attr, obj in list(vars(cls).items()):
            if attr in DUNDER_DENY:
                continue
            if attr.startswith("__") and attr.endswith("__") and attr not in DUNDER_ALLOW:
                continue
            target = f"{module.__name__}:{cls.__qualname__}.{attr}"
            if isinstance(obj, types.FunctionType):
                self._patch(cls, attr, obj, target)
            elif isinstance(obj, staticmethod) and isinstance(
                obj.__func__, types.FunctionType
            ):
                wrapped = self._wrap(obj.__func__, target)
                if wrapped is not obj.__func__:
                    self._set(cls, attr, obj, staticmethod(wrapped))
            elif isinstance(obj, classmethod) and isinstance(
                obj.__func__, types.FunctionType
            ):
                wrapped = self._wrap(obj.__func__, target)
                if wrapped is not obj.__func__:
                    self._set(cls, attr, obj, classmethod(wrapped))
            elif isinstance(obj, property) and isinstance(
                obj.fget, types.FunctionType
            ):
                wrapped = self._wrap(obj.fget, target)
                if wrapped is not obj.fget:
                    self._set(
                        cls, attr, obj,
                        property(wrapped, obj.fset, obj.fdel, obj.__doc__),
                    )
            elif isinstance(obj, functools.cached_property) and isinstance(
                obj.func, types.FunctionType
            ):
                wrapped = self._wrap(obj.func, target)
                if wrapped is not obj.func:
                    replacement = functools.cached_property(wrapped)
                    replacement.__set_name__(cls, attr)
                    self._set(cls, attr, obj, replacement)

    def _patch(self, owner, attr: str, func, target: str) -> None:
        wrapped = self._wrap(func, target)
        if wrapped is not func:
            self._set(owner, attr, func, wrapped)

    def _set(self, owner, attr: str, original, replacement) -> None:
        try:
            setattr(owner, attr, replacement)
        except (AttributeError, TypeError):
            return
        self._patched.append((owner, attr, original))

    def _wrap(self, func, target: str):
        # Generator functions are wrapped like any other: calling one only
        # creates the generator, so the wrapper stays non-invasive. Replay is
        # what materialises the sequence.
        if id(func) in self._wrapped_ids:
            return func
        recorder = self

        def wrapper(*args, **kwargs):
            recorder._capture(target, args, kwargs)
            return func(*args, **kwargs)

        try:
            wrapper.__name__ = func.__name__
            wrapper.__qualname__ = func.__qualname__
            wrapper.__module__ = func.__module__
            wrapper.__doc__ = func.__doc__
            wrapper.__wrapped__ = func
        except (AttributeError, TypeError):
            pass
        self._wrapped_ids.add(id(wrapper))
        return wrapper

    # -- capture --------------------------------------------------------

    def _capture(self, target: str, args, kwargs) -> None:
        # Pickling can re-enter recorded code via __reduce__; never record
        # calls that originate from our own machinery.
        if getattr(_local, "busy", False):
            return
        with self._lock:
            if target in self._abandoned:
                return
            bucket = self.calls[target]
            if len(bucket) >= self.max_per_target:
                self.stats["skipped_at_cap"] += 1
                return

            # Whether an input is new is only knowable after pickling it, and
            # hot functions are called with the same handful of values over and
            # over. Recording naively therefore spends most of its time
            # serialising inputs it already has. Once a target starts repeating
            # itself we back off geometrically, so the cost tracks how much a
            # function still has left to teach us rather than how often it is
            # called.
            if self._skip[target] > 1:
                self._countdown[target] -= 1
                if self._countdown[target] > 0:
                    self.stats["sampled_out"] += 1
                    return
                self._countdown[target] = self._skip[target]

        # Pickling stays outside the lock: it is the expensive part, and
        # serialising threads on it would make recording a bottleneck rather
        # than an observer.
        _local.busy = True
        try:
            blob = pickle.dumps((args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            with self._lock:
                self.stats["unpicklable"] += 1
            return
        finally:
            _local.busy = False

        with self._lock:
            if len(blob) > self.max_blob_bytes:
                self.stats["oversized"] += 1
                # Paying the pickle cost only to discard the result is what
                # makes recursive parsers unrecordable. Once a target has
                # produced oversized inputs repeatedly, stop attempting it.
                self._oversized_streak[target] += 1
                if self._oversized_streak[target] >= self.abandon_after:
                    self._abandoned.add(target)
                    self.stats["abandoned_targets"] += 1
                return
            self._oversized_streak[target] = 0

            self.stats["captured"] += 1
            key = digest(["blob", len(blob), _cheap_hash(blob)])
            if key in bucket:
                # Seen before. Back off, but never so far that a function which
                # later receives novel inputs stays invisible.
                self._dup_streak[target] += 1
                if self._dup_streak[target] >= self.backoff_after:
                    self._skip[target] = min(
                        self._skip[target] * 2 or 2, self.max_skip)
                    self._countdown[target] = self._skip[target]
                    self._dup_streak[target] = 0
                return
            # The cap was checked before pickling; another thread may have
            # filled the bucket while this call was serialising.
            if len(bucket) >= self.max_per_target:
                self.stats["skipped_at_cap"] += 1
                return
            # Something new: this function is still productive, so sample it
            # fully again.
            self._dup_streak[target] = 0
            self._skip[target] = 1
            bucket[key] = blob

    # -- teardown -------------------------------------------------------

    def uninstall(self) -> None:
        for owner, attr, original in reversed(self._patched):
            try:
                setattr(owner, attr, original)
            except Exception:
                pass
        self._patched.clear()

    def dump(self, path: str) -> dict:
        records = []
        for target, bucket in self.calls.items():
            for blob in bucket.values():
                records.append({"target": target, "args": blob})
        abandoned = sorted(self._abandoned)
        # The abandoned list travels with the recording: `check` runs in a
        # later process and has no other way to know which functions its
        # verdict does not cover.
        write_recording(path, records, abandoned)
        summary = {
            "targets": len(self.calls),
            "records": len(records),
            **dict(self.stats),
            # Named explicitly: these functions are simply not covered, and a
            # silent gap would read as "verified" when it is not.
            "abandoned": abandoned,
        }
        return summary


def _cheap_hash(blob: bytes) -> str:
    import hashlib

    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def merge_recordings(shards: list[str], out: str, cap: int | None = None) -> dict:
    """Combine per-worker recordings into one, dropping duplicates.

    Under pytest-xdist each worker records in its own process, so a run's
    inputs are spread across several files. Dedup uses the same
    (target, bytes) identity the recorder uses within a process, so a value
    two workers both saw is stored once.

    `cap` is re-applied here because each worker enforced it independently:
    without this, `-n 4` would quietly record four times what the user asked
    for.
    """
    seen: set[tuple[str, bytes]] = set()
    per_target: dict[str, int] = defaultdict(int)
    merged: list[dict] = []
    abandoned: set[str] = set()
    dropped = 0

    for shard in shards:
        try:
            payload = load_recording(shard)
        except Exception:
            continue
        # A target any worker gave up on is not fully covered overall, even if
        # another worker happened to capture some of its inputs.
        abandoned.update(payload.get("abandoned") or [])
        for record in payload.get("records", []):
            target = record["target"]
            key = (target, record["args"])
            if key in seen:
                continue
            if cap is not None and per_target[target] >= cap:
                dropped += 1
                continue
            seen.add(key)
            per_target[target] += 1
            merged.append(record)

    write_recording(out, merged, sorted(abandoned))

    return {
        "shards": len(shards),
        "records": len(merged),
        "targets": len({r["target"] for r in merged}),
        "dropped_over_cap": dropped,
        "abandoned": sorted(abandoned),
    }
