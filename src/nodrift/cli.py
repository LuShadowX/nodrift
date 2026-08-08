"""nodrift command line interface.

    nodrift record --package mypkg      run the test suite, capture real inputs
    nodrift check HEAD~1                replay them against then and now
    nodrift check HEAD~1 HEAD           or against two refs, neither checked out
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_DIR = ".nodrift"
RECORDING = "recording.pkl"


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------

def cmd_record(args: argparse.Namespace) -> int:
    out = args.out or os.path.join(DEFAULT_DIR, RECORDING)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    command = args.pytest_args or ["-q"]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *command,
         "--nodrift", ",".join(args.package),
         "--nodrift-out", os.path.abspath(out),
         "--nodrift-cap", str(args.cap),
         *(["--nodrift-include", ",".join(args.include)] if args.include else []),
         *(["--nodrift-exclude", ",".join(args.exclude)] if args.exclude else [])],
    )
    if not os.path.exists(out):
        print("nodrift: no recording produced — did any tests run?", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        # A red suite still yields a usable recording; say so rather than
        # failing, but do not pretend the run was clean.
        print(
            "nodrift: tests did not all pass; the recording covers whatever ran.",
            file=sys.stderr,
        )
    return 0


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def _git(*argv: str, cwd: str | None = None) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _strip_bytecode(root: str) -> None:
    """Delete compiled bytecode from a staged tree.

    A repository with `__pycache__` committed — or a stray `.pyc` — hands the
    replay bytecode compiled from *other* source. Python will run it in
    preference to the file we just materialised, so `check` reports no
    behaviour change while the two versions genuinely differ. Silent, and
    worse than an error.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) == "__pycache__":
            shutil.rmtree(dirpath, ignore_errors=True)
            dirnames[:] = []
            continue
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                try:
                    os.remove(os.path.join(dirpath, name))
                except OSError:
                    pass


def _export(ref: str, dest: str) -> None:
    """Materialise `ref` into `dest` (which is emptied first)."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        capture_output=True, check=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", dest], input=archive, check=True)
    _strip_bytecode(dest)


def _export_worktree(dest: str) -> None:
    """Copy the working tree, uncommitted changes included."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    tracked = _git("ls-files").splitlines()
    for rel in tracked:
        src = os.path.abspath(rel)
        if not os.path.exists(src):
            continue
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(src, target)
    _strip_bytecode(dest)


def _replay(recording: str, source_root: str, out: str, sub: str) -> None:
    """Replay in a subprocess with `source_root` as the only source of truth.

    Two things are load-bearing here:

    * `cwd` is the staged copy, never the caller's repo. `python -m` puts the
      working directory first on `sys.path`, so running from the repo would
      import the live tree for *both* versions and silently compare it with
      itself.
    * Every version is staged at the same path, so code that embeds its own
      file location in output cannot make identical versions look different.
    """
    root = os.path.join(source_root, sub) if sub else source_root
    env = dict(
        os.environ,
        PYTHONHASHSEED="0",
        PYTHONPATH=os.pathsep.join(filter(None, [root])),
    )
    env.pop("PYTHONSAFEPATH", None)
    subprocess.run(
        [sys.executable, "-m", "nodrift.replay",
         os.path.abspath(recording), os.path.abspath(out), "--deterministic"],
        env=env, cwd=root, check=True, capture_output=True,
    )


def _load_abandoned(recording: str) -> list[str]:
    """Functions the recorder gave up on, as carried by the recording."""
    from .recorder import load_recording

    return list(load_recording(recording).get("abandoned") or [])


def cmd_check(args: argparse.Namespace) -> int:
    from .compare import compare

    recording = args.recording or os.path.join(DEFAULT_DIR, RECORDING)
    if not os.path.exists(recording):
        print(f"nodrift: no recording at {recording} — run 'nodrift record' first",
              file=sys.stderr)
        return 1

    abandoned = _load_abandoned(recording)

    workdir = tempfile.mkdtemp(prefix="nodrift-")
    stage = os.path.join(workdir, "src")
    results = os.path.join(workdir, "results")
    os.makedirs(results)

    try:
        base_1 = os.path.join(results, "base1.json")
        base_2 = os.path.join(results, "base2.json")
        head = os.path.join(results, "head.json")

        candidate = getattr(args, "against", None)

        print(f"nodrift: replaying {args.ref} ...", file=sys.stderr)
        _export(args.ref, stage)
        _replay(recording, stage, base_1, args.subdir)
        # Second identical pass: anything that disagrees with itself here is
        # nondeterministic and cannot support a claim either way.
        _replay(recording, stage, base_2, args.subdir)

        # With no second ref the candidate is the working tree, which is the
        # common case: check what you are about to commit.
        print(f"nodrift: replaying {candidate or 'working tree'} ...",
              file=sys.stderr)
        if candidate:
            _export(candidate, stage)
        else:
            _export_worktree(stage)
        _replay(recording, stage, head, args.subdir)

        report = compare(base_1, head, base_2)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Deliberately outside the `finally`: if the replay above raised, `report`
    # is unbound and reporting here would mask the real error with a NameError.
    return _print_report(report, args, abandoned)


def _plural(count: int, word: str) -> str:
    """"1 function", "2 functions" — the report is read by people."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _print_not_covered(abandoned: list[str], verbose: bool) -> None:
    """Say which functions the verdict above does not speak for.

    Without this, `check` reports "no behaviour change" while silently
    omitting every function whose inputs could not be recorded — because they
    were too large, or because they were never picklable in the first place
    (a function taking a callback on every call, say). On `sqlparse` that is
    42 of the most important ones.
    """
    if not abandoned:
        return
    print(f"  {_plural(len(abandoned), 'function')} not fully recorded "
          f"(inputs too large, or not picklable) — not covered by this check")
    if verbose:
        for name in abandoned:
            print(f"      {name}")
    else:
        print("      re-run with --verbose to list them")


def _print_report(report: dict, args: argparse.Namespace,
                  abandoned: list[str] | None = None) -> int:
    abandoned = abandoned or []

    if args.json:
        print(json.dumps({**report, "not_covered": abandoned}, indent=2))
        return 1 if report["total_differs"] else 0

    total = report["total_records"]
    differs = report["total_differs"]
    quarantined = report.get("quarantined", 0)

    if quarantined:
        print(f"  {_plural(quarantined, 'record')} quarantined as "
              f"nondeterministic (not compared)")
        # A bare count tells a user nothing about whether the function they
        # care about is among the ones that stopped being checked.
        targets = report.get("quarantined_targets") or []
        if targets:
            if getattr(args, "verbose", False):
                for target in targets:
                    print(f"      {target}")
            else:
                shown = ", ".join(targets[:3])
                more = f", and {len(targets) - 3} more" if len(targets) > 3 else ""
                print(f"      in {shown}{more} — --verbose to list them")

    _print_not_covered(abandoned, getattr(args, "verbose", False))

    if not differs:
        print(f"\n  no behaviour change across "
              f"{_plural(total, 'recorded input')} in "
              f"{_plural(report['total_targets'], 'function')}\n")
        return 0

    print(f"\n  {differs} of {total} recorded inputs behave differently "
          f"({_plural(report['changed_targets'], 'function')})\n")
    for target, counts in sorted(
        report["changed"].items(), key=lambda kv: -kv[1]["differs"]
    ):
        total_t = counts["same"] + counts["differs"]
        print(f"    {target}")
        print(f"        {counts['differs']} of {total_t} inputs differ")
        for example in report["examples"].get(target, [])[:1]:
            print(f"        before: {json.dumps(example['a'], default=str)[:150]}")
            print(f"        after:  {json.dumps(example['b'], default=str)[:150]}")
    print()
    return 1


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nodrift",
        description="Prove a refactor changed nothing, by replaying real inputs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="run the test suite and capture inputs")
    rec.add_argument("--package", "-p", action="append", required=True,
                     help="package to record (repeatable)")
    rec.add_argument("--out", "-o", default=None)
    rec.add_argument("--cap", type=int, default=600,
                     help="max distinct inputs per function (default 600)")
    rec.add_argument("--include", action="append", default=None,
                     metavar="PATTERN",
                     help="record only targets matching this fnmatch pattern "
                          "against 'module:Qualname', e.g. 'mypkg.core*' "
                          "(repeatable)")
    rec.add_argument("--exclude", action="append", default=None,
                     metavar="PATTERN",
                     help="skip targets matching this fnmatch pattern "
                          "against 'module:Qualname', e.g. 'mypkg.vendored*' "
                          "(repeatable)")
    rec.add_argument("pytest_args", nargs="*",
                     help="extra arguments passed through to pytest")
    rec.set_defaults(func=cmd_record)

    chk = sub.add_parser(
        "check", help="compare a git ref against the working tree, or two refs")
    chk.add_argument("ref", nargs="?", default="HEAD",
                     help="git ref to treat as the baseline (default HEAD)")
    chk.add_argument("against", nargs="?", default=None,
                     help="second ref to compare with; defaults to the "
                          "working tree")
    chk.add_argument("--recording", "-r", default=None)
    chk.add_argument("--subdir", default="",
                     help="path within the repo holding the package (e.g. src)")
    chk.add_argument("--json", action="store_true", help="emit the full report")
    chk.add_argument("--verbose", "-v", action="store_true",
                     help="name the functions that were quarantined or could "
                          "not be recorded")
    chk.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
