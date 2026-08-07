"""Record the real inputs a package's functions receive during a test run.

The premise of the tool: a project's existing test suite already exercises its
functions with realistic inputs. Rather than synthesising inputs (which needs
good type annotations that real code rarely has), we harvest the ones that are
already flowing through.
"""

from __future__ import annotations

import functools
import pickle
import sys
import threading
import types
from collections import defaultdict

from .fingerprint import digest, fingerprint

_local = threading.local()

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
        self.calls: dict[str, dict[str, bytes]] = defaultdict(dict)
        self.recorded_outcome: dict[str, dict[str, list]] = defaultdict(dict)
        self.stats = defaultdict(int)
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
        if target in self._abandoned:
            return
        bucket = self.calls[target]
        if len(bucket) >= self.max_per_target:
            self.stats["skipped_at_cap"] += 1
            return

        _local.busy = True
        try:
            blob = pickle.dumps((args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            self.stats["unpicklable"] += 1
            return
        finally:
            _local.busy = False

        if len(blob) > self.max_blob_bytes:
            self.stats["oversized"] += 1
            # Paying the pickle cost only to discard the result is what makes
            # recursive parsers unrecordable. Once a target has produced
            # oversized inputs repeatedly, stop attempting it at all.
            self._oversized_streak[target] += 1
            if self._oversized_streak[target] >= self.abandon_after:
                self._abandoned.add(target)
                self.stats["abandoned_targets"] += 1
            return
        self._oversized_streak[target] = 0

        self.stats["captured"] += 1
        bucket[digest(["blob", len(blob), _cheap_hash(blob)])] = blob

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
        # Persist abandoned targets with the recording so `nodrift check`
        # can report the coverage gap, not only the pass/fail summary.
        payload = {
            "version": 1,
            "records": records,
            "abandoned": abandoned,
        }
        with open(path, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        summary = {
            "targets": len(self.calls),
            "records": len(records),
            **dict(self.stats),
            "abandoned": abandoned,
        }
        return summary


def _cheap_hash(blob: bytes) -> str:
    import hashlib

    return hashlib.blake2b(blob, digest_size=16).hexdigest()
