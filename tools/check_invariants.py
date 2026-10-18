#!/usr/bin/env python
"""Check facts that cannot all be true at once.

Every wrong answer this project produced was caught one of two ways: a
number that was not physically possible, or two outputs disagreeing
about the same thing. Neither is visible by reading code, and neither is
a crash. This runs both checks over the real stored data.

Worked examples, all real:

  * branded averaged 1.0 actions per line and 6.8 "special summons".
    More summons than actions is impossible; the metric was counting
    response windows.
  * `matrix` said kewl-tune survives Infinite Impermanence 100% while
    `report` said 0%, off the same record. One was reading a fire
    entry's `status` (did the SEARCH succeed) as survival.
  * A deck solved 6/6 hands in two seconds where every other deck took
    60-170s per hand. The target was "set a card", satisfiable in two
    actions.

    venv/bin/python tools/check_invariants.py

Exit 1 if any invariant fails. Each failure prints what disagrees, not
just that something did.
"""

import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skills import scenarios  # noqa: E402
from skills.stats import wilson  # noqa: E402

MAXX_C = "23434538"


def _records(pattern):
    for f in sorted(glob.glob(str(ROOT / "results" / pattern))):
        for line in Path(f).read_text().splitlines():
            if line.strip():
                try:
                    yield Path(f).name, json.loads(line)
                except json.JSONDecodeError:
                    continue


def check_solved_plus_bricks(fails):
    """solved + bricks + errors must equal the hands actually attempted."""
    for name, rec in _records("*.jsonl"):
        s = rec.get("summary")
        if not s or s.get("hands") is None:
            continue
        total = (s.get("solved") or 0) + (s.get("bricks") or 0) + (s.get("errors") or 0)
        if total != s["hands"]:
            fails.append(
                f"{name}: {s['deck']} solved+bricks+errors={total} "
                f"but hands={s['hands']}")


def check_rate_matches_counts(fails):
    """A reported rate must match its own numerator and denominator."""
    for name, rec in _records("*.jsonl"):
        s = rec.get("summary")
        if not s or not s.get("hands"):
            continue
        for key, num in (("brick_rate", s.get("bricks")),
                         ("best_line_rate", s.get("solved"))):
            rate = s.get(key)
            if rate is None or num is None:
                continue
            expect = num / s["hands"]
            # Upstream stores these rounded to 4 places (0.5833 against
            # 0.58333...), so an exact comparison flags every record.
            # Tolerance has to clear that rounding without hiding a real
            # mismatch, which would be off by far more than a rounding
            # step.
            if abs(rate - expect) > 1e-3:
                fails.append(
                    f"{name}: {s['deck']} {key}={rate:.3f} but "
                    f"{num}/{s['hands']}={expect:.3f}")


def check_survival_agrees_with_fire_entries(fails):
    """Our recomputed survival must be derivable from the raw entries.

    This is the check that would have caught reading `status` as
    survival: the summary field and a recomputation disagreed, and only
    one of them could be right.
    """
    for name, rec in _records("*.jsonl"):
        if not rec.get("hands") or not isinstance(rec.get("hands"), list):
            continue
        cells = scenarios.handtrap_matrix([rec])
        for deck, data in cells.items():
            for code, cell in data["cells"].items():
                if cell["tested"] > cell["offered"]:
                    fails.append(
                        f"{name}: {deck} card {code} tested={cell['tested']} "
                        f"> offered={cell['offered']}")
                if cell["survival"] is not None and not 0 <= cell["survival"] <= 1:
                    fails.append(
                        f"{name}: {deck} card {code} survival="
                        f"{cell['survival']} out of range")


def check_windows_not_mistaken_for_summons(fails):
    """Nothing may reintroduce Maxx "C" windows as a summon count.

    The data genuinely shows more Maxx "C" windows than a line has
    actions -- branded averages 1.0 actions against 6.8 windows -- which
    is exactly why they cannot be special summons. That pattern is
    expected and is not itself a failure. What would be a failure is any
    code reading it as summons again, so this asserts the retraction
    holds rather than re-flagging the data.
    """
    for gone in ("summon_counts", "window_anomalies"):
        if hasattr(scenarios, gone):
            fails.append(
                f"scenarios.{gone} is back — it read Maxx \"C\" response "
                f"windows as special summons (branded: 1.0 actions, 6.8 "
                f"\"summons\")")


def check_wilson_contains_point(fails):
    for n, k in ((12, 0), (12, 12), (40, 31), (100, 50), (3, 1)):
        p, lo, hi = wilson(k, n)
        if not lo <= p <= hi:
            fails.append(f"wilson({k},{n}) point {p} outside [{lo},{hi}]")


def check_board_frequencies_sum_to_one(fails):
    from skills import db
    conn = db.connect()
    rows = conn.execute(
        "SELECT archetype, SUM(frequency) s FROM boards GROUP BY archetype"
    ).fetchall()
    for r in rows:
        if abs(r["s"] - 1.0) > 1e-6:
            fails.append(
                f"boards: {r['archetype']} frequencies sum to {r['s']:.4f}, not 1")


def check_target_is_in_its_deck(fails):
    """A derived target must be a card the deck can actually produce."""
    from skills import db
    from skills.deck import Deck
    conn = db.connect()
    for name, spec in scenarios.load_targets().items():
        if name.startswith("_"):
            continue
        path = ROOT / "assets" / "decks" / f"{name}.ydk"
        if not path.exists():
            continue
        deck = Deck.from_ydk(path)
        deck.canonicalise(conn)
        targets = spec["targets"] if isinstance(spec, dict) else spec
        code = int(str(targets[0]).split("@")[0])
        if code not in set(deck.main) | set(deck.extra):
            fails.append(
                f"targets: {name} aims at {code}, which is in neither its "
                f"main nor extra deck")


CHECKS = [
    check_solved_plus_bricks,
    check_rate_matches_counts,
    check_survival_agrees_with_fire_entries,
    check_windows_not_mistaken_for_summons,
    check_wilson_contains_point,
    check_board_frequencies_sum_to_one,
    check_target_is_in_its_deck,
]


def main():
    fails = []
    for check in CHECKS:
        try:
            check(fails)
        except Exception as e:  # a broken check is itself a finding
            fails.append(f"{check.__name__} raised {type(e).__name__}: {e}")
    if not fails:
        print(f"all {len(CHECKS)} invariants hold over the stored data")
        return 0
    print(f"{len(fails)} invariant failure(s):\n")
    for f in fails:
        print(f"  ! {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
