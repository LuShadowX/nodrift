"""Observe the files a call writes, so writes count as behaviour.

Return values are only part of what a function does. A function that writes a
different file, or different bytes, is not behaving the same way — but until
this existed, nodrift could not see the difference.

Two rules govern everything here:

* **Paths are normalised.** Both versions are replayed from the same staged
  directory, but that directory sits under a temp path that differs run to
  run. Recording a raw path would make every call look changed.
* **Content is hashed, never stored.** We need to know *that* the bytes
  differ, not what they were, and recordings are already large.
"""

from __future__ import annotations

import builtins
import hashlib
import io
import os
import re
import tempfile

# A per-run temporary directory, plus the one random segment inside it.
# macOS puts temp files under /var/folders/<2 chars>/<hash>/T/, so matching a
# fixed prefix is not enough — the varying part is deeper than it looks.
_TEMP_ROOTS = [
    r"/private/var/folders(?:/[^/]+){1,2}/T",
    r"/var/folders(?:/[^/]+){1,2}/T",
    re.escape(tempfile.gettempdir()),
    r"/private/tmp",
    r"/tmp",
]
_TEMPISH = re.compile(r"(" + "|".join(_TEMP_ROOTS) + r")/[^/]+")

_WRITE_MODES = set("wxa+")


class _WatchedFile:
    """Delegates to a real file object, hashing whatever is written."""

    __slots__ = ("_fh", "_digest", "_record")

    def __init__(self, fh, record):
        object.__setattr__(self, "_fh", fh)
        object.__setattr__(self, "_digest", hashlib.blake2b(digest_size=16))
        object.__setattr__(self, "_record", record)

    def write(self, data):
        self._feed(data)
        return self._fh.write(data)

    def writelines(self, lines):
        lines = list(lines)
        for line in lines:
            self._feed(line)
        return self._fh.writelines(lines)

    def _feed(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        self._digest.update(data)
        self._record["bytes"] += len(data)
        self._record["hash"] = self._digest.hexdigest()

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)

    def __iter__(self):
        return iter(self._fh)


def normalise(path: str, root: str | None = None) -> str:
    """Strip the parts of a path that differ between two runs."""
    try:
        text = os.fspath(path)
    except TypeError:
        return "<non-path>"
    if not isinstance(text, str):
        text = text.decode("utf-8", "replace")

    root = root or os.getcwd()
    try:
        if os.path.commonpath([os.path.abspath(text), root]) == root:
            return os.path.relpath(os.path.abspath(text), root)
    except (ValueError, OSError):
        pass
    return _TEMPISH.sub(r"\1/<tmp>", text)


class WriteWatcher:
    """Context manager recording every file opened for writing."""

    def __init__(self, root: str | None = None):
        self.root = root or os.getcwd()
        self.writes: list[dict] = []
        self._saved: list[tuple[object, str, object]] = []
        self._depth = 0

    def __enter__(self):
        real_open = builtins.open

        def watched_open(file, mode="r", *args, **kwargs):
            fh = real_open(file, mode, *args, **kwargs)
            if not _WRITE_MODES.intersection(mode):
                return fh
            record = {
                "path": normalise(file, self.root),
                "mode": "".join(sorted(set(mode) - {"b", "t"})),
                "bytes": 0,
                "hash": None,
            }
            self.writes.append(record)
            return _WatchedFile(fh, record)

        # pathlib and many libraries hold their own reference to io.open, so
        # patching builtins alone would miss them.
        for module, name in ((builtins, "open"), (io, "open")):
            self._saved.append((module, name, getattr(module, name)))
            setattr(module, name, watched_open)
        return self

    def __exit__(self, *exc):
        for module, name, original in reversed(self._saved):
            setattr(module, name, original)
        self._saved.clear()
        return False

    def summary(self) -> list:
        """A comparable description of what this call wrote."""
        return sorted(
            [w["path"], w["mode"], w["bytes"], w["hash"]] for w in self.writes
        )
