"""Is each search converged? 1x / 2x / 4x its budget (Stage 11 step 1b).

The user plays real matches from these games, so every search in a game must
be as close to its best as the compute allows: a lazy enemy flatters your
deck as much as a weak pilot under-rates it. Each search is tested alone, the
others at the games' budgets (SOLVE_MS, OTK_MS), on the stored positions:

  going first
    t1     our turn 1 at L          -> their kill check at 1x  (lost on T2?)
    kill   their kill check at L    on the stored turn-1 board  (lost on T2?)
    t3     their turn 2 at 1x       -> our turn 3 at L          (won?)
  going second
    et1    their turn 1 at L        -> our break at 1x          (won?)
    brk    our break at L           on the stored enemy board   (won?)

Each (stage, position) runs SEEDS seeds; within a seed every level uses the
same seed and the same upstream position, so 1x / 2x / 4x are paired. A
search is converged when 2x -> 4x no longer moves its outcome; two seeds
disagreeing at one level says the same thing from the other side.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/budget_plateau.py [positions]
        [--stages kill,brk] [--levels 8,16]   (more levels: own file, merged by summary)
    venv/bin/python tools/experiments/budget_plateau.py summary
Resumable: results.jsonl under OUT, one line per (stage, side, hand, seed, rep).
"""

import json
import random
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, remote, trainer  # noqa: E402

DECK, ENEMY = "imported", "Angelechy-Meta"
OUT = ROOT / "results" / "experiments" / "budget_plateau"
LEVELS = (1, 2, 4)
SEEDS = 2
POSITIONS = 36          # per side
STAGES = {1: ("t1", "kill", "t3"), 0: ("et1", "brk")}
_lock = threading.Lock()


def kill(start, prefix, enemy_target, ours, scratch, otk_ms, seed):
    """turn2's kill check alone: lost iff every defence is beaten."""
    base = ["--start", str(start), "--root", str(prefix), "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2"]
    for v in game.DEFENCES:
        k = game._solve(prefix, base + game.defender_args(ours, hold_until=v)
                        + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)],
                        scratch, f"otk-h{v}", otk_ms, seed)
        if not (k["solution"] and game._winner(k["solution"]) == 1):
            return False
    return True


def plan(conn, dh, n=POSITIONS):
    """Every (stage, stored turn-1 row, rep) this bench runs, seed 0 of every
    stage first: a run cut short still covers every search."""
    pos = {}
    for first in (1, 0):
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=? AND "
            "device='server' AND ok=1 ORDER BY hand_index, enemy_seed", (dh, ENEMY, first))]
        pos[first] = random.Random(first).sample(rows, min(n, len(rows)))
    # Turn 3 only exists when we live through turn 2 (~31% of games): its
    # positions are drawn from those we usually survive in the stored games.
    lives = {(g["hand_index"], g["enemy_seed"]) for g in conn.execute(
        "SELECT hand_index, enemy_seed FROM game_results WHERE deck_hash=? AND enemy=? "
        "AND first=1 AND device='server' GROUP BY hand_index, enemy_seed "
        "HAVING SUM(outcome != 'lost_t2') >= 5", (dh, ENEMY))}
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 AND "
        "device='server' AND ok=1 ORDER BY hand_index, enemy_seed", (dh, ENEMY))
        if (r["hand_index"], r["enemy_seed"]) in lives]
    pos["t3"] = random.Random(3).sample(rows, min(n, len(rows)))

    def rows_for(st):
        return pos["t3"] if st == "t3" else pos[1 if st in STAGES[1] else 0]
    return [(st, rows_for(st)[i], rep) for rep in range(SEEDS) for i in range(n)
            for st in (*STAGES[1], *STAGES[0]) if i < len(rows_for(st))]


def main(limit=None, stages=None, levels=LEVELS):
    from ygorl.engine.duel import default_cards

    dev = remote.current()
    remote.prepare(dev)
    conn = db.connect()
    cards = default_cards()
    deck = ROOT / "assets" / "decks" / f"{DECK}.ydk"
    enemy_deck = ROOT / "assets" / "decks" / f"{ENEMY}.ydk"
    dh = trainer.deck_hash(deck)
    enemy_target = game.scenarios.target_for(ENEMY)
    theirs = game.enemy_respond_codes(enemy_deck, cards)
    ours_hand = game.respond_codes(deck, cards, extra=trainer.prefs(conn, DECK)["interrupters"])
    hands = {r["hand_index"]: dict(r) for r in conn.execute(
        "SELECT hand_index, hand, best_goal, best_record FROM trainer_hands "
        "WHERE deck_hash=? AND best_goal IS NOT NULL", (dh,))}
    n = int(limit) if limit else POSITIONS
    jobs = [j for j in plan(conn, dh, n) if not stages or j[0] in stages]
    OUT.mkdir(parents=True, exist_ok=True)
    res_file = results_file(levels)
    done = set()
    if res_file.exists():
        for d in map(json.loads, res_file.read_text().splitlines()):
            done.add((d["stage"], d["hand"], d["eseed"], d["rep"]))
    sms, oms = engine.SOLVE_MS, game.OTK_MS

    def record(rec):
        with _lock, res_file.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    def task(stage, r, rep):
        h, e = r["hand_index"], r["enemy_seed"]
        if (stage, h, e, rep) in done:
            return
        hrow = hands[h]
        goal = trainer.split_goal(json.loads(hrow["best_goal"]))[0]
        seed = e * 100 + 50 + rep      # stored games used e*100 + 0..9
        scratch = OUT / "scratch" / f"{stage}-h{h}-e{e}-r{rep}"
        rec = {"stage": stage, "first": r["first"], "hand": h, "eseed": e, "rep": rep,
               "device": dev.name, "levels": {}}
        start = trainer.hand_start(hrow["best_record"])
        try:
            if stage == "t1":
                for L in levels:
                    t1 = game.turn1(deck, enemy_deck, json.loads(hrow["hand"]), goal, e,
                                    scratch / f"L{L}", sms * L, seed, theirs, start=start)
                    if t1 is None:
                        rec["levels"][L] = {"ok": 0}
                        continue
                    ours = set(game._pieces(t1["board"], 0, cards)) | set(ours_hand)
                    rec["levels"][L] = {
                        "ok": 1, "reached": int(t1["reached"]),
                        "pieces": len(game._pieces(t1["board"], 0, cards)),
                        "lost": int(kill(t1["start"], t1["prefix"], enemy_target, ours,
                                         scratch / f"L{L}k", oms, seed))}
            elif stage == "kill":
                ours = set(json.loads(r["pieces"] or "[]")) | set(ours_hand)
                for L in levels:
                    rec["levels"][L] = {"lost": int(kill(r["start"], r["prefix"], enemy_target,
                                                         ours, scratch / f"L{L}", oms * L, seed))}
            elif stage == "t3":
                ours = set(json.loads(r["pieces"] or "[]")) | set(ours_hand)
                t2 = game.turn2(r["start"], r["prefix"], enemy_target, ours,
                                scratch / "t2", sms, seed)
                if t2["lost"] or t2.get("won"):
                    rec["upstream"] = "lost_t2" if t2["lost"] else "won_t2"
                else:
                    ep = set(theirs) | set(game._pieces(t2["board"], 1, cards))
                    for L in levels:
                        t3 = game.turn3(t2["start"], t2["prefix"], game.steer(goal, enemy_target),
                                        ep, scratch / f"L{L}", sms * L, seed)
                        rec["levels"][L] = {"won": int(t3["won"]), "damage": t3["damage"]}
            elif stage == "et1":
                for L in levels:
                    t1 = game.enemy_turn1(deck, enemy_deck, json.loads(hrow["hand"]), e,
                                          enemy_target, scratch / f"L{L}", sms * L, seed,
                                          ours_hand, start=start)
                    if t1 is None:
                        rec["levels"][L] = {"ok": 0}
                        continue
                    b = game.turn3(t1["start"], t1["prefix"], game.steer(goal, enemy_target),
                                   set(theirs) | set(game._pieces(t1["board"], 0, cards)),  # turn 1's seat, as game.run
                                   scratch / f"L{L}b", sms, seed, player=1, turn=2)
                    rec["levels"][L] = {"ok": 1, "reached": int(t1["reached"]),
                                        "won": int(b["won"])}
            elif stage == "brk":
                for L in levels:
                    b = game.turn3(r["start"], r["prefix"], game.steer(goal, enemy_target),
                                   set(theirs) | set(json.loads(r["pieces"] or "[]")),
                                   scratch / f"L{L}", sms * L, seed, player=1, turn=2)
                    rec["levels"][L] = {"won": int(b["won"]), "damage": b["damage"]}
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        record(rec)

    def guarded(job):
        try:
            task(*job)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- reported, retried on the next run
            print(f"{job[0]} h{job[1]['hand_index']} e{job[1]['enemy_seed']} r{job[2]}: "
                  f"failed: {type(exc).__name__}: {exc}", flush=True)

    print(f"{len(jobs)} tasks on {dev.name} ({dev.workers} at once), "
          f"{len(done)} already done", flush=True)
    with ThreadPoolExecutor(dev.workers) as ex:
        list(ex.map(guarded, jobs))
    summary()

def results_file(levels=LEVELS):
    """1x/2x/4x in results.jsonl; a run at other levels (`--levels 8,16`) in
    its own file, merged with it by summary()."""
    return OUT / ("results.jsonl" if tuple(levels) == LEVELS else
                  f"results_L{'-'.join(map(str, levels))}.jsonl")


def progress():
    """For the Jobs page: the runs going now (by their files), tasks of the plan done."""
    conn = db.connect()
    keys = [(st, r["hand_index"], r["enemy_seed"], rep) for st, r, rep in
            plan(conn, trainer.deck_hash(ROOT / "assets" / "decks" / f"{DECK}.ydk"))]
    parts, done_all, total = [], 0, 0
    for f in sorted(OUT.glob("results*.jsonl")):
        done = {(d["stage"], d["hand"], d["eseed"], d["rep"])
                for d in map(json.loads, f.read_text().splitlines())}
        tag = "" if f.name == "results.jsonl" else " " + f.stem.split("_L")[1].replace("-", "x/") + "x"
        for st in (*STAGES[1], *STAGES[0]):
            if any(k[0] == st for k in done):
                ks = [k for k in keys if k[0] == st]
                parts.append((st + tag, sum(k in done for k in ks), len(ks)))
                done_all += parts[-1][1]
                total += parts[-1][2]
    return {"done": done_all, "total": total, "parts": parts}


METRIC = {"t1": "lost", "kill": "lost", "t3": "won", "et1": "won", "brk": "won"}
WHAT = {"t1": "our turn 1 -> lost on T2", "kill": "their kill check -> lost on T2",
        "t3": "our turn 3 -> won", "et1": "their turn 1 -> we broke it",
        "brk": "our going-second break -> won"}


def summary(res_file=None):

    files = [Path(res_file)] if res_file else sorted(OUT.glob("results*.jsonl"))
    merged = {}
    for f in files:
        for d in map(json.loads, f.read_text().splitlines()):
            k = (d["stage"], d["hand"], d["eseed"], d["rep"])
            if k in merged:
                merged[k]["levels"].update(d["levels"])
            else:
                merged[k] = d
    recs = list(merged.values())
    for stage, m in METRIC.items():
        levels = sorted({int(L) for d in recs if d["stage"] == stage for L in d["levels"]})
        rs = [d for d in recs if d["stage"] == stage and len(d["levels"]) == len(levels)
              and all(m in d["levels"][str(L)] for L in levels)]
        skipped = sum(1 for d in recs if d["stage"] == stage) - len(rs)
        if not rs:
            continue
        print(f"\n{stage}: {WHAT[stage]}  ({len(rs)} runs, {skipped} without all levels)")
        for L in levels:
            v = [d["levels"][str(L)][m] for d in rs]
            extra = ""
            if "reached" in rs[0]["levels"][str(L)]:
                extra = f"   goal reached {sum(d['levels'][str(L)]['reached'] for d in rs) / len(rs):.0%}"
            print(f"  {L}x: {m} {sum(v) / len(v):.0%}{extra}")
        for a, b in zip(levels, levels[1:]):
            up = sum(1 for d in rs if d["levels"][str(b)][m] > d["levels"][str(a)][m])
            dn = sum(1 for d in rs if d["levels"][str(b)][m] < d["levels"][str(a)][m])
            print(f"  {a}x -> {b}x: {up} runs went to {m}=1, {dn} went to 0")
        # Seed agreement: both seeds of one position, same level.
        by = {}
        for d in rs:
            by.setdefault((d["hand"], d["eseed"]), {})[d["rep"]] = d
        pairs = [p for p in by.values() if len(p) >= 2]
        if pairs:
            agree = {L: sum(1 for p in pairs if len({p[k]["levels"][str(L)][m] for k in p}) == 1)
                     / len(pairs) for L in levels}
            print("  seeds agree: " + "  ".join(f"{L}x {agree[L]:.0%}" for L in levels)
                  + f"  ({len(pairs)} positions)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        summary()
    else:
        args, opt = [], {}
        it = iter(sys.argv[1:])
        for x in it:
            if x in ("--stages", "--levels"):
                opt[x[2:]] = next(it).split(",")
            else:
                args.append(x)
        main(*args[:1], stages=opt.get("stages"),
             levels=tuple(int(L) for L in opt.get("levels", LEVELS)))
