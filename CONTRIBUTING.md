# Contributing to nodrift

Thanks for looking. This project is small and deliberately easy to contribute
to — most of the useful work is self-contained and needs no understanding of
the rest of the engine.

## Get set up

```bash
git clone https://github.com/LuShadowX/nodrift
cd nodrift
uv venv && uv pip install -e ".[dev]"    # or: python -m venv .venv && pip install -e ".[dev]"
pytest
```

21 tests, about two seconds. If they pass, you are ready.

## How the pieces fit

Five files, each with one job:

| File | Job |
|---|---|
| `recorder.py` | Wraps your package's functions, pickles the arguments they receive |
| `plugin.py` | The pytest hook that turns recording on |
| `replay.py` | Feeds recorded inputs back into one version of the code |
| `fingerprint.py` | Reduces any Python value to something comparable across processes |
| `compare.py` | Diffs two replay runs, quarantines anything nondeterministic |

The flow is always: **record → replay twice on the baseline → replay the
candidate → compare.**

## The rule that matters most

**A false positive is worse than a missed change.**

If the tool cries wolf once, nobody runs it again. When you are unsure whether
something is a real difference or an artefact of the environment, the correct
behaviour is to quarantine it and say so — not to report it.

Two bugs already found the hard way, both of this kind:

- `Tag.__slots__` cached `hash()`, which Python randomises per process
- `__repr__` embedding `id(self)` in decimal, which no address scrubber catches

Both were fixed by making the tool prove a difference is real (replay the
baseline twice) rather than assuming it.

## Where to start

Issues labelled [`good first issue`][gfi] are genuinely self-contained — most
touch one file and need one test.

[gfi]: https://github.com/LuShadowX/nodrift/labels/good%20first%20issue

The broad areas that need help:

- **Side-effect capture.** Right now only return values, exceptions and
  argument mutation are compared. Network calls, file writes and database
  queries are invisible. Each library (`requests`, `sqlalchemy`, `pathlib`)
  is a separate, independent piece of work.
- **Comparators.** Some types need care: numpy arrays want tolerance, floats
  want a policy, datetimes are often incidental.
- **Performance.** Recording currently costs 15–20x. `sys.monitoring`
  (Python 3.12+) should be much cheaper than wrapping functions.
- **Portability.** The per-call timeout uses `SIGALRM`, so Windows is
  unsupported.

## Pull requests

- Add a test. If you are fixing a bug, name the test after the bug.
- Run `pytest` before pushing; CI runs Ubuntu and macOS across Python
  3.9, 3.11 and 3.13.
- Small and focused beats large and complete.
- If you change what the tool reports, say so plainly in the README. The
  README documents the limits honestly and should stay that way.

## Reporting a bug

Most valuable of all: **a false positive.** If `nodrift check` reports a
change on code you did not change, that is a serious bug — please open an
issue with the function and, if you can, a small reproduction.

## Not sure?

Open an issue and ask. A question is a contribution; it usually means the
documentation was unclear.
