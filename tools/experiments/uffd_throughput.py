"""How much more search does R2V_ARENA_UFFD=1 buy at the games' real
settings (2 threads, 4 searches at once, 60 s, full budget)? Same stored
turn-3 positions, same seed, both arms interleaved; reads the solver's own
counters (rollout states, finisher expansions) and the outcome.

    venv/bin/python tools/experiments/uffd_throughput.py [positions]
"""

import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import game, trainer  # noqa: E402

OUT = ROOT / "results" / "experiments" / "uffd_throughput"


def counters(log):
    st = sum(int(b) for _a, b in re.findall(r"(\d+) rollouts\s+(\d+) states", log))
    fin = sum(int(x) for x in re.findall(r"^\s+archive \d+ \S+ \S+\s+(\d+) exp\.", log, re.M))
    return st, fin


def main():
    from ygorl.engine.duel import default_cards

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    conn = sqlite3.connect(ROOT / "data" / "duelroom.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT g.*, t.best_goal FROM game_results g JOIN trainer_hands t "
        "ON t.deck_hash=g.deck_hash AND t.hand_index=g.hand_index "
        "WHERE g.first=1 AND g.enemy='angelechy' AND g.outcome IN ('win','no_win') "
        "ORDER BY (g.hand_index * 7919 + g.enemy_seed * 104729 + g.run * 37) % 1013 "
        "LIMIT ?", (n,)).fetchall()
    cards = default_cards()
    theirs = game.enemy_respond_codes(ROOT / "assets" / "decks" / "angelechy.ydk")
    got = {}

    def one(job):
        i, arm = job
        g = rows[i]
        d = (game.GAMES / "imported" / "angelechy" / f"h{g['hand_index']}"
             / f"e{g['enemy_seed']}" / f"run{g['run']}" / "t2")
        goal = trainer.split_goal(json.loads(g["best_goal"]))[0]
        respond = set(theirs) | set(game._pieces(game.replay(d / "prefix.yrpX").board, 1, cards))
        scratch = OUT / f"p{i}" / arm
        r = game.turn3(d / "start.yrpX", d / "prefix.yrpX", game.steer(goal, goal[0]), respond, scratch,
                       game.engine.SOLVE_MS, g["enemy_seed"] * 100 + g["run"],
                       extra=["--stop-on-win", "0"],
                       # game.ARENA_ENV turns uffd on; the control turns it off
                       env_extra={"R2V_ARENA_UFFD": "1" if arm == "uffd" else "0"})
        st, fin = counters((scratch / "t3.log").read_text(errors="replace"))
        got[job] = (r["won"], st, fin)
        if (i, "fault") in got and (i, "uffd") in got:
            a, b = got[(i, "fault")], got[(i, "uffd")]
            print(f"p{i:<2} fault: {'WIN' if a[0] else 'no '} states {a[1]:>8} fin {a[2]:>7}"
                  f" | uffd: {'WIN' if b[0] else 'no '} states {b[1]:>8} fin {b[2]:>7}"
                  f" | states x{b[1] / max(a[1], 1):.2f}", flush=True)

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, [(i, arm) for i in range(len(rows)) for arm in ("fault", "uffd")]))
    for arm in ("fault", "uffd"):
        vals = [got[(i, arm)] for i in range(len(rows))]
        print(f"{arm}: {sum(v[0] for v in vals)}/{len(vals)} wins, "
              f"{sum(v[1] for v in vals)} rollout states, {sum(v[2] for v in vals)} finisher exp.")


if __name__ == "__main__":
    main()
