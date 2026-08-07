"""Replay recorded inputs against one version of a codebase.

Run as a subprocess with the version-under-test first on sys.path, so two
versions can be compared without either polluting the other's import state.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import functools
import importlib
import json
import pickle
import signal
import sys
import threading
import types

from .fingerprint import fingerprint
from .recorder import load_recording
from .sideeffects import WriteWatcher

CALL_TIMEOUT_SECONDS = 5

# SIGALRM is the stronger mechanism — it interrupts some blocking C calls that
# an async exception cannot — and it is what the published results were
# measured with, so it stays the default wherever it exists. Windows has no
# SIGALRM, hence the fallback below.
HAVE_SIGALRM = hasattr(signal, "SIGALRM")


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout("call exceeded time limit")


@contextlib.contextmanager
def _deadline(seconds: int):
    """Abort the wrapped call if it outlasts `seconds`.

    A mutation can easily produce an infinite loop, so this is not optional:
    without it a single bad candidate hangs the whole run.
    """
    if HAVE_SIGALRM:
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
        return

    # Windows: a watchdog thread throws _Timeout into the calling thread.
    # Delivery happens between bytecodes, the same way a signal handler runs,
    # so a call blocked inside C code can still outlast its deadline.
    thread_id = threading.get_ident()
    state = {"lock": threading.Lock(), "fired": False, "done": False}

    def fire():
        with state["lock"]:
            if state["done"]:
                return
            state["fired"] = True
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread_id), ctypes.py_object(_Timeout)
            )

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        # Claim the state before cancelling: if the watchdog is already in
        # flight this blocks until it finishes, so `fired` is trustworthy.
        with state["lock"]:
            state["done"] = True
        timer.cancel()
        if state["fired"]:
            # An exception raised too late to be caught above would otherwise
            # surface during the next record and be blamed on the wrong call.
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread_id), None
            )


def resolve(target: str):
    """'pkg.mod:Class.method' -> the underlying plain function.

    Looks names up in the class __dict__ so descriptors (property,
    classmethod, cached_property) are returned unevaluated, then unwraps them
    to the raw function. The recorded arguments already include `self`/`cls`.
    """
    module_name, _, qualname = target.partition(":")
    obj = importlib.import_module(module_name)
    for part in qualname.split("."):
        if isinstance(obj, type) and part in vars(obj):
            obj = vars(obj)[part]
        else:
            obj = getattr(obj, part)

    if isinstance(obj, property):
        return obj.fget
    if isinstance(obj, functools.cached_property):
        return obj.func
    if isinstance(obj, (staticmethod, classmethod)):
        return obj.__func__
    if isinstance(obj, types.MethodType):
        return obj.__func__
    return obj


MAX_MATERIALISE = 2000


def _materialise(value):
    """Drain generators/iterators so their yielded sequence can be compared.

    A generator's identity says nothing about behaviour; the sequence it
    produces is the behaviour. Bounded so a mutation that creates an infinite
    generator cannot hang the run.
    """
    if isinstance(value, types.GeneratorType) or (
        hasattr(value, "__next__") and hasattr(value, "__iter__")
    ):
        items = []
        try:
            for n, item in enumerate(value):
                if n >= MAX_MATERIALISE:
                    items.append(("truncated",))
                    break
                items.append(item)
        except BaseException as exc:  # a partial sequence then a raise
            return ["stream", items, ["raised", type(exc).__name__, str(exc)]]
        return ["stream", items]
    return value


def run(recording_path: str, deterministic: bool) -> dict:
    if deterministic:
        _install_determinism_controls()

    records = load_recording(recording_path)["records"]

    if HAVE_SIGALRM:
        signal.signal(signal.SIGALRM, _alarm)
    results: dict[str, list] = {}
    resolved: dict[str, object] = {}

    for index, record in enumerate(records):
        target = record["target"]
        key = f"{target}#{index}"

        if target not in resolved:
            try:
                resolved[target] = resolve(target)
            except Exception as exc:
                resolved[target] = exc
        func = resolved[target]
        if isinstance(func, Exception):
            results[key] = ["unresolvable", type(func).__name__]
            continue

        try:
            args, kwargs = pickle.loads(record["args"])
        except Exception as exc:
            results[key] = ["unloadable", type(exc).__name__]
            continue

        before = fingerprint([args, kwargs])
        watcher = WriteWatcher()
        try:
            with _deadline(CALL_TIMEOUT_SECONDS), watcher:
                value = _materialise(func(*args, **kwargs))
            outcome = ["return", fingerprint(value)]
        except _Timeout:
            outcome = ["timeout"]
        except BaseException as exc:  # noqa: BLE001 - behaviour includes raising
            outcome = ["raise", fingerprint(exc)]

        after = fingerprint([args, kwargs])
        results[key] = [outcome, before == after, watcher.summary()]

    return results


def _install_determinism_controls() -> None:
    """Neutralise the common sources of run-to-run variation."""
    import random
    import os

    random.seed(0)
    os.environ["PYTHONHASHSEED"] = "0"

    try:
        import numpy

        numpy.random.seed(0)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recording")
    parser.add_argument("output")
    parser.add_argument("--deterministic", action="store_true")
    args = parser.parse_args()

    results = run(args.recording, args.deterministic)
    with open(args.output, "w") as fh:
        json.dump(results, fh, sort_keys=True, default=str)
    print(f"[bhv] replayed {len(results)} records -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
