"""nodrift command line interface.

    nodrift record --package mypkg      run the test suite, capture real inputs
    nodrift check HEAD~1                replay them against then and now
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
         "--nodrift-cap", str(args.cap)],
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


def cmd_check(args: argparse.Namespace) -> int:
    from .compare import compare

    recording = args.recording or os.path.join(DEFAULT_DIR, RECORDING)
    if not os.path.exists(recording):
        print(f"nodrift: no recording at {recording} — run 'nodrift record' first",
              file=sys.stderr)
        return 1

    workdir = tempfile.mkdtemp(prefix="nodrift-")
    stage = os.path.join(workdir, "src")
    results = os.path.join(workdir, "results")
    os.makedirs(results)

    try:
        base_1 = os.path.join(results, "base1.json")
        base_2 = os.path.join(results, "base2.json")
        head = os.path.join(results, "head.json")

        print(f"nodrift: replaying {args.ref} ...", file=sys.stderr)
        _export(args.ref, stage)
        _replay(recording, stage, base_1, args.subdir)
        # Second identical pass: anything that disagrees with itself here is
        # nondeterministic and cannot support a claim either way.
        _replay(recording, stage, base_2, args.subdir)

        print("nodrift: replaying working tree ...", file=sys.stderr)
        _export_worktree(stage)
        _replay(recording, stage, head, args.subdir)

        report = compare(base_1, head, base_2)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return _print_report(report, args)


def _print_report(report: dict, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(report, indent=2))
        return 1 if report["total_differs"] else 0

    total = report["total_records"]
    differs = report["total_differs"]
    quarantined = report.get("quarantined", 0)

    if quarantined:
        print(f"  {quarantined} record(s) quarantined as nondeterministic "
              f"(not compared)")

    if not differs:
        print(f"\n  no behaviour change across {total} recorded inputs "
              f"in {report['total_targets']} functions\n")
        return 0

    print(f"\n  {differs} of {total} recorded inputs behave differently "
          f"({report['changed_targets']} functions)\n")
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
    rec.add_argument("pytest_args", nargs="*",
                     help="extra arguments passed through to pytest")
    rec.set_defaults(func=cmd_record)

    chk = sub.add_parser("check", help="compare a git ref against the working tree")
    chk.add_argument("ref", nargs="?", default="HEAD",
                     help="git ref to compare against (default HEAD)")
    chk.add_argument("--recording", "-r", default=None)
    chk.add_argument("--subdir", default="",
                     help="path within the repo holding the package (e.g. src)")
    chk.add_argument("--json", action="store_true", help="emit the full report")
    chk.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
