"""Defender A/B, turn 3 (patch 0004): with the ENEMY defending smarter, how
often do you still find lethal?

Positions: stored going-first games that reached turn 3 (`game_results`,
outcome win / no_win), each with its `run<k>/t2/{start,prefix}.yrpX`. Each
arm runs the same turn-3 lethal search the games ran (`game.turn3`, the
same seed), differing only in the enemy defender's flags / binary:

  A  greedy   the live rooted binary, patch 0002's defender (the games' arm)
  D  hold     the new binary, --opp-smart + --opp-hold (the games' new default)

    venv/bin/python tools/experiments/defender_bench_t3.py [deck] [enemy] [n_win] [n_no]

Writes results.jsonl under OUT (resumable) and prints the paired summary.
"""

import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import engine, game, scenarios, trainer  # noqa: E402

OUT = Path("/tmp/claude-1000/-home-deezer/beb19f01-23ee-46b2-b103-b63615d8323f"
           "/scratchpad/defbench_t3")
SMART_BIN = engine.VENDOR / "build" / "combo-solver-smart" / "bin" / "combosolver"
ARMS = {"A": (game.ROOTED_BIN, False), "D": (SMART_BIN, True)}


def positions(conn, dh, enemy, n_win, n_no):
    out = []
    for outcome, n in (("win", n_win), ("no_win", n_no)):
        rows = conn.execute(
            "SELECT r.*, h.best_goal FROM game_results r JOIN trainer_hands h "
            "ON h.deck_hash=r.deck_hash AND h.hand_index=r.hand_index "
            "WHERE r.deck_hash=? AND r.enemy=? AND r.first=1 AND r.outcome=? "
            "ORDER BY r.hand_index, r.enemy_seed, r.run", (dh, enemy, outcome)).fetchall()
        # every 7th: spread over hands and enemy hands
        out += [dict(r) for r in rows[::7]][:n]
    return out


def run_arm(g, arm, deck, enemy, theirs, cards):
    binary, smart = ARMS[arm]
    d = game.games_dir(deck, enemy, True) / f"h{g['hand_index']}" / \
        f"e{g['enemy_seed']}" / f"run{g['run']}"
    start, prefix = d / "t2" / "start.yrpX", d / "t2" / "prefix.yrpX"
    goal = trainer.split_goal(json.loads(g["best_goal"]))[0]
    respond = set(theirs) | set(game._pieces(game.replay(prefix).board, 1, cards))
    scratch = OUT / f"h{g['hand_index']}e{g['enemy_seed']}r{g['run']}" / arm
    was = game.DEFENDER
    game.DEFENDER = "smart" if smart else "greedy"
    t0 = time.time()
    try:
        r = game.turn3(start, prefix, goal[0], respond, scratch, engine.SOLVE_MS,
                       g["enemy_seed"] * 100 + g["run"], binary=binary)
    finally:
        game.DEFENDER = was
    return {"won": r["won"], "damage": r["damage"], "s": round(time.time() - t0, 1)}


def main():
    from ygorl.engine.duel import default_cards

    deck = sys.argv[1] if len(sys.argv) > 1 else "imported"
    enemy = sys.argv[2] if len(sys.argv) > 2 else "Angelechy-Meta"
    n_win = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    n_no = int(sys.argv[4]) if len(sys.argv) > 4 else 20
    conn = sqlite3.connect(ROOT / "data" / "duelroom.db")
    conn.row_factory = sqlite3.Row
    cards = default_cards()
    dh = trainer.deck_hash(ROOT / "assets" / "decks" / f"{deck}.ydk")
    theirs = game.enemy_respond_codes(ROOT / "assets" / "decks" / f"{enemy}.ydk")
    rows = positions(conn, dh, enemy, n_win, n_no)
    rows = [g for g in rows if (game.games_dir(deck, enemy, True) / f"h{g['hand_index']}"
                                / f"e{g['enemy_seed']}" / f"run{g['run']}" / "t2"
                                / "prefix.yrpX").exists()]
    OUT.mkdir(parents=True, exist_ok=True)
    res_path = OUT / "results.jsonl"
    done = {}
    if res_path.exists():
        for ln in res_path.read_text().splitlines():
            d = json.loads(ln)
            done[(d["h"], d["e"], d["r"], d["arm"])] = d
    print(f"{len(rows)} positions, {len(done)} results stored", flush=True)
    jobs = [(i, a) for i in range(len(rows)) for a in ARMS
            if (rows[i]["hand_index"], rows[i]["enemy_seed"], rows[i]["run"], a) not in done]

    def one(job):
        i, a = job
        g = rows[i]
        try:
            r = run_arm(g, a, deck, enemy, theirs, cards)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! h{g['hand_index']} e{g['enemy_seed']} r{g['run']} {a}: {exc}", flush=True)
            return
        d = {"h": g["hand_index"], "e": g["enemy_seed"], "r": g["run"], "arm": a,
             "stored": g["outcome"], **r}
        with open(res_path, "a") as f:
            f.write(json.dumps(d) + "\n")
        done[(d["h"], d["e"], d["r"], a)] = d
        print(f"  h{d['h']:>2} e{d['e']} r{d['r']} {a}: {'WIN' if r['won'] else 'no '} "
              f"dmg={r['damage']} {r['s']}s (stored {g['outcome']})", flush=True)

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, jobs))

    full = [g for g in rows if all((g["hand_index"], g["enemy_seed"], g["run"], a) in done
                                   for a in ARMS)]
    print(f"\n{len(full)} positions with both arms")
    for a in ARMS:
        got = [done[(g["hand_index"], g["enemy_seed"], g["run"], a)] for g in full]
        w = sum(x["won"] for x in got)
        print(f"  arm {a}: {w}/{len(got)} lethal ({w / max(1, len(got)):.0%}), "
              f"{sum(x['s'] for x in got) / max(1, len(got)):.1f} s/search")
    lost = sum(1 for g in full if done[(g["hand_index"], g["enemy_seed"], g["run"], "A")]["won"]
               and not done[(g["hand_index"], g["enemy_seed"], g["run"], "D")]["won"])
    gained = sum(1 for g in full if not done[(g["hand_index"], g["enemy_seed"], g["run"], "A")]["won"]
                 and done[(g["hand_index"], g["enemy_seed"], g["run"], "D")]["won"])
    print(f"  A lethal but not D: {lost}; D lethal but not A: {gained}")


if __name__ == "__main__":
    main()
