# nodrift

[![PyPI](https://img.shields.io/pypi/v/nodrift)](https://pypi.org/project/nodrift/)
[![Python](https://img.shields.io/pypi/pyversions/nodrift)](https://pypi.org/project/nodrift/)
[![CI](https://github.com/LuShadowX/nodrift/actions/workflows/ci.yml/badge.svg)](https://github.com/LuShadowX/nodrift/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Prove a refactor changed nothing — by running it, not by reading it.**

You changed 200 lines. Your tests pass. Did anything *actually* change?

Tests check what someone remembered to check. `nodrift` compares everything
observable: return values, exceptions raised, and whether arguments were
mutated — using the real inputs your test suite already produces.

No model reviews the code. The verdict comes from execution.

```
$ nodrift record --package mypkg
[nodrift] recording 505 callables in mypkg
[nodrift] 18402 distinct inputs across 408 functions -> .nodrift/recording.pkl

$ nodrift check HEAD~1

  3 of 18402 recorded inputs behave differently (1 function)

    mypkg.dates:parse
        3 of 47 inputs differ
        before: ["raise", ["exception", "ValueError", "invalid date"]]
        after:  ["return", ["object", "datetime.date", ...]]
```

That last block is the point. Not *"this looks risky"* — **here is the input,
here is the difference.**

## Install

```bash
pip install nodrift
```

Requires Python 3.9+. Unix only for now (the per-call timeout uses `SIGALRM`).

## Use

**1. Record** — run your test suite once. `nodrift` watches, and writes down
the real arguments every function receives.

```bash
nodrift record --package mypkg
```

**2. Check** — replay those inputs against an older commit and against your
working tree, then compare.

```bash
nodrift check HEAD~1
```

Exit code is `0` if nothing changed, `1` if something did — so it drops
straight into CI.

If your package lives under `src/`, pass `--subdir src`.

## How it works

1. **Record.** A pytest plugin wraps every function in your package and
   pickles the arguments it receives. Deduplicated, capped per function.
2. **Replay.** Both versions are exported to the *same* directory path, one
   after the other, and every recorded input is replayed against each.
3. **Compare.** Outcomes are reduced to a structural fingerprint — stable
   across processes, order-independent for sets and dicts, with memory
   addresses scrubbed — and compared.

Two details that matter more than they sound:

- **The baseline is replayed twice.** Anything that disagrees with *itself*
  is nondeterministic — a clock, a random seed, an `id()` in a `repr` — and is
  quarantined rather than reported. Silence beats a false alarm.
- **Both versions run from the same path.** Code that embeds its own file path
  in output would otherwise look changed when it isn't. Holding seeds constant
  is not enough; the environment is part of the input.

## What it does *not* do

Being direct, because a tool like this is only worth having if you trust it.

**It is not better than your tests at finding bugs.** Measured across 118
injected mutations in two libraries, `nodrift` found exactly one defect their
test suites missed. Use it to check that a change is *inert*, not to hunt for
bugs.

**It only sees what your tests already run.** No coverage there, no signal
here.

**Some functions cannot be recorded at all.** Code that passes large object
graphs around — parsers, AST walkers, tree transformers — produces arguments
too big to capture. On `sqlparse`, 40 core functions were skipped for this
reason. `nodrift` reports them as *not covered* rather than pretending
otherwise, but the gap is real.

**Side effects are not captured yet.** Network calls, file writes and database
queries are invisible. Only return values, exceptions and argument mutation are
compared.

**Recording is slow.** Expect a 15–20x slowdown on the recorded run. Tests that
assert on wall-clock time will fail while recording.

## Measured behaviour

Two libraries, chosen as opposites — one with an exceptional test suite, one
with an ordinary one.

| | `packaging` | `sqlparse` |
|---|---|---|
| Test suite | 62,424 tests | 494 tests |
| Recorded inputs | 184,592 | 15,830 |
| Functions covered | 408 | 141 |
| **False positives on identical code** | **0** | **0** |
| Injected mutations caught by `nodrift` | 50 / 61 | 52 / 57 |
| Injected mutations caught by the test suite | 57 / 61 | 52 / 57 |

Zero false alarms is the property the tool lives or dies by, and it holds on
both.

On `sqlparse` the two are exactly tied, and `nodrift` missed nothing the tests
caught — it matched a real suite's sensitivity without anyone writing the
assertions. On `packaging`, whose suite is far stronger than most, it trailed;
it did find one bug all 62,424 tests miss (an error reporting `has invalid
data` for a field that was merely unrecognised), but one find in 118 mutations
is not a bug-hunting tool. It is a safety net.

## Contributing

Contributions are very welcome, and the work is deliberately easy to pick up —
most of it touches one file and needs one test.

Start with the [good first issues][gfi]. The broad areas that need help:

- **Side-effect capture** — intercept `requests`, `sqlalchemy`, `open()`
- **Comparators** for types that need tolerance, e.g. numpy arrays
- **Performance** — recording currently costs 15-20x
- **Windows support** — replace the `SIGALRM` timeout
- **Framework adapters** beyond pytest

The most valuable bug report of all is a **false positive**: if `nodrift check`
reports a change on code you did not change, please open an issue.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and how the pieces fit
together.

[gfi]: https://github.com/LuShadowX/nodrift/labels/good%20first%20issue

```bash
git clone https://github.com/LuShadowX/nodrift
cd nodrift
uv venv && uv pip install -e ".[dev]"
pytest
```

## Licence

MIT
