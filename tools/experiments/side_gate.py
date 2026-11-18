"""Stage 13 gate: does the side harness measure a blank as a blank and a
breaker as a breaker? Nobody reads a side result before this passes.

  (a) going first:  -3 Ash +3 Alexandrite Dragon (a vanilla) vs the main deck
      -> must NOT come out as a gain (interval above 0 = harness wrong)
  (b) going second: -3 Ash +3 Lightning Storm vs -3 Ash +3 Alexandrite Dragon
      -> must come out as a clear gain (Storm clears Angelechy's board)

Same hands, enemy hands, seeds; runs 0..1. Resumable (games are stored).

    [DUELROOM_DEVICE=...] venv/bin/python tools/experiments/side_gate.py [run|summary]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, game, remote, side  # noqa: E402

BASE, ENEMY = "imported", "Angelechy-Meta"
ASH, VANILLA, STORM = 14558127, 43096270, 14532163
ENEMY_HANDS = 10


def configs(conn):
    blank = side.make(conn, BASE, [(ASH, VANILLA, 3)])
    storm = side.make(conn, BASE, [(ASH, STORM, 3)])
    return blank, storm


def main():
    conn = db.connect()
    blank, storm = configs(conn)
    for c in (blank, storm):
        print(f"{c['name']}: {c['hands']} hands, {c['hands_touched']} touched, "
              f"{c['goals_changed']} goals changed", flush=True)
    device = remote.current().name
    # The main deck's own games are part of (a): runs 0..1 of the same
    # pairings, re-played where they are missing (2026-11-18: the going-first
    # games of hands whose goal opens "@hand" were deleted, turn 3 was broken).
    base = {"name": BASE, "path": side.deck_file(BASE)}
    for cfg, first in ((base, True), (blank, True), (blank, False), (storm, False)):
        game.run(conn, cfg["path"], cfg["name"], ENEMY, enemy_hands=ENEMY_HANDS,
                 runs=side.RUNS, first=first, device=device)
    summary()


def progress():
    """For the Jobs page: games played per pass (hands x enemy hands x runs each)."""
    from skills import trainer
    conn = db.connect()
    hands = conn.execute("SELECT COUNT(*) FROM trainer_hands WHERE deck_hash=? AND "
                         "best_goal IS NOT NULL",
                         (trainer.deck_hash(side.deck_file(BASE)),)).fetchone()[0]
    per = hands * ENEMY_HANDS * side.RUNS
    main_n = conn.execute(
        "SELECT COUNT(*) FROM game_results WHERE deck_hash=? AND enemy=? AND first=1 "
        "AND enemy_seed >= 1000 AND enemy_seed < ? AND run < ?",
        (trainer.deck_hash(side.deck_file(BASE)), ENEMY, 1000 + ENEMY_HANDS,
         side.RUNS)).fetchone()[0]
    parts = [("main deck, going first", main_n, per)]
    for label, swap, first in (("blank, going first", VANILLA, 1),
                               ("blank, going second", VANILLA, 0),
                               ("Storm, going second", STORM, 0)):
        path = side.deck_file(side.config_name(BASE, [(ASH, swap, 3)]))
        n = 0
        if path.exists():
            n = conn.execute(
                "SELECT COUNT(*) FROM game_results WHERE deck_hash=? AND enemy=? AND first=? "
                "AND enemy_seed >= 1000 AND enemy_seed < ? AND run < ?",
                (trainer.deck_hash(path), ENEMY, first, 1000 + ENEMY_HANDS,
                 side.RUNS)).fetchone()[0]
        parts.append((label, n, per))
    return {"done": sum(p[1] for p in parts), "total": per * len(parts), "parts": parts}


def summary():
    conn = db.connect()
    blank, storm = configs(conn)
    device = remote.current().name
    for label, a, b, first in (("(a) blank vs main, going first", BASE, blank["name"], True),
                               ("(b) Storm vs blank, going second", blank["name"], storm["name"], False)):
        c = side.compare(conn, a, b, ENEMY, first=first, device=device)
        if not c:
            print(f"{label}: nothing to compare yet")
            continue
        p, lo, hi = c["win"]
        print(f"{label}: {c['pairings']} pairings, win {c['base_win']:.0%} -> {c['side_win']:.0%}, "
              f"change {p:+.1%} (95% {lo:+.1%} to {hi:+.1%}) -> {c['verdict']}")
        if "lost_t2" in c:
            p, lo, hi = c["lost_t2"]
            print(f"    killed on turn 2 change {p:+.1%} (95% {lo:+.1%} to {hi:+.1%})")
    print("gate: (a) must not read 'helps', (b) must read 'helps'")


if __name__ == "__main__":
    summary() if sys.argv[1:2] == ["summary"] else main()
