"""Does a SEARCHED defender stop what the scripts could not? (Stage 14 step 3)

defender_family.py: no family of fixed scripts stopped more converged
attacker wins than today's two. Here the defender searches its replies on
the attacker's own line (game.searched_defence: branch, re-search, learn a
rule, the attacker re-plans), on the plateau's positions and seeds
(budget_plateau.plan, seed e*100+50+rep), at LEVEL x the games' budget:

    kill   their kill through our turn-1 board (OTK_MS x L), we defend
    brk    our break through their turn-1 board (SOLVE_MS x L), they defend

The summary pairs each run with the plateau's scripted result at the same
level and seed (the DEFENCES family, "beat every script"): how many attacker
wins the searched defender takes away, how many it gives (it starts from one
script, not the family, and every extra search is one more chance for the
attacker to miss), and what it costs.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/searched_defender.py [stage ...]
    venv/bin/python tools/experiments/searched_defender.py summary
Resumable: results.jsonl under OUT, one line per (stage, position, seed).
"""

import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, remote, trainer  # noqa: E402

sys.path.insert(0, str(ROOT / "tools" / "experiments"))
import budget_plateau as bp  # noqa: E402

OUT = ROOT / "results" / "experiments" / "searched_defender"
LEVEL = {"kill": 4, "brk": 4}
_lock = threading.Lock()


def _key(d):
    return (d["stage"], d["hand"], d["eseed"], d["rep"], d["level"])


def jobs(conn, stages=tuple(LEVEL)):
    dh = trainer.deck_hash(ROOT / "assets" / "decks" / f"{bp.DECK}.ydk")
    return [(st, r, rep) for st, r, rep in bp.plan(conn, dh) if st in stages]


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

    def task(stage, r, rep):
        h, e, L = r["hand_index"], r["enemy_seed"], LEVEL[stage]
        if (stage, h, e, rep, L) in done:
            return
        seed = e * 100 + 50 + rep          # the plateau's seeds
        scratch = OUT / "scratch" / f"{stage}-h{h}-e{e}-r{rep}"
        pieces = set(json.loads(r["pieces"] or "[]"))
        t0 = time.time()
        try:
            if stage == "kill":
                out = game.searched_defence(r["start"], r["prefix"], enemy_target, 1, 2,
                                            pieces | set(ours_hand), scratch,
                                            game.OTK_MS * L, seed)
            else:
                out = game.searched_defence(r["start"], r["prefix"],
                                            game.steer(goals[h], enemy_target), 1, 2,
                                            set(theirs) | pieces, scratch,
                                            engine.SOLVE_MS * L, seed)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        rec = {"stage": stage, "first": r["first"], "hand": h, "eseed": e, "rep": rep,
               "level": L, "attacker_won": int(out["won"]), "why": out["why"],
               "rules": out["rules"], "searches": out["searches"],
               "secs": round(time.time() - t0), "device": dev.name}
        with _lock, res_file.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    def guarded(job):
        try:
            task(*job)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- reported, retried on the next run
            print(f"{job[0]} h{job[1]['hand_index']} e{job[1]['enemy_seed']} r{job[2]}: "
                  f"failed: {type(exc).__name__}: {exc}", flush=True)

    todo = jobs(conn, stages)
    print(f"{len(todo)} positions on {dev.name} ({dev.workers} at once), "
          f"{len(done)} already done", flush=True)
    with ThreadPoolExecutor(dev.workers) as ex:
        list(ex.map(guarded, todo))
    summary()


def progress():
    """For the Jobs page: positions done per stage."""
    f = OUT / "results.jsonl"
    keys = {(st, r["hand_index"], r["enemy_seed"], rep, LEVEL[st])
            for st, r, rep in jobs(db.connect())}
    done = {_key(d) for d in map(json.loads, f.read_text().splitlines())} if f.exists() else set()
    parts = [(st, sum(k in done for k in keys if k[0] == st), sum(k[0] == st for k in keys))
             for st in LEVEL]
    return {"done": len(keys & done), "total": len(keys), "parts": parts}


def scripted():
    """The plateau's results: (stage, hand, eseed, rep) -> {level: attacker won}."""
    out = {}
    for f in sorted((ROOT / "results" / "experiments" / "budget_plateau").glob("results*.jsonl")):
        for d in map(json.loads, f.read_text().splitlines()):
            k = (d["stage"], d["hand"], d["eseed"], d["rep"])
            for lv, v in d["levels"].items():
                won = v.get("lost") if d["stage"] == "kill" else v.get("won")
                if won is not None:
                    out.setdefault(k, {})[int(lv)] = int(won)
    return out


def summary():
    recs = [json.loads(l) for l in (OUT / "results.jsonl").read_text().splitlines()]
    base = scripted()
    for stage, what in (("kill", "their kill through our turn-1 board"),
                        ("brk", "our break through their turn-1 board")):
        rs = [d for d in recs if d["stage"] == stage]
        if not rs:
            continue
        L = rs[0]["level"]
        pair = [(d, base.get((stage, d["hand"], d["eseed"], d["rep"]), {}).get(L)) for d in rs]
        pair = [(d, s) for d, s in pair if s is not None]
        n = len(pair)
        print(f"\n{stage}: {what} at {L}x, attacker wins ({n} runs paired with the plateau)")
        print(f"  scripted family {'+'.join(map(str, game.DEFENCES))}: "
              f"{sum(s for _, s in pair) / max(1, n):.0%}")
        print(f"  searched:        {sum(d['attacker_won'] for d, _ in pair) / max(1, n):.0%}")
        both = sum(d["attacker_won"] and s for d, s in pair)
        print(f"  beats both:      {both / max(1, n):.0%}")
        saved = sum(s and not d["attacker_won"] for d, s in pair)
        given = sum(d["attacker_won"] and not s for d, s in pair)
        print(f"  searched takes away {saved} of the scripted wins, gives {given}")
        why = {}
        for d in rs:
            why[d["why"]] = why.get(d["why"], 0) + 1
        print("  why: " + ", ".join(f"{k} {v}" for k, v in sorted(why.items())))
        print(f"  cost: {sum(d['searches'] for d in rs) / len(rs):.1f} searches, "
              f"{sum(d['secs'] for d in rs) / len(rs) / 60:.1f} min per position")
        pos = {}
        for d in rs:
            pos.setdefault((d["hand"], d["eseed"]), []).append(d["attacker_won"])
        pairs = [x for x in pos.values() if len(x) >= 2]
        if pairs:
            print(f"  seeds agree: {sum(len(set(x)) == 1 for x in pairs) / len(pairs):.0%} "
                  f"({len(pairs)} positions)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        summary()
    else:
        main(*sys.argv[1:])
