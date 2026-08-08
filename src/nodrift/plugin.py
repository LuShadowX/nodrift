"""pytest plugin: harvest the real inputs your test suite already produces.

Inert unless --nodrift is passed (or NODRIFT_PACKAGES is set), so installing
nodrift never changes how your suite runs.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys

import glob

from .recorder import Recorder, merge_recordings

_recorder = None


def _worker_id() -> str:
    """xdist worker name ('gw0'), or '' in the controller / a serial run."""
    return os.environ.get("PYTEST_XDIST_WORKER", "")


def _under_xdist(config) -> bool:
    return bool(getattr(config.option, "numprocesses", None))


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
    group.addoption(
        "--nodrift-include",
        action="store",
        default=None,
        metavar="PATTERNS",
        help="Comma-separated fnmatch patterns; record only matching targets "
             "(e.g. 'mypkg.core.*').",
    )
    group.addoption(
        "--nodrift-exclude",
        action="store",
        default=None,
        metavar="PATTERNS",
        help="Comma-separated fnmatch patterns to skip "
             "(e.g. 'mypkg.vendored.*').",
    )


def _patterns(raw) -> list[str]:
    return [p.strip() for p in str(raw or "").split(",") if p.strip()]


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

    # Under xdist the controller runs no tests, so it records nothing. If it
    # installed anyway it would write an empty file over the workers' output
    # and `check` would report a confident all-clear based on no data.
    if _under_xdist(config) and not _worker_id():
        return

    for name in packages:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            print(f"[nodrift] cannot import {name!r}: {exc}", file=sys.stderr)
            continue
        _import_submodules(module)

    cap = _setting(config, "--nodrift-cap", "NODRIFT_CAP", 600)
    include = _patterns(_setting(config, "--nodrift-include",
                                 "NODRIFT_INCLUDE", None))
    exclude = _patterns(_setting(config, "--nodrift-exclude",
                                 "NODRIFT_EXCLUDE", None))
    _recorder = Recorder(packages, max_per_target=int(cap),
                         include=include, exclude=exclude)
    _recorder.install()
    print(
        f"[nodrift] recording {_recorder.patched_count} callables in "
        f"{', '.join(packages)}",
        file=sys.stderr,
    )
    if _recorder.skipped_targets:
        # Said out loud: a pattern that matched more than intended is a
        # coverage gap, and a silent one would read as "verified".
        print(
            f"[nodrift] {len(_recorder.skipped_targets)} callables skipped by "
            f"--include/--exclude; they are NOT covered",
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

    out = _setting(config, "--nodrift-out", "NODRIFT_OUT", None) or os.path.join(
        ".nodrift", "recording.pkl"
    )

    if _recorder is None:
        # Controller under xdist: the workers have finished and written their
        # shards, so this is where they get stitched together.
        if _under_xdist(config) and not _worker_id():
            shards = sorted(glob.glob(out + ".gw*"))
            if shards:
                merged = merge_recordings(
                    shards, out,
                    cap=int(_setting(config, "--nodrift-cap", "NODRIFT_CAP", 600)),
                )
                for shard in shards:
                    try:
                        os.remove(shard)
                    except OSError:
                        pass
                print(
                    f"[nodrift] merged {merged['shards']} worker recordings: "
                    f"{merged['records']} distinct inputs across "
                    f"{merged['targets']} functions -> {out}",
                    file=sys.stderr,
                )
        return

    _recorder.uninstall()
    if _worker_id():
        out = f"{out}.{_worker_id()}"
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
            f"(inputs too large, or not picklable); they are NOT covered",
            file=sys.stderr,
        )
    _recorder = None
