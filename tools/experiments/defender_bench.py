"""Defender A/B (patch 0004): does the smarter defender survive the enemy's
turn-2 kill search more often than the greedy one?

Positions: every stored going-first turn 1 of a matchup (`game_turn1`,
`first=1`, `ok=1`) -- your real board, their real hand. Each arm runs the
same search the games run first on turn 2 (`--goal-win`, OTK_MS, the same
seed), differing only in the defender flags / binary:

  A  greedy   the live rooted binary, patch 0002's defender (the games' arm)
  B  same     the NEW binary with the SAME flags: must behave like A
  C  smart    --opp-smart: priority order + value-based follow-up targets
  D  hold     C plus --opp-hold on every piece that is not a hand trap
  E, F        D with --opp-hold-until 2 / 3 (the family the game loop can pick from)

    venv/bin/python tools/experiments/defender_bench.py [deck] [enemy] [limit] [arms]

Writes results.jsonl under OUT (resumable) and prints the paired summary.
The "new" binaries are builds of the rooted solver with patch 0004 beside the live
one: `ROOTED_DST=vendor/ygorl/build/combo-solver-smart2 tools/build_rooted_solver.sh`
(the live binary already has 0004 since 2026-11-11; for a greedy-vs-smart A/B the
flags alone differ, so one binary serves both arms).
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
           "/scratchpad/defbench")
SMART_BIN = engine.VENDOR / "build" / "combo-solver-smart" / "bin" / "combosolver"
SMART2_BIN = engine.VENDOR / "build" / "combo-solver-smart2" / "bin" / "combosolver"
# arm: (binary, smart, hold, hold_until)
ARMS = {
    "A": (game.ROOTED_BIN, False, False, 1),
    "B": (SMART_BIN, False, False, 1),
    "C": (SMART_BIN, True, False, 1),
    "D": (SMART_BIN, True, True, 1),
    "E": (SMART2_BIN, True, True, 2),   # hold until they control 2 monsters
    "F": (SMART2_BIN, True, True, 3),
}


def arm_args(respond, cards, smart, hold, hold_until=1):
    codes = game.respond_order(respond, cards)
    args = ["--opp-greedy"]
    if codes:
        args += ["--opp-respond", "|".join(map(str, codes))]
    if smart:
        args.append("--opp-smart")
    if hold:
        h = [c for c in codes if c not in scenarios.HANDTRAPS]
        if h:
            args += ["--opp-hold", "|".join(map(str, h))]
        if hold_until != 1:
            args += ["--opp-hold-until", str(hold_until)]
    return args


def run_arm(t1, arm, enemy_target, ours, cards):
    binary, smart, hold, until = ARMS[arm]
    scratch = OUT / f"h{t1['hand_index']}e{t1['enemy_seed']}" / arm
    args = ["--start", t1["start"], "--root", t1["prefix"], "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2"] \
        + arm_args(ours, cards, smart, hold, until) \
        + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)]
    t0 = time.time()
    r = game._solve(t1["prefix"], args, scratch, "t2-otk", game.OTK_MS,
                    t1["enemy_seed"] * 100, binary=binary)
    lost = bool(r["solution"]) and game._winner(r["solution"]) == 1
    dmg = None
    if not lost and r["approach"]:
        pos = game.replay(r["approach"])
        low = pos.min_lp[0] if pos.min_lp[0] is not None else pos.lp[0]
        dmg = max(0, 8000 - low)
    return {"lost": lost, "damage": dmg, "s": round(time.time() - t0, 1)}


def main():
    from ygorl.engine.duel import default_cards

    deck = sys.argv[1] if len(sys.argv) > 1 else "imported"
    enemy = sys.argv[2] if len(sys.argv) > 2 else "Angelechy-Meta"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    arms = sys.argv[4] if len(sys.argv) > 4 else "ABCD"
    conn = sqlite3.connect(ROOT / "data" / "duelroom.db")
    conn.row_factory = sqlite3.Row
    cards = default_cards()
    deck_path = ROOT / "assets" / "decks" / f"{deck}.ydk"
    dh = trainer.deck_hash(deck_path)
    enemy_target = scenarios.target_for(enemy)
    ours_extra = trainer.prefs(conn, deck)["interrupters"]
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 "
        "AND ok=1 ORDER BY hand_index, enemy_seed", (dh, enemy))]
    if limit:
        rows = rows[:limit]
    OUT.mkdir(parents=True, exist_ok=True)
    res_path = OUT / "results.jsonl"
    done = {}
    if res_path.exists():
        for ln in res_path.read_text().splitlines():
            d = json.loads(ln)
            done[(d["h"], d["e"], d["arm"])] = d
    print(f"{len(rows)} positions, arms {arms}, {len(done)} results stored",
          flush=True)
    jobs = [(i, a) for i in range(len(rows)) for a in arms
            if (rows[i]["hand_index"], rows[i]["enemy_seed"], a) not in done]

    def one(job):
        i, a = job
        t1 = rows[i]
        ours = set(json.loads(t1["pieces"])) | set(ours_extra) | set(scenarios.HANDTRAPS)
        try:
            r = run_arm(t1, a, enemy_target, ours, cards)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! h{t1['hand_index']} e{t1['enemy_seed']} {a}: {exc}", flush=True)
            return
        d = {"h": t1["hand_index"], "e": t1["enemy_seed"], "arm": a, **r}
        with open(res_path, "a") as f:
            f.write(json.dumps(d) + "\n")
        done[(d["h"], d["e"], a)] = d
        print(f"  h{d['h']:>2} e{d['e']} {a}: {'LOST' if r['lost'] else 'ok  '} "
              f"dmg={r['damage']} {r['s']}s", flush=True)

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, jobs))

    # summary, paired on positions that have every arm
    full = [r for r in rows if all((r["hand_index"], r["enemy_seed"], a) in done for a in arms)]
    print(f"\n{len(full)} positions with all arms {arms}")
    for a in arms:
        got = [done[(r["hand_index"], r["enemy_seed"], a)] for r in full]
        lost = sum(g["lost"] for g in got)
        dmg = [g["damage"] for g in got if g["damage"] is not None]
        print(f"  arm {a}: lost {lost}/{len(got)} ({lost / max(1, len(got)):.0%}), "
              f"survivors took {sum(dmg) / max(1, len(dmg)):.0f} dmg mean, "
              f"{sum(g['s'] for g in got) / max(1, len(got)):.1f} s/search")
    if "A" in arms:
        for a in arms.replace("A", ""):
            flips = [(r["hand_index"], r["enemy_seed"]) for r in full
                     if done[(r["hand_index"], r["enemy_seed"], "A")]["lost"]
                     != done[(r["hand_index"], r["enemy_seed"], a)]["lost"]]
            saved = sum(1 for h, e in flips if done[(h, e, "A")]["lost"])
            print(f"  A vs {a}: {saved} saved, {len(flips) - saved} newly lost")


if __name__ == "__main__":
    main()
