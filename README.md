# nodrift

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

**It is not better than your tests at finding bugs.** Measured on `packaging`:
its test suite caught 57 of 61 injected mutations, `nodrift` caught 50. Use it
to check that a change is *inert*, not to hunt for bugs.

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
| Recorded inputs | 184,592 | 15,830 |
| Functions covered | 408 | 141 |
| **False positives on identical code** | **0** | **0** |

Zero false alarms is the property the tool lives or dies by, and it holds on
both.

On 61 injected mutations in `packaging`, `nodrift` found one real bug that all
**62,424** of its tests miss: an error message reporting `has invalid data` for
a field that was merely unrecognised.

## Contributing

Issues and pull requests are welcome. The most useful contributions right now:

- **Side-effect capture** — intercept `requests`, `sqlalchemy`, `open()`
- **Comparators** for types that need tolerance, e.g. numpy arrays
- **Windows support** — replace the `SIGALRM` timeout
- **Framework adapters** beyond pytest

Each is self-contained; you do not need to understand the whole engine.

```bash
git clone https://github.com/LuShadowX/nodrift
cd nodrift
uv venv && uv pip install -e ".[dev]"
pytest
```

## Licence

MIT
