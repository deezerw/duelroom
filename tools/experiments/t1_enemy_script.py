"""On OUR turn 1, should the enemy's hand traps wait for a better target?

On our turn 1 the enemy has no board, only hand traps, and its defender
script fires them at the first eligible window (`game.DEFENCES` hold only
board pieces, so on turn 1 every DEFENCES entry is the same script). The
`game.SCRIPTS` "all1" / "all2" make the hand traps wait until we control
1 / 2 monsters -- measured only on their turn 2 and our break (STAGE11), never
here, where Angelechy's 22 on-our-turn cards act. If a waiting script leaves
us a worse board, the enemy should get to choose it (as it chooses among
DEFENCES on its own turn).

Same positions, same seeds, current method (patch 0009 etc.); per arm our
turn 1 solved against that script, then the games' converged kill check:

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/t1_enemy_script.py [positions]
    venv/bin/python tools/experiments/t1_enemy_script.py summary
    (T1ES_WORKERS=1 T1ES_SUFFIX=_server: one game at a time in its own folder -- the
     server's 2 cores the trainer leaves idle, 2026-11-20)
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

from skills import db, engine, game, remote, trainer  # noqa: E402

DECK, ENEMY = "imported", "Angelechy-Meta"
import os
# one results folder per machine: a paired experiment never pairs across devices
OUT = ROOT / "results" / "experiments" / ("t1_enemy_script" + os.environ.get("T1ES_SUFFIX", ""))
WORKERS = int(os.environ.get("T1ES_WORKERS", "0")) or None
ARMS = ("first", "all1", "all2")      # "first" = the games' script (hand traps fire at once)
POSITIONS = 40
_lock = threading.Lock()


def script_args(theirs, arm, cards):
    if arm == "first":
        return game.defender_args(theirs, cards)
    return game.defender_args(theirs, cards, **game.SCRIPTS[arm])


def kill(start, prefix, enemy_target, ours, enemy_deck, scratch, otk_ms, seed):
    """turn2's kill check alone (lost iff every defence is beaten)."""
    base = ["--start", str(start), "--root", str(prefix), "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2"] \
        + game.own_turn_args(enemy_deck)
    for v in game.DEFENCES:
        k = game._solve(prefix, base + game.defence_args(ours, v)
                        + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)],
                        scratch, f"otk-h{v}", otk_ms, seed)
        if not (k["solution"] and game._winner(k["solution"]) == 1):
            return False
    return True


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
    pos = random.Random(1).sample(pos, min(int(limit), len(pos)))
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["hand"], d["eseed"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    jobs = [(h, e, arm) for h, e in pos for arm in ARMS if (h["hand_index"], e, arm) not in done]
    print(f"{len(pos)} positions x {len(ARMS)} arms: {len(jobs)} to run on {dev.name}", flush=True)

    def one(job):
        h, e, arm = job
        scratch = OUT / "scratch" / f"h{h['hand_index']}-e{e}-{arm}"
        try:
            goal = trainer.split_goal(json.loads(h["best_goal"]))[0]
            # turn1's own defender flags are replaced by the arm's script
            t1 = game.turn1(deck, enemy_deck, json.loads(h["hand"]), goal, e, scratch,
                            engine.SOLVE_MS, e, [], start=trainer.hand_start(h["best_record"]),
                            extra=script_args(theirs, arm, cards))
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

    with ThreadPoolExecutor(WORKERS or dev.workers) as ex:
        list(ex.map(guarded, jobs))
    summary()


def summary():
    f = OUT / "results.jsonl"
    if not f.exists():
        print("no results yet")
        return
    recs = [json.loads(l) for l in f.read_text().splitlines()]
    by = {}
    for d in recs:
        if d.get("ok"):
            by.setdefault((d["hand"], d["eseed"]), {})[d["arm"]] = d
    paired = [v for v in by.values() if all(a in v for a in ARMS)]
    print(f"{len(paired)} positions with every arm (kill check {recs[0]['otk_ms'] // 1000} s a defence)")
    for arm in ARMS:
        xs = [v[arm] for v in paired]
        n = max(1, len(xs))
        print(f"  enemy {arm:5}: our board killed {sum(x['lost'] for x in xs) / n:.0%}, "
              f"pieces {sum(x['pieces'] for x in xs) / n:.2f}, full goal "
              f"{sum(x['reached'] for x in xs) / n:.0%}")
    fam = sum(1 for v in paired if any(v[a]["lost"] for a in ARMS))
    print(f"  enemy picks its best script per position: killed {fam / max(1, len(paired)):.0%}")
    from scipy.stats import binomtest
    for arm in ARMS[1:]:
        worse = sum(1 for v in paired if v[arm]["lost"] and not v["first"]["lost"])
        better = sum(1 for v in paired if not v[arm]["lost"] and v["first"]["lost"])
        p = binomtest(worse, worse + better).pvalue if worse + better else 1.0
        print(f"  {arm} vs first: {worse} board(s) newly killed, {better} saved (sign p = {p:.3f})")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*(sys.argv[1:2] or [POSITIONS]))
