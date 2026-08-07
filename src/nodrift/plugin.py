"""pytest plugin: harvest the real inputs your test suite already produces.

Inert unless --nodrift is passed (or NODRIFT_PACKAGES is set), so installing
nodrift never changes how your suite runs.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys

from .recorder import Recorder

_recorder = None


def pytest_addoption(parser):
    group = parser.getgroup("nodrift")
    group.addoption(
        "--nodrift",
        action="store",
        default=None,
        metavar="PKGS",
        help="Comma-separated packages to record inputs for.",
    )
    group.addoption(
        "--nodrift-out",
        action="store",
        default=None,
        metavar="PATH",
        help="Where to write the recording (default .nodrift/recording.pkl).",
    )
    group.addoption(
        "--nodrift-cap",
        action="store",
        type=int,
        default=None,
        help="Max distinct inputs recorded per function (default 600).",
    )


def _setting(config, option, env, default):
    value = config.getoption(option)
    if value is not None:
        return value
    return os.environ.get(env, default)


def pytest_configure(config):
    global _recorder

    raw = _setting(config, "--nodrift", "NODRIFT_PACKAGES", None)
    if not raw:
        return
    packages = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not packages:
        return

    for name in packages:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            print(f"[nodrift] cannot import {name!r}: {exc}", file=sys.stderr)
            continue
        _import_submodules(module)

    cap = _setting(config, "--nodrift-cap", "NODRIFT_CAP", 600)
    _recorder = Recorder(packages, max_per_target=int(cap))
    _recorder.install()
    print(
        f"[nodrift] recording {_recorder.patched_count} callables in "
        f"{', '.join(packages)}",
        file=sys.stderr,
    )


def _import_submodules(package) -> None:
    path = getattr(package, "__path__", None)
    if path is None:
        return
    for info in pkgutil.walk_packages(path, package.__name__ + "."):
        try:
            importlib.import_module(info.name)
        except Exception:
            # A submodule that cannot import on this platform simply is not
            # recorded; that is a coverage gap, not a failure.
            continue


def pytest_unconfigure(config):
    global _recorder
    if _recorder is None:
        return

    _recorder.uninstall()
    out = _setting(config, "--nodrift-out", "NODRIFT_OUT", None) or os.path.join(
        ".nodrift", "recording.pkl"
    )
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    summary = _recorder.dump(out)

    print(
        f"[nodrift] {summary['records']} distinct inputs across "
        f"{summary['targets']} functions -> {out}",
        file=sys.stderr,
    )
    if summary.get("abandoned"):
        print(
            f"[nodrift] {len(summary['abandoned'])} functions not recorded "
            f"(inputs too large to capture); they are NOT covered",
            file=sys.stderr,
        )
    _recorder = None
