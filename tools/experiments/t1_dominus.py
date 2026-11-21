"""On OUR turn 1, should Angelechy fire its Dominus traps from the hand?

A Dominus trap fired FROM THE HAND locks its controller's monster effects of
some attributes for the rest of the duel (Impulse: LIGHT/EARTH/WIND -- every
Angelechy monster but Seneschal is LIGHT; Purge: DARK/WATER/FIRE -- Seneschal,
Kusanagi; Spark: all but LIGHT/DARK). On our turn 1 Angelechy has no field, so
the hand is its only way to fire them, and its defender fires every eligible
one at once with no word on that cost. Read 2026-11-21 (371 going-first games):
it did so in 272; we died on their turn 2 in 93% of games with none fired, 88%
with Impulse, 84% with Spark, 72% with Purge -- a hint that the lock costs,
confounded by which hands fire what. This asks it on paired positions.

Arms: "first" = the games' defender; "no-<card>" = the same without that card
(it stays in hand for their turn: Problem's discard, a set for our turn 3).
Per arm our turn 1 solved against that defence, then the games' kill check at
the games' budget (t1_enemy_script.kill: a loss needs every DEFENCES script beaten).

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/t1_dominus.py [positions]
    venv/bin/python tools/experiments/t1_dominus.py summary
Resumable: results.jsonl under OUT, one line per (hand, enemy hand, arm).
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from skills import db, engine, game, remote, trainer  # noqa: E402
from t1_enemy_script import kill  # noqa: E402

DECK, ENEMY = "imported", "Angelechy-Meta"
OUT = ROOT / "results" / "experiments" / "t1_dominus"
DOMINUS = {"impulse": 40366667, "purge": 97045737, "spark": 6325660}
ARMS = ("first", "no-impulse", "no-purge", "no-spark")
POSITIONS = 40
_lock = threading.Lock()


def main(limit=POSITIONS):
    from ygorl.engine.duel import default_cards

    dev = remote.current()
    remote.prepare(dev)
    conn = db.connect()
    cards = default_cards()
    deck, enemy_deck = engine.DECKS / f"{DECK}.ydk", engine.DECKS / f"{ENEMY}.ydk"
    dh = trainer.deck_hash(deck)
    enemy_target = game.scenarios.target_for(ENEMY)
    theirs = game.enemy_respond_codes(enemy_deck, cards)
    ours_hand = game.respond_codes(deck, cards, extra=trainer.prefs(conn, DECK)["interrupters"])
    hands = [dict(r) for r in conn.execute(
        "SELECT hand_index, hand, best_goal, best_record FROM trainer_hands "
        "WHERE deck_hash=? AND best_goal IS NOT NULL ORDER BY hand_index", (dh,))]
    pos = [(h, game.ENEMY_SEED0 + e) for h in hands for e in range(10)]
    pos = random.Random(7).sample(pos, min(int(limit), len(pos)))
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["hand"], d["eseed"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    jobs = [(h, e, arm) for h, e in pos for arm in ARMS if (h["hand_index"], e, arm) not in done]
    print(f"{len(pos)} positions x {len(ARMS)} arms: {len(jobs)} to run on {dev.name}", flush=True)

    def one(job):
        h, e, arm = job
        drop = DOMINUS[arm[3:]] if arm.startswith("no-") else None
        resp = [c for c in theirs if c != drop]
        scratch = OUT / "scratch" / f"h{h['hand_index']}-e{e}-{arm}"
        try:
            goal = trainer.split_goal(json.loads(h["best_goal"]))[0]
            t1 = game.turn1(deck, enemy_deck, json.loads(h["hand"]), goal, e, scratch,
                            engine.SOLVE_MS, e, [], start=trainer.hand_start(h["best_record"]),
                            extra=game.defender_args(resp, cards))
            rec = {"hand": h["hand_index"], "eseed": e, "arm": arm, "ok": int(t1 is not None),
                   "device": dev.name, "otk_ms": game.OTK_MS * game.OTK_X}
            if t1:
                pieces = game._pieces(t1["board"], 0, cards)
                lost = kill(t1["start"], t1["prefix"], enemy_target,
                            set(pieces) | set(ours_hand), enemy_deck, scratch / "kill",
                            game.OTK_MS * game.OTK_X, e * 100)
                rec.update(reached=int(t1["reached"]), pieces=len(pieces), lost=int(lost))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        with _lock, res.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    def guarded(job):
        try:
            one(job)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- retried on the next run
            print(f"{job[0]['hand_index']} e{job[1]} {job[2]}: failed: {type(exc).__name__}: {exc}",
                  flush=True)

    with ThreadPoolExecutor(dev.workers) as ex:
        list(ex.map(guarded, jobs))
    summary()


def summary():
    f = OUT / "results.jsonl"
    if not f.exists():
        print("no results yet")
        return
    by = {}
    for d in map(json.loads, f.read_text().splitlines()):
        if d.get("ok"):
            by.setdefault((d["hand"], d["eseed"]), {})[d["arm"]] = d
    paired = [v for v in by.values() if all(a in v for a in ARMS)]
    print(f"{len(paired)} positions with every arm")
    from scipy.stats import binomtest
    for arm in ARMS:
        xs = [v[arm] for v in paired]
        n = max(1, len(xs))
        line = (f"  {arm:11}: our board killed {sum(x['lost'] for x in xs) / n:.0%}, "
                f"pieces {sum(x['pieces'] for x in xs) / n:.2f}")
        if arm != "first":
            saved = sum(1 for v in paired if v["first"]["lost"] and not v[arm]["lost"])
            worse = sum(1 for v in paired if v[arm]["lost"] and not v["first"]["lost"])
            p = binomtest(worse, worse + saved).pvalue if worse + saved else 1.0
            line += f"; vs first: they kill {worse} more, {saved} fewer (sign p = {p:.3f})"
        print(line)


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*(sys.argv[1:2] or [POSITIONS]))
