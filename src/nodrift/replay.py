"""Replay recorded inputs against one version of a codebase.

Run as a subprocess with the version-under-test first on sys.path, so two
versions can be compared without either polluting the other's import state.
"""

from __future__ import annotations

import argparse
import functools
import importlib
import json
import pickle
import signal
import sys
import types

from .fingerprint import fingerprint
from .sideeffects import WriteWatcher

CALL_TIMEOUT_SECONDS = 5


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout("call exceeded time limit")


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

    with open(recording_path, "rb") as fh:
        records = pickle.load(fh)

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
        signal.alarm(CALL_TIMEOUT_SECONDS)
        try:
            with watcher:
                value = _materialise(func(*args, **kwargs))
            outcome = ["return", fingerprint(value)]
        except _Timeout:
            outcome = ["timeout"]
        except BaseException as exc:  # noqa: BLE001 - behaviour includes raising
            outcome = ["raise", fingerprint(exc)]
        finally:
            signal.alarm(0)

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
