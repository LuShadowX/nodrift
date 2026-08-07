"""Recording under pytest-xdist.

Before this was handled, the xdist controller wrote an empty recording over
the workers' output, so `check` reported "no behaviour change across 0
recorded inputs" — a confident all-clear based on nothing.
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import textwrap

import pytest

from nodrift.recorder import merge_recordings

PKG = '''
def band(n):
    return "high" if n > 100 else "low"


def size(text):
    return len(text)
'''

TESTS = '''
import calc
import pytest


@pytest.mark.parametrize("n", range(60))
def test_band(n):
    assert calc.band(n) in ("high", "low")


@pytest.mark.parametrize("s", ["a" * i for i in range(60)])
def test_size(s):
    assert calc.size(s) == len(s)
'''


def _write(root, name, source):
    path = os.path.join(str(root), name)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(source))


def test_merge_dedups_and_respects_cap(tmp_path):
    def shard(name, records):
        path = str(tmp_path / name)
        with open(path, "wb") as fh:
            pickle.dump(records, fh)
        return path

    a = shard("a.pkl", [{"target": "m:f", "args": b"1"},
                        {"target": "m:f", "args": b"2"}])
    b = shard("b.pkl", [{"target": "m:f", "args": b"2"},   # duplicate of a
                        {"target": "m:f", "args": b"3"},
                        {"target": "m:g", "args": b"9"}])

    out = str(tmp_path / "merged.pkl")
    result = merge_recordings([a, b], out)
    assert result["records"] == 4, "duplicate across shards was not collapsed"
    assert result["targets"] == 2

    # Each worker enforces the cap independently, so it has to be re-applied.
    capped = merge_recordings([a, b], out, cap=2)
    assert capped["records"] == 3          # 2 of m:f, 1 of m:g
    assert capped["dropped_over_cap"] == 1


@pytest.mark.skipif(
    subprocess.run([sys.executable, "-c", "import xdist"],
                   capture_output=True).returncode != 0,
    reason="pytest-xdist not installed",
)
def test_parallel_recording_is_not_empty(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write(project, "calc.py", PKG)
    _write(project, "test_calc.py", TESTS)
    out = str(tmp_path / "rec.pkl")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(project), "-q", "-p", "no:cacheprovider",
         "-n", "2", "--nodrift", "calc", "--nodrift-out", out],
        capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=str(project)),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert os.path.exists(out), proc.stderr

    with open(out, "rb") as fh:
        records = pickle.load(fh)["records"]
    assert records, "controller clobbered the workers' recording"
    assert {r["target"] for r in records} >= {"calc:band", "calc:size"}

    # Shards must be cleaned up, not left beside the recording.
    leftovers = [p for p in os.listdir(str(tmp_path)) if p.startswith("rec.pkl.")]
    assert not leftovers, f"worker shards left behind: {leftovers}"
