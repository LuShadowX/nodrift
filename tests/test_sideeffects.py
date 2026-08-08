"""File writes as observable behaviour."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from nodrift.compare import compare
from nodrift.sideeffects import WriteWatcher, normalise


def test_writes_are_recorded_with_path_mode_and_hash(tmp_path):
    target = tmp_path / "out.txt"
    with WriteWatcher(root=str(tmp_path)) as watcher:
        with open(target, "w") as fh:
            fh.write("hello")

    (path, mode, size, digest), = watcher.summary()
    assert path == "out.txt"          # relative to root, not the temp prefix
    assert "w" in mode
    assert size == 5
    assert digest


def test_reads_are_not_recorded(tmp_path):
    target = tmp_path / "in.txt"
    target.write_text("data")
    with WriteWatcher(root=str(tmp_path)) as watcher:
        with open(target) as fh:
            fh.read()
    assert watcher.summary() == []


def test_different_content_gives_a_different_hash(tmp_path):
    def write(text):
        with WriteWatcher(root=str(tmp_path)) as watcher:
            with open(tmp_path / "f.txt", "w") as fh:
                fh.write(text)
        return watcher.summary()

    assert write("aaa") == write("aaa")
    assert write("aaa") != write("bbb")


def test_pathlib_writes_are_caught(tmp_path):
    """pathlib holds its own reference to io.open, so builtins alone misses it."""
    with WriteWatcher(root=str(tmp_path)) as watcher:
        pathlib.Path(tmp_path / "p.txt").write_text("via pathlib")
    assert watcher.summary(), "pathlib write was not observed"


def test_writelines_is_counted(tmp_path):
    with WriteWatcher(root=str(tmp_path)) as watcher:
        with open(tmp_path / "l.txt", "w") as fh:
            fh.writelines(["a\n", "b\n"])
    (_, _, size, _), = watcher.summary()
    assert size == 4


@pytest.mark.parametrize("root", [
    "/var/folders/_w/p89dnxrd7tl7clzwytpbg6th0000gn/T",   # macOS
    "/var/folders/ab/T",                                   # shorter variant
    "/tmp",                                                # Linux
])
def test_normalise_strips_per_run_temp_directories(root):
    a = normalise(f"{root}/nodrift-xyz/pkg/f.py", root="/nowhere")
    b = normalise(f"{root}/nodrift-abc/pkg/f.py", root="/nowhere")
    assert a == b, "two runs would look different purely from their temp path"


def test_normalise_handles_the_real_tempdir(tmp_path):
    import tempfile
    a = normalise(os.path.join(tempfile.mkdtemp(), "x.py"), root="/nowhere")
    b = normalise(os.path.join(tempfile.mkdtemp(), "x.py"), root="/nowhere")
    assert a == b


def test_open_is_restored_afterwards(tmp_path):
    import builtins
    import io

    before = (builtins.open, io.open)
    with WriteWatcher(root=str(tmp_path)):
        pass
    assert (builtins.open, io.open) == before


# -- end to end -------------------------------------------------------------

PKG = '''
def save(path, name):
    with open(path, "w") as fh:
        fh.write("user=" + name)
    return "ok"
'''

# Same return value, different bytes on disk. Invisible before write capture.
MUTATED = PKG.replace('"user=" + name', '"username=" + name')

TESTS = '''
import os, tempfile
import app

def test_save():
    path = os.path.join(tempfile.mkdtemp(), "u.txt")
    assert app.save(path, "lu") == "ok"
'''


def _write(root, name, source):
    with open(os.path.join(str(root), name), "w") as fh:
        fh.write(textwrap.dedent(source))


def _replay(source_dir, recording, out):
    subprocess.run(
        [sys.executable, "-m", "nodrift.replay", recording, out, "--deterministic"],
        env=dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=str(source_dir)),
        cwd=str(source_dir), check=True, capture_output=True,
    )


def test_write_change_is_detected_though_return_value_is_identical(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write(project, "app.py", PKG)
    _write(project, "test_app.py", TESTS)
    recording = str(tmp_path / "rec.pkl")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(project), "-q", "-p", "no:cacheprovider",
         "--nodrift", "app", "--nodrift-out", recording],
        capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=str(project)),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    stage = tmp_path / "stage"
    stage.mkdir()

    _write(stage, "app.py", PKG)
    base1, base2 = str(tmp_path / "b1.json"), str(tmp_path / "b2.json")
    _replay(stage, recording, base1)
    _replay(stage, recording, base2)
    assert compare(base1, base2)["total_differs"] == 0, "unstable on identical code"

    _write(stage, "app.py", MUTATED)
    head = str(tmp_path / "head.json")
    _replay(stage, recording, head)

    report = compare(base1, head, base2)
    assert report["total_differs"] > 0, (
        "a changed file write went unnoticed; the return value is 'ok' either way"
    )
    assert any("save" in target for target in report["changed"])


# --------------------------------------------------------------------------
# path scrubbing across operating systems
# --------------------------------------------------------------------------

def test_windows_temp_directories_are_scrubbed():
    """The per-run segment has to go, whichever OS wrote the path.

    Cause 4 of the false positives fixed in 0.1.0 was a per-run temp directory
    surviving into a recorded write path. The patterns that fixed it were
    POSIX-only, so on Windows two runs of identical code compared unequal.
    Asserted against literal Windows paths so it holds on any host.
    """
    from nodrift.sideeffects import _TEMPISH, _slash

    def scrub(path):
        return _TEMPISH.sub(r"\1/<tmp>", _slash(path))

    first = scrub(r"C:\Users\lu\AppData\Local\Temp\nodrift-a1b2\out.txt")
    second = scrub(r"C:\Users\lu\AppData\Local\Temp\nodrift-z9y8\out.txt")
    assert first == second == "C:/Users/lu/AppData/Local/Temp/<tmp>/out.txt"

    # Drive letter and casing vary on Windows; the pattern must not care.
    assert scrub(r"d:\users\lu\appdata\local\temp\xyz\out.txt").endswith(
        "/<tmp>/out.txt"
    )
    assert scrub(r"C:\Windows\Temp\abc\log.txt") == "C:/Windows/Temp/<tmp>/log.txt"


def test_posix_temp_directories_are_still_scrubbed():
    """The Windows patterns must not have displaced the existing ones."""
    from nodrift.sideeffects import _TEMPISH, _slash

    def scrub(path):
        return _TEMPISH.sub(r"\1/<tmp>", _slash(path))

    assert scrub("/tmp/nodrift-a1b2/out.txt") == "/tmp/<tmp>/out.txt"
    assert (
        scrub("/private/var/folders/xy/hash1234/T/nodrift-a1/out.txt")
        == "/private/var/folders/xy/hash1234/T/<tmp>/out.txt"
    )


def test_paths_inside_the_root_come_back_with_forward_slashes(tmp_path):
    """Recordings must not differ by separator alone."""
    root = str(tmp_path)
    nested = os.path.join(root, "pkg", "data", "out.txt")
    assert normalise(nested, root=root) == "pkg/data/out.txt"
