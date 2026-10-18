#!/usr/bin/env python
"""Break the code on purpose and check that something notices.

Twice tonight a check that looked right was hollow. The optimizer test
passed against the exact bug it was written to catch, because its
fixture made the correct and incorrect behaviours pick the same answer.
The invariant checker's first version flagged a pattern that was
expected and would have been permanently red.

A green suite is not evidence. The only evidence is that the suite goes
red when the code is wrong.

Each mutation below reverses a bug this project actually had. If a
mutation SURVIVES — everything still passes — then that behaviour is
not really covered, whatever the test names suggest.

    venv/bin/python tools/mutation_test.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, find, replace, what bug this re-creates)
MUTATIONS = [
    ("skills/scenarios.py",
     '                if st == "no_window":\n                    continue\n',
     "",
     "count a handtrap with no legal window as a tested window"),

    ("skills/scenarios.py",
     '                if f.get("survives"):',
     '                if st == "solved":',
     'read a fire entry\'s status as survival (status means the SEARCH ran)'),

    ("skills/scenarios.py",
     "                1 if code in extras.get(deck, ()) else 0,  # always makeable",
     "                0,",
     "ignore extra-deck membership when ranking targets"),

    ("skills/scenarios.py",
     "    kept = {k: v for k, v in existing.items() if k not in targets}",
     "    kept = {}",
     "clobber user-deck targets on a meta refresh"),

    ("skills/optimizer.py",
     "                if score > best.score and (improved is None\n"
     "                                           or score > improved.score):",
     "                if score > best.score:",
     "accept the last improving swap rather than the best"),

    ("skills/optimizer.py",
     "    out.sort(key=lambda t: pct.get(t[0], 0.0))",
     "",
     "cut an arbitrary card instead of the least-played one"),

    ("skills/config.py",
     "        share = 1.0 / len(tags)",
     "        share = 1.0",
     "count hybrid decks once per tag, inflating hybrid archetypes"),

    ("skills/deck.py",
     '        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]',
     '        return hashlib.sha256(str(sorted(set(self.main))).encode()).hexdigest()[:16]',
     "ignore card multiplicity in the deck hash"),

    ("skills/stats.py",
     "    if k > K or k > n or (n - k) > (N - K) or k < 0:",
     "    if k > K or k < 0:",
     "drop hypergeometric bounds checks"),

    ("skills/scenarios.py",
     "def derive_targets(jsonl_path, per_deck=1, min_hits=1):",
     "def derive_targets(jsonl_path, per_deck=2, min_hits=1):",
     "emit two targets per deck, which funnel_eval treats as a conjunction"),

    ("skills/scenarios.py",
     "        targets = spec.get(\"targets\") or []\n"
     "        rank = (_zone_rank(targets[0]) if targets\n"
     "                else spec.get(\"_zone_rank\", 3))",
     "        rank = spec.get(\"_zone_rank\", 3)",
     "read zone rank from a stripped key, so saved specs never flag"),

    ("skills/scenarios.py",
     "    exclude = set(exclude or ())",
     "    exclude = set()",
     "ignore the exclusion set in the handtrap ranking"),

    ("skills/meta_scrape.py",
     "        if len(cards) < 3:          # too thin a core to identify anything",
     "        if False:",
     "identify an archetype from a one or two card core"),

    # Both boundaries, not one. Dropping only the left still blocks "Ra"
    # inside "Dragon" because the right boundary sees the following "g",
    # so a one-sided mutation survives without the behaviour being
    # untested — the mutation was wrong, not the test.
    ("skills/meta_scrape.py",
     "    return re.compile(left + body + right, re.IGNORECASE)",
     "    return re.compile(body, re.IGNORECASE)",
     'plain substring tag matching, so "Ra" matches inside "Dragon"'),

    ("skills/builder.py",
     "        lo = stats.wilson(round(pct * n), n)[1]",
     "        lo = pct",
     "rank card search on the raw inclusion rate, so 100% of two lists "
     "outranks 85% of forty"),

    ("skills/builder.py",
     "            0 if (p and p[\"own\"]) else 1,   # the deck's own archetype first",
     "            0,",
     "ignore the deck's own archetype when ranking the card search"),

    ("skills/builder.py",
     "    if name_low == q:\n        return 0\n    return 1 if q in name_low else 2",
     "    if name_low == q:\n        return 0\n    if name_low.startswith(q):\n"
     "        return 1\n    return 2 if q in name_low else 3",
     "tier search hits by prefix, burying meta cards under alphabetical ones"),

    ("skills/carddb.py",
     "    out = sorted(babel.glob(\"release-*.cdb\"))",
     "    out = []",
     "stop merging recent set databases into the card db"),
]


def run_suite():
    r = subprocess.run(
        [str(ROOT / "venv/bin/pytest"), "tests/", "-q", "--no-header", "-x"],
        capture_output=True, text=True, cwd=str(ROOT))
    return r.returncode == 0


def main():
    if not run_suite():
        print("the suite is already failing — fix that before mutating")
        return 2

    survived, killed = [], 0
    for rel, find, repl, what in MUTATIONS:
        path = ROOT / rel
        original = path.read_text()
        if find not in original:
            survived.append((rel, what, "PATTERN NOT FOUND — mutation is stale"))
            continue
        path.write_text(original.replace(find, repl, 1))
        try:
            still_green = run_suite()
        finally:
            path.write_text(original)
        if still_green:
            survived.append((rel, what, "survived — nothing caught it"))
        else:
            killed += 1
            print(f"  killed   {what}")

    print()
    if survived:
        print(f"{len(survived)} mutation(s) SURVIVED out of {len(MUTATIONS)}:\n")
        for rel, what, why in survived:
            print(f"  ! {rel}: {what}")
            print(f"    {why}")
        print("\nA surviving mutation means that behaviour is not covered,")
        print("whatever the test names say. Add a test that distinguishes it.")
        return 1
    print(f"all {killed} mutations killed — the suite notices each of these bugs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
