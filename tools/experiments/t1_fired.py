"""What did the enemy fire during our stored turn 1s, and did we reach the goal?

Reads every going-first `game_turn1` (ok=1) of a matchup, replays the solver's
turn-1 output (the solution or the best approach) and lists the enemy's
activations (player 1) during that turn, next to `reached` and the pieces.

    venv/bin/python tools/experiments/t1_fired.py [deck] [enemy]
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, game, impact, trainer  # noqa: E402,F401  (game sets YGORL env)


def main(deck="imported", enemy="Angelechy-Meta"):
    conn = db.connect()
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM cards")}
    dh = trainer.deck_hash(ROOT / "assets" / "decks" / f"{deck}.ydk")
    rows = conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 AND ok=1 "
        "ORDER BY hand_index, enemy_seed", (dh, enemy)).fetchall()
    by_fired = Counter()
    reached_by_fired = Counter()
    out = []
    for r in rows:
        t1dir = Path(r["start"]).parent.parent
        files = sorted(Path(t1dir, "out-t1").glob("solution_*.yrp")) or \
            sorted(Path(t1dir, "out-t1").glob("best_approach*.yrp"))
        if not files:
            continue
        used = impact.used_cards(files[0], player=1)
        fired = tuple(sorted(names.get(c, str(c)) for c, t in used.items() if t <= 1))
        by_fired[fired] += 1
        reached_by_fired[fired] += r["reached"]
        out.append({"hand": r["hand_index"], "seed": r["enemy_seed"],
                    "reached": r["reached"], "fired": list(fired),
                    "pieces": [names.get(c, c) for c in json.loads(r["pieces"] or "[]")]})
    print(f"{len(out)} turn 1s")
    for fired, n in by_fired.most_common():
        print(f"  {n:3d}  reached {reached_by_fired[fired]:3d}  {' + '.join(fired) or '(nothing)'}")
    Path(sys.argv[3] if len(sys.argv) > 3 else "/dev/null").write_text(
        "\n".join(json.dumps(o) for o in out))


if __name__ == "__main__":
    main(*sys.argv[1:3])
