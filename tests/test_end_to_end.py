"""End to end: record a toy package's inputs, then replay two versions.

The two properties that matter are asserted here directly — identical code
must report nothing, and a genuine behaviour change must be found.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from nodrift.compare import compare

ORIGINAL = '''
def classify(n):
    if n <= 0:
        return "non-positive"
    return "positive"


def describe(items):
    return {"count": len(items), "first": items[0] if items else None}


class Box:
    def __init__(self, value):
        self.value = value

    def scaled(self, factor):
        return Box(self.value * factor)

    def __repr__(self):
        return f"<Box {self.value} at {id(self)}>"
'''

# `<=` becomes `<`: zero now classifies as positive.
MUTATED = ORIGINAL.replace("if n <= 0:", "if n < 0:")

TESTS = '''
import toy


def test_classify():
    assert toy.classify(5) == "positive"
    assert toy.classify(-1) == "non-positive"
    assert toy.classify(0) == "non-positive"


def test_describe():
    assert toy.describe([1, 2])["count"] == 2
    assert toy.describe([])["first"] is None


def test_box():
    assert toy.Box(3).scaled(2).value == 6
    repr(toy.Box(1))
'''


def _write(root, name, source):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(source))
    return path


def _replay(source_dir, recording, out):
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=source_dir)
    subprocess.run(
        [sys.executable, "-m", "nodrift.replay", recording, out, "--deterministic"],
        env=env, check=True, capture_output=True,
    )


@pytest.fixture
def recorded(tmp_path):
    """Record inputs from the toy package's own test suite."""
    project = tmp_path / "project"
    project.mkdir()
    _write(str(project), "toy.py", ORIGINAL)
    _write(str(project), "test_toy.py", TESTS)

    recording = str(tmp_path / "recording.pkl")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(project), "-q", "-p", "no:cacheprovider",
         "--nodrift", "toy", "--nodrift-out", recording],
        capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=str(project)),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert os.path.exists(recording), proc.stderr
    return project, recording, tmp_path


def test_identical_code_reports_no_change(recorded, tmp_path):
    """The property the tool lives or dies by: no false alarms."""
    project, recording, _ = recorded
    # Both versions live at the same path, so path-dependent output cannot
    # masquerade as a behaviour change.
    stage = tmp_path / "stage"
    stage.mkdir()
    _write(str(stage), "toy.py", ORIGINAL)

    base1 = str(tmp_path / "a.json")
    base2 = str(tmp_path / "b.json")
    head = str(tmp_path / "c.json")
    for out in (base1, base2, head):
        _replay(str(stage), recording, out)

    # Same three-pass shape the CLI uses: two baselines to establish what is
    # nondeterministic, then the candidate.
    assert compare(base1, head, base2)["total_differs"] == 0


def test_behaviour_change_is_detected(recorded, tmp_path):
    project, recording, _ = recorded
    stage = tmp_path / "stage2"
    stage.mkdir()

    _write(str(stage), "toy.py", ORIGINAL)
    base = str(tmp_path / "base.json")
    base2 = str(tmp_path / "base2.json")
    _replay(str(stage), recording, base)
    _replay(str(stage), recording, base2)

    _write(str(stage), "toy.py", MUTATED)
    head = str(tmp_path / "head.json")
    _replay(str(stage), recording, head)

    report = compare(base, head, base2)
    assert report["total_differs"] > 0
    assert any("classify" in target for target in report["changed"])


def test_quarantine_is_what_suppresses_nondeterminism(recorded, tmp_path):
    """`Box.__repr__` embeds `id(self)` in decimal, which no scrubber catches.

    Without the second baseline it is reported as a change; with it, it is
    correctly quarantined. This is why the CLI always replays the baseline
    twice.
    """
    project, recording, _ = recorded
    stage = tmp_path / "stage3"
    stage.mkdir()
    _write(str(stage), "toy.py", ORIGINAL)

    base1 = str(tmp_path / "q1.json")
    base2 = str(tmp_path / "q2.json")
    head = str(tmp_path / "q3.json")
    for out in (base1, base2, head):
        _replay(str(stage), recording, out)

    assert compare(base1, head)["total_differs"] > 0
    report = compare(base1, head, base2)
    assert report["total_differs"] == 0
    assert report["quarantined"] > 0


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(str(repo), "shapes.py", ORIGINAL.replace("classify", "classify"))
    _write(str(repo), "test_shapes.py", TESTS.replace("toy", "shapes"))
    for argv in (["init", "-q", "."], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-qm", "initial"]):
        subprocess.run(["git", *argv], cwd=str(repo), check=True,
                       capture_output=True)
    return repo


def _nodrift(repo, *argv):
    return subprocess.run(
        [sys.executable, "-m", "nodrift.cli", *argv],
        cwd=str(repo), capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=str(repo)),
    )


def test_cli_compares_against_the_ref_not_the_working_tree(tmp_path):
    """Regression: `python -m` puts cwd first on sys.path.

    Replaying from inside the repo made both passes import the live working
    tree, so every check reported "no change" no matter what was edited.
    """
    repo = _git_repo(tmp_path)

    recorded = _nodrift(repo, "record", "--package", "shapes")
    assert (repo / ".nodrift" / "recording.pkl").exists(), recorded.stderr

    clean = _nodrift(repo, "check", "HEAD")
    assert clean.returncode == 0, clean.stdout + clean.stderr

    source = (repo / "shapes.py").read_text()
    (repo / "shapes.py").write_text(source.replace('return "positive"',
                                                   'return "POSITIVE"'))

    changed = _nodrift(repo, "check", "HEAD")
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert "behave differently" in changed.stdout
    assert "classify" in changed.stdout


def test_cli_reports_clean_run(recorded, tmp_path):
    project, recording, _ = recorded
    with open(recording, "rb") as fh:
        assert fh.read(1), "recording should not be empty"
    out = str(tmp_path / "r.json")
    _replay(str(project), recording, out)
    with open(out) as fh:
        results = json.load(fh)
    assert results, "replay produced no results"
