"""A/B: does `--stop-on-win k` (patch 0002 + duelroom) change who wins?

Replays stored going-first turn-3 positions (the t2 start/prefix of real
games) through three arms with the same seed, target and respond list:
  A  --stop-on-win 0: the full budget (needs the patch 0003 binary)
  B  --stop-on-win 4
  C  --stop-on-win 1
and prints per-position outcome and wall time. A win counts only if the
written solution replays to the target player winning (game._winner),
exactly as game.py scores it.

    venv/bin/python tools/experiments/stop_on_win_ab.py [n_wins] [n_no_wins]
"""

import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import game, trainer  # noqa: E402

OUT = ROOT / "results" / "experiments" / "stop_on_win"


def positions(conn, n_win, n_no):
    rows = []
    for outcome, n in (("win", n_win), ("no_win", n_no)):
        rows += conn.execute(
            "SELECT g.*, t.best_goal FROM game_results g JOIN trainer_hands t "
            "ON t.deck_hash=g.deck_hash AND t.hand_index=g.hand_index "
            "WHERE g.first=1 AND g.enemy='angelechy' AND g.outcome=? "
            "ORDER BY (g.hand_index * 7919 + g.enemy_seed * 104729 + g.run * 31) % 1009 "
            "LIMIT ?", (outcome, n)).fetchall()
    return rows


def run_arm(g, arm, k, cards, theirs):
    d = game.GAMES / "imported" / "angelechy" / f"h{g['hand_index']}" / \
        f"e{g['enemy_seed']}" / f"run{g['run']}"
    start, prefix = d / "t2" / "start.yrpX", d / "t2" / "prefix.yrpX"
    goal = trainer.split_goal(json.loads(g["best_goal"]))[0]
    respond = set(theirs) | set(game._pieces(game.replay(prefix).board, 1, cards))
    scratch = OUT / f"h{g['hand_index']}e{g['enemy_seed']}r{g['run']}" / arm
    t0 = time.time()
    r = game.turn3(start, prefix, game.steer(goal, goal[0]), respond, scratch, game.engine.SOLVE_MS,
                   g["enemy_seed"] * 100 + g["run"],
                   # Always stated: game.turn3 passes STOP_ON_WIN itself, and
                   # the solver keeps the LAST value given, so arm A says 0.
                   extra=["--stop-on-win", str(k)])
    log = (scratch / "t3.log").read_text(errors="replace")
    note = next((ln.strip() for ln in log.splitlines() if "stop-on-win" in ln), "")
    return {"won": r["won"], "damage": r["damage"], "s": round(time.time() - t0, 1),
            "note": note}


def main():
    from ygorl.engine.duel import default_cards

    n_win = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    n_no = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    conn = sqlite3.connect(ROOT / "data" / "duelroom.db")
    conn.row_factory = sqlite3.Row
    cards = default_cards()
    theirs = game.enemy_respond_codes(ROOT / "assets" / "decks" / "angelechy.ydk")
    rows = positions(conn, n_win, n_no)
    # Four searches at once, 2 threads each: the load the games run under.
    # The three arms of a position are submitted together so machine load
    # drifts over them alike.
    jobs = [(i, arm, k) for i in range(len(rows))
            for arm, k in (("A", 0), ("B", 4), ("C", 1))]
    got = {}

    def one(job):
        i, arm, k = job
        got[(i, arm)] = run_arm(rows[i], arm, k, cards, theirs)
        if all((i, a) in got for a in "ABC"):
            g, res = rows[i], {a: got[(i, a)] for a in "ABC"}
            print(f"{i + 1:>2}/{len(rows)} h{g['hand_index'] + 1} e{g['enemy_seed'] - 999} "
                  f"r{g['run'] + 1} stored={g['outcome']:<6} "
                  + "  ".join(f"{a}:{'WIN' if r['won'] else 'no '} {r['s']:>5}s"
                              for a, r in res.items())
                  + f"  | B {res['B']['note'][:60]}", flush=True)
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, jobs))
    results = [(dict(rows[i]), {a: got[(i, a)] for a in "ABC"}) for i in range(len(rows))]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=str))
    for arm in "ABC":
        w = sum(r[arm]["won"] for _g, r in results)
        s = sum(r[arm]["s"] for _g, r in results)
        print(f"arm {arm}: {w}/{len(results)} wins, {s / len(results):.1f} s mean")
    flips = [(g["hand_index"], g["enemy_seed"], g["run"]) for g, r in results
             if r["A"]["won"] and not (r["B"]["won"] and r["C"]["won"])]
    print("A won but B or C did not:", flips or "none")


if __name__ == "__main__":
    main()
