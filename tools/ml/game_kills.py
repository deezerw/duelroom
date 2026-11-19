"""The richer kill-check label, read back from the games already played.

Every stored game going first ran the enemy's kill check against your turn-1
board and kept its solver log and its line (`run<k>/t2-otk.log` for the first
defence, `t2-otk-h<v>.log` for the others). `board_data.kill_detail` reads the
same things for new boards; this fills them in for the old games, no search:
time to the first win, wins, states searched, LP taken, the enemy cards
played, ours that fired, what we stopped.

One board per hand, as Part 2.2 found: this is data for a hand / position
value, not for picking between boards (that is board_data's).

    venv/bin/python tools/ml/game_kills.py [workers]
    venv/bin/python tools/ml/game_kills.py summary
Resumable: results/ml/game_kills.jsonl, one line per game.
"""

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "ml"))

from skills import db, game  # noqa: E402

OUT = ROOT / "results" / "ml" / "game_kills.jsonl"
KEY = ("deck_hash", "enemy", "hand_index", "board", "enemy_seed", "device", "run")


def games(conn):
    """Every stored going-first game, with its deck's name and turn 1's prefix."""
    return [dict(r) for r in conn.execute(
        "SELECT r.deck_hash, t.deck, r.enemy, r.hand_index, r.board, r.enemy_seed, "
        "r.device, r.run, r.outcome, r.created, g.prefix "
        "FROM game_results r "
        "JOIN (SELECT DISTINCT deck_hash, deck FROM trainer_hands) t USING (deck_hash) "
        "JOIN game_turn1 g ON g.deck_hash=r.deck_hash AND g.enemy=r.enemy "
        "AND g.hand_index=r.hand_index AND g.board=r.board AND g.enemy_seed=r.enemy_seed "
        "AND g.first=r.first AND g.device=r.device "
        "WHERE r.first=1 AND r.outcome != 'no_t1' AND g.ok=1")]


def label(g):
    import board_data

    run = (game.games_dir(g["deck"], g["enemy"], True, g["device"])
           / f"h{g['hand_index']}" / f"e{g['enemy_seed']}" / f"run{g['run']}")
    out = {k: g[k] for k in KEY} | {"outcome": g["outcome"], "created": g["created"]}
    kills = []
    for log in sorted(run.glob("t2-otk*.log")):
        tag = log.stem[len("t2-otk"):]          # "" = the first defence, "-h2" ...
        d = run / f"out-t2-otk{tag}"
        sols = sorted(d.glob("solution_*.yrp"))
        appr = sorted(d.glob("best_approach*.yrp"))
        k = {"log": log, "solution": sols[0] if sols else None,
             "approach": appr[0] if appr else None}
        lost = int(bool(k["solution"]) and game._winner(k["solution"]) == 1)
        defence = int(tag[2:]) if tag else game.DEFENCES[0]
        kills.append(board_data.kill_detail(k, g["prefix"], defence, lost, keep=None))
    out["kill"] = kills
    out["ok"] = int(bool(kills))
    return out


def main(workers=6):
    conn = db.connect()
    done = set()
    if OUT.exists():
        done = {tuple(d[k] for k in KEY) for d in map(json.loads, OUT.read_text().splitlines())}
    todo = [g for g in games(conn) if tuple(g[k] for k in KEY) not in done]
    print(f"{len(todo)} game(s) to label, {len(done)} done", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(int(workers)) as ex, OUT.open("a") as fh:
        for n, rec in enumerate(ex.map(label, todo, chunksize=4), 1):
            fh.write(json.dumps(rec) + "\n")
            if n % 200 == 0:
                fh.flush()
                print(f"{n}/{len(todo)}", flush=True)
    summary()


def summary():
    recs = [json.loads(l) for l in OUT.read_text().splitlines()]
    ok = [d for d in recs if d["ok"]]
    kills = [k for d in ok for k in d["kill"]]
    won = [k for k in kills if k["lost"]]
    held = [k for k in kills if not k["lost"]]
    print(f"{len(recs)} games, {len(ok)} with kill checks on disk, {len(kills)} kill checks")
    if won:
        t = sorted(k["first_win_s"] for k in won if k["first_win_s"] is not None)
        print(f"  killed {len(won)}: first win median {t[len(t) // 2]:.1f} s"
              f" ({len(won) - len(t)} found by the finisher, not the rollouts)" if t else "")
    if held:
        dmg = sorted(k["damage"] for k in held)
        print(f"  held {len(held)}: damage taken median {dmg[len(dmg) // 2]}, "
              f"{sum(1 for x in dmg if x >= 6000)} took 6000+")
    errs = sum(1 for k in kills if k.get("read_error"))
    print(f"  replays unreadable: {errs}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*sys.argv[1:2])
