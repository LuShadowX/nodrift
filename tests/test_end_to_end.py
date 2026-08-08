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
import time

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
        [sys.executable, "-m", "pytest", ".", "-q", "-p", "no:cacheprovider",
         "--nodrift", "toy", "--nodrift-out", recording],
        # Run from inside the project. Given an absolute path, pytest walks up
        # looking for a rootdir, and on Windows that reaches C:\ and dies on
        # the permission-denied "Documents and Settings" junction.
        cwd=str(project),
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


# --------------------------------------------------------------------------
# functions the recorder had to give up on
# --------------------------------------------------------------------------

# `digest` is called with arguments past max_blob_bytes often enough to trip
# abandon_after, so the recorder stops attempting it. `classify` stays cheap
# and records normally, which is what makes the gap partial rather than total.
BIG = '''
def classify(n):
    return "positive" if n > 0 else "other"


def digest(payload):
    return len(payload)
'''

BIG_TESTS = '''
import big


def test_classify():
    assert big.classify(1) == "positive"


def test_digest():
    for i in range(6):
        assert big.digest("x" * 80_000 + str(i)) == 80_000 + len(str(i))
'''


def _big_repo(tmp_path):
    repo = tmp_path / "bigrepo"
    repo.mkdir()
    _write(str(repo), "big.py", BIG)
    _write(str(repo), "test_big.py", BIG_TESTS)
    for argv in (["init", "-q", "."], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-qm", "initial"]):
        subprocess.run(["git", *argv], cwd=str(repo), check=True,
                       capture_output=True)
    return repo


def test_abandoned_functions_survive_into_the_recording(tmp_path):
    """The gap has to travel with the recording — `check` is a later process."""
    from nodrift.cli import _load_abandoned

    repo = _big_repo(tmp_path)
    recorded = _nodrift(repo, "record", "--package", "big")
    recording = repo / ".nodrift" / "recording.pkl"
    assert recording.exists(), recorded.stdout + recorded.stderr

    assert "big:digest" in _load_abandoned(str(recording))


def test_check_names_the_functions_it_does_not_cover(tmp_path):
    """A clean verdict that hides an uncovered function reads as a lie."""
    repo = _big_repo(tmp_path)
    _nodrift(repo, "record", "--package", "big")

    quiet = _nodrift(repo, "check", "HEAD")
    assert quiet.returncode == 0, quiet.stdout + quiet.stderr
    assert "not covered by this check" in quiet.stdout
    assert "big:digest" not in quiet.stdout, "names belong behind --verbose"

    loud = _nodrift(repo, "check", "HEAD", "--verbose")
    assert "big:digest" in loud.stdout, loud.stdout

    as_json = _nodrift(repo, "check", "HEAD", "--json")
    assert "big:digest" in json.loads(as_json.stdout)["not_covered"]


def test_check_surfaces_a_bad_ref_instead_of_a_name_error(tmp_path):
    """Regression: reporting inside the `finally` masked the real failure.

    With `_print_report` in the teardown path, a replay that raised left
    `report` unbound, so the user saw a NameError instead of the git error.
    """
    repo = _big_repo(tmp_path)
    _nodrift(repo, "record", "--package", "big")

    result = _nodrift(repo, "check", "no-such-ref")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "NameError" not in combined, combined


def test_recordings_are_compressed_on_disk(recorded):
    """A 4.5k-LOC library produced 68 MB uncompressed; gzip takes it to 3 MB."""
    _, recording, _ = recorded
    with open(recording, "rb") as fh:
        assert fh.read(2) == b"\x1f\x8b", "recording was written uncompressed"


def test_legacy_recordings_without_the_payload_wrapper_still_replay(
    recorded, tmp_path
):
    """Recordings made before the wrapper are a bare, uncompressed list.

    Covers both migrations at once: no gzip header and no payload dict.
    """
    import pickle

    from nodrift.cli import _load_abandoned
    from nodrift.recorder import load_recording

    project, recording, _ = recorded
    payload = load_recording(recording)

    legacy = str(tmp_path / "legacy.pkl")
    with open(legacy, "wb") as fh:
        pickle.dump(payload["records"], fh)

    assert _load_abandoned(legacy) == []
    out = str(tmp_path / "legacy.json")
    _replay(str(project), legacy, out)
    with open(out) as fh:
        assert json.load(fh), "legacy recording replayed to nothing"


def test_merge_keeps_every_workers_abandoned_targets(tmp_path):
    """Under xdist the merge is the only place the union can be formed."""
    from nodrift.recorder import load_recording, merge_recordings, write_recording

    shards = []
    for index, names in enumerate((["a:one"], ["a:two", "a:one"])):
        shard = str(tmp_path / f"shard.gw{index}")
        write_recording(
            shard, [{"target": "a:kept", "args": b"%d" % index}], names,
        )
        shards.append(shard)

    out = str(tmp_path / "merged.pkl")
    summary = merge_recordings(shards, out)

    assert summary["abandoned"] == ["a:one", "a:two"]
    assert load_recording(out)["abandoned"] == ["a:one", "a:two"]


def test_merge_reads_uncompressed_shards_from_an_older_nodrift(tmp_path):
    """A half-upgraded install must not silently drop a worker's inputs."""
    import pickle

    from nodrift.recorder import load_recording, merge_recordings

    shard = str(tmp_path / "old.gw0")
    with open(shard, "wb") as fh:
        pickle.dump([{"target": "a:kept", "args": b"1"}], fh)

    out = str(tmp_path / "merged.pkl")
    merge_recordings([shard], out)

    assert load_recording(out)["records"] == [{"target": "a:kept", "args": b"1"}]


# --------------------------------------------------------------------------
# the per-call timeout, on platforms with and without SIGALRM
# --------------------------------------------------------------------------

def _spin(seconds):
    """Burn time in pure Python, where an async exception can reach us."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass


@pytest.mark.parametrize("mechanism", ["sigalrm", "watchdog"])
def test_the_deadline_aborts_a_hung_call(monkeypatch, mechanism):
    """A mutation that loops forever must not hang the whole run.

    The watchdog case is forced on every platform, so the Windows path is
    exercised by CI on Linux and macOS rather than only where it ships.
    """
    import signal as signal_module

    from nodrift import replay

    if mechanism == "sigalrm":
        if not replay.HAVE_SIGALRM:
            pytest.skip("platform has no SIGALRM")
        signal_module.signal(signal_module.SIGALRM, replay._alarm)
    monkeypatch.setattr(replay, "HAVE_SIGALRM", mechanism == "sigalrm")

    started = time.monotonic()
    with pytest.raises(replay._Timeout):
        with replay._deadline(1):
            _spin(30)
    assert time.monotonic() - started < 10, "deadline did not fire promptly"


def test_the_watchdog_leaves_a_call_inside_its_deadline_alone(monkeypatch):
    from nodrift import replay

    monkeypatch.setattr(replay, "HAVE_SIGALRM", False)
    with replay._deadline(30):
        value = sum(range(1000))
    assert value == 499500


def test_the_watchdog_recovers_after_firing(monkeypatch):
    """A record that times out must not affect the record that follows it.

    The watchdog leaves an async exception pending in the calling thread; if
    it is not cleared on the way out it lands on whatever runs next, and the
    tool reports a timeout against innocent code.
    """
    from nodrift import replay

    monkeypatch.setattr(replay, "HAVE_SIGALRM", False)

    with pytest.raises(replay._Timeout):
        with replay._deadline(1):
            _spin(30)

    # The next call must be untouched, both outside a deadline and inside one.
    assert sum(range(100_000)) == 4_999_950_000
    with replay._deadline(30):
        assert sum(range(1000)) == 499500


def test_both_timeout_mechanisms_replay_a_recording_identically(
    recorded, tmp_path, monkeypatch
):
    """The fallback has to agree with SIGALRM, not merely avoid crashing.

    Replaying the same recording under each mechanism must produce the same
    fingerprints — a portability fix that changed results would be worse than
    no portability at all.
    """
    from nodrift import replay

    project, recording, _ = recorded
    monkeypatch.syspath_prepend(str(project))

    if not replay.HAVE_SIGALRM:
        pytest.skip("platform has no SIGALRM to compare against")

    monkeypatch.setattr(replay, "HAVE_SIGALRM", True)
    first = replay.run(recording, deterministic=True)
    second = replay.run(recording, deterministic=True)

    monkeypatch.setattr(replay, "HAVE_SIGALRM", False)
    with_watchdog = replay.run(recording, deterministic=True)

    # The toy package puts id(self) in a __repr__, so two runs of the *same*
    # mechanism already disagree there. Quarantine those the way compare()
    # does, and hold the rest to exact equality.
    stable = {key for key, value in first.items() if second.get(key) == value}
    assert stable, "replay produced nothing deterministic to compare"
    assert {k: with_watchdog[k] for k in stable} == {k: first[k] for k in stable}
