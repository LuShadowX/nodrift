"""Compare two replay result sets and report behavioural differences."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict


def compare(path_a: str, path_b: str, path_a2: str | None = None) -> dict:
    """Compare two replay runs.

    If `path_a2` is given it is a second run of the *same* code as `path_a`.
    Any record that disagrees with itself across those two runs is inherently
    nondeterministic (embedded ids, clocks, randomness) and is quarantined
    rather than reported, since no claim about it can be made either way.
    """
    with open(path_a) as fh:
        a = json.load(fh)
    with open(path_b) as fh:
        b = json.load(fh)

    quarantined: set[str] = set()
    if path_a2:
        with open(path_a2) as fh:
            a2 = json.load(fh)
        quarantined = {k for k in set(a) | set(a2) if a.get(k) != a2.get(k)}

    keys = (set(a) | set(b)) - quarantined
    per_target: dict[str, dict[str, int]] = defaultdict(
        lambda: {"same": 0, "differs": 0}
    )
    examples: dict[str, list] = defaultdict(list)

    for key in sorted(keys):
        target = key.rsplit("#", 1)[0]
        va, vb = a.get(key), b.get(key)
        if va == vb:
            per_target[target]["same"] += 1
        else:
            per_target[target]["differs"] += 1
            if len(examples[target]) < 3:
                examples[target].append({"key": key, "a": va, "b": vb})

    changed = {t: c for t, c in per_target.items() if c["differs"]}
    total_records = len(keys)
    total_differs = sum(c["differs"] for c in per_target.values())

    return {
        "total_records": total_records,
        "total_differs": total_differs,
        "total_targets": len(per_target),
        "changed_targets": len(changed),
        "quarantined": len(quarantined),
        "quarantined_targets": sorted({k.rsplit("#", 1)[0] for k in quarantined}),
        "changed": changed,
        "examples": {t: examples[t] for t in changed},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_a")
    parser.add_argument("results_b")
    parser.add_argument("--show-examples", action="store_true")
    args = parser.parse_args()

    report = compare(args.results_a, args.results_b)
    print(
        f"records={report['total_records']} differing={report['total_differs']} "
        f"targets={report['total_targets']} changed_targets={report['changed_targets']}"
    )
    for target, counts in sorted(
        report["changed"].items(), key=lambda kv: -kv[1]["differs"]
    ):
        print(f"  {target}: {counts['differs']} differing / "
              f"{counts['same'] + counts['differs']} records")
        if args.show_examples:
            for ex in report["examples"][target]:
                print(f"      A: {json.dumps(ex['a'])[:220]}")
                print(f"      B: {json.dumps(ex['b'])[:220]}")


if __name__ == "__main__":
    main()
