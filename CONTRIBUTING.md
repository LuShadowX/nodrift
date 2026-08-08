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

66 tests, about ten seconds. If they pass, you are ready.

## How the pieces fit

Seven files, each with one job:

| File | Job |
|---|---|
| `recorder.py` | Wraps your package's functions, pickles the arguments they receive |
| `plugin.py` | The pytest hook that turns recording on |
| `replay.py` | Feeds recorded inputs back into one version of the code |
| `sideeffects.py` | Watches the files a call writes, so writes count as behaviour |
| `fingerprint.py` | Reduces any Python value to something comparable across processes |
| `compare.py` | Diffs two replay runs, quarantines anything nondeterministic |
| `cli.py` | The `record` and `check` commands |

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

- **Side-effect capture.** Return values, exceptions, argument mutation and
  file writes are compared. Network calls and database queries are still
  invisible. Each library (`requests`, `sqlalchemy`) is a separate,
  independent piece of work.
- **Comparators.** Some types need care. Two are settled: numpy arrays
  compare by shape, dtype and contents with no tolerance, and datetimes and
  UUIDs are quarantined rather than normalised (issue #12). Reopening either
  means naming the real change the new policy would hide.
- **Performance.** Recording currently costs ~4x. Note that `sys.monitoring`
  (Python 3.12+) is *not* the lever it looks like: profiling shows function
  interception costs about 1% of the overhead, and serialisation the rest.
- **Portability.** Windows is supported and tested in CI. Where `SIGALRM`
  does not exist the per-call timeout falls back to a watchdog thread, which
  cannot interrupt a call blocked inside C code.

## Pull requests

- Add a test. If you are fixing a bug, name the test after the bug.
- Run `pytest` before pushing; CI runs Ubuntu, macOS and Windows across
  Python 3.9, 3.11 and 3.13.
- Small and focused beats large and complete.
- If you change what the tool reports, say so plainly in the README. The
  README documents the limits honestly and should stay that way.

## Using AI to contribute

**Using AI is not wrong, and it is not discouraged here.** Plenty of good code
gets written with it. What matters is the same thing that has always mattered:
you understood the change, you tested it, and you can defend it in review.

Two things are asked of you:

**1. Say so.** If a patch was substantially written by an AI tool or agent,
mention it in the pull request description. One line is enough — *"drafted with
Claude / Copilot / Cursor"*. Nobody will think less of you for it. It simply
tells the reviewer what to look at more carefully, and undisclosed AI work that
turns out to be wrong costs far more trust than disclosed work ever could.

**2. Use it carefully.** Read the code you are submitting. Run the tests.
Check that it does what the issue actually asked for. AI is very good at
producing changes that look right and are subtly wrong — this project exists
precisely because that class of bug is hard to catch by reading.

### What will be closed

Fully autonomous agents opening pull requests with no human who has read the
change, and low-effort patches submitted without being run, will be closed
without detailed review. Not because a machine wrote them, but because review
time is the scarce resource here and an unread patch spends it without
offering anything back.

If you are unsure whether your contribution qualifies, it almost certainly
does — just disclose it and say what you verified.

## Reporting a bug

Most valuable of all: **a false positive.** If `nodrift check` reports a
change on code you did not change, that is a serious bug — please open an
issue with the function and, if you can, a small reproduction.

## Not sure?

Open an issue and ask. A question is a contribution; it usually means the
documentation was unclear.
