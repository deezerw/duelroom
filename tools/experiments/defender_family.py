"""Which defence scripts make the attacker's win a real one? (Stage 11, defender)

The plateau (budget_plateau.py) found the ATTACKER winning more the longer it
searches, on both sides: their kill through our turn-1 board 38% -> 92% lost
at 1x -> 16x, our break through theirs 33% -> 81% won at 1x -> 8x. The
defender is a fixed script, and a fixed script is visible to the attacker's
rollouts. A family of scripts with "the attacker must beat every one" is a
defender that commits to its script before the attacker searches it: still
weaker than a real, adaptive defender, never stronger -- so a bigger family
can only move the number toward the truth (less the search noise of one more
randomized search per script, which agreement across seeds bounds).

On the plateau's own positions and seeds, at the converged-or-highest budget
(kill 16x OTK_MS, brk 8x SOLVE_MS), every candidate script alone:

    game.DEFENCES entries 0..3 (hold board pieces until n monsters) and
    game.SCRIPTS (answer order, hand traps held too)

The summary builds the family greedily: the script that leaves the fewest
attacker wins once it must also beat it, and so on.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/defender_family.py [stage ...]
    venv/bin/python tools/experiments/defender_family.py summary
Resumable: results.jsonl under OUT, one line per (stage, position, seed, script).
"""

import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, remote, trainer  # noqa: E402

sys.path.insert(0, str(ROOT / "tools" / "experiments"))
import budget_plateau as bp  # noqa: E402

OUT = ROOT / "results" / "experiments" / "defender_family"
CANDIDATES = (1, 2, 0, 3, *game.SCRIPTS)
LEVEL = {"kill": 16, "brk": 8}
_lock = threading.Lock()


def _key(d):
    return (d["stage"], d["hand"], d["eseed"], d["rep"], str(d["script"]))


def jobs(conn, stages=tuple(LEVEL)):
    dh = trainer.deck_hash(ROOT / "assets" / "decks" / f"{bp.DECK}.ydk")
    return [(st, r, rep, s) for st, r, rep in bp.plan(conn, dh) if st in stages
            for s in CANDIDATES]


def main(*stages):
    from ygorl.engine.duel import default_cards

    stages = stages or tuple(LEVEL)
    dev = remote.current()
    remote.prepare(dev)
    conn = db.connect()
    cards = default_cards()
    deck = ROOT / "assets" / "decks" / f"{bp.DECK}.ydk"
    enemy_deck = ROOT / "assets" / "decks" / f"{bp.ENEMY}.ydk"
    dh = trainer.deck_hash(deck)
    enemy_target = game.scenarios.target_for(bp.ENEMY)
    theirs = game.enemy_respond_codes(enemy_deck, cards)
    ours_hand = game.respond_codes(deck, cards, extra=trainer.prefs(conn, bp.DECK)["interrupters"])
    goals = {r["hand_index"]: trainer.split_goal(json.loads(r["best_goal"]))[0]
             for r in conn.execute("SELECT hand_index, best_goal FROM trainer_hands "
                                   "WHERE deck_hash=? AND best_goal IS NOT NULL", (dh,))}
    OUT.mkdir(parents=True, exist_ok=True)
    res_file = OUT / "results.jsonl"
    done = ({_key(d) for d in map(json.loads, res_file.read_text().splitlines())}
            if res_file.exists() else set())

    def task(stage, r, rep, script):
        h, e = r["hand_index"], r["enemy_seed"]
        if (stage, h, e, rep, str(script)) in done:
            return
        seed = e * 100 + 50 + rep          # the plateau's seeds
        scratch = OUT / "scratch" / f"{stage}-h{h}-e{e}-r{rep}-{script}"
        L = LEVEL[stage]
        try:
            if stage == "kill":
                ours = set(json.loads(r["pieces"] or "[]")) | set(ours_hand)
                args = ["--start", r["start"], "--root", r["prefix"], "--no-ref",
                        "--target", enemy_target, "--player", "1", "--turns", "2"] \
                    + game.defence_args(ours, script, cards) \
                    + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)]
                k = game._solve(r["prefix"], args, scratch, "otk", game.OTK_MS * L, seed)
                won = bool(k["solution"]) and game._winner(k["solution"]) == 1
            else:
                b = game.turn3(r["start"], r["prefix"], game.steer(goals[h], enemy_target),
                               set(theirs) | set(json.loads(r["pieces"] or "[]")),
                               scratch, engine.SOLVE_MS * L, seed, player=1, turn=2,
                               defences=(script,))
                won = bool(b["won"])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        rec = {"stage": stage, "first": r["first"], "hand": h, "eseed": e, "rep": rep,
               "script": script, "level": L, "attacker_won": int(won), "device": dev.name}
        with _lock, res_file.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    def guarded(job):
        try:
            task(*job)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- reported, retried on the next run
            print(f"{job[0]} h{job[1]['hand_index']} e{job[1]['enemy_seed']} r{job[2]} "
                  f"{job[3]}: failed: {type(exc).__name__}: {exc}", flush=True)

    todo = jobs(conn, stages)
    print(f"{len(todo)} searches on {dev.name} ({dev.workers} at once), "
          f"{len(done)} already done", flush=True)
    with ThreadPoolExecutor(dev.workers) as ex:
        list(ex.map(guarded, todo))
    summary()


def progress():
    """For the Jobs page: searches done per stage."""
    f = OUT / "results.jsonl"
    keys = {(st, r["hand_index"], r["enemy_seed"], rep, str(s))
            for st, r, rep, s in jobs(db.connect())}
    done = {_key(d) for d in map(json.loads, f.read_text().splitlines())} if f.exists() else set()
    parts = [(st, sum(k in done for k in keys if k[0] == st), sum(k[0] == st for k in keys))
             for st in LEVEL]
    return {"done": len(keys & done), "total": len(keys), "parts": parts}


def summary():
    recs = [json.loads(l) for l in (OUT / "results.jsonl").read_text().splitlines()]
    for stage, what in (("kill", "their kill through our turn-1 board"),
                        ("brk", "our break through their turn-1 board")):
        by = {}
        for d in recs:
            if d["stage"] == stage:
                by.setdefault((d["hand"], d["eseed"], d["rep"]), {})[str(d["script"])] = d["attacker_won"]
        scripts = [str(s) for s in CANDIDATES]
        runs = {k: v for k, v in by.items() if all(s in v for s in scripts)}
        if not runs:
            continue
        n = len(runs)
        print(f"\n{stage}: {what} at {LEVEL[stage]}x, attacker wins  ({n} runs with every script, "
              f"{len(by) - n} partial)")
        for s in scripts:
            print(f"  alone {s:>5}: {sum(v[s] for v in runs.values()) / n:.0%}")

        def beats_all(fam):
            return sum(all(v[s] for s in fam) for v in runs.values())
        today = [str(x) for x in game.DEFENCES]
        print(f"  today's family {'+'.join(today)}: {beats_all(today) / n:.0%}")
        fam, left = [], list(scripts)
        while left:
            best = min(left, key=lambda s: (beats_all(fam + [s]), scripts.index(s)))
            fam.append(best)
            left.remove(best)
            print(f"  family +{best:>5} -> {'+'.join(fam)}: {beats_all(fam) / n:.0%}")
        # Seeds of one position agree under the full family? (the noise check)
        pos = {}
        for (h, e, rep), v in runs.items():
            pos.setdefault((h, e), []).append(all(v[s] for s in scripts))
        pairs = [x for x in pos.values() if len(x) >= 2]
        if pairs:
            print(f"  seeds agree under all {len(scripts)}: "
                  f"{sum(len(set(x)) == 1 for x in pairs) / len(pairs):.0%} ({len(pairs)} positions)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        summary()
    else:
        main(*sys.argv[1:])
