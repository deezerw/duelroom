"""Turn 1 against hand traps: aim for the goal's INTERRUPTIONS only? (paired)

Against Angelechy-Meta our turn 1 reaches its trained goal in 14 of 240
positions: the enemy fires something in 236 of them, and the search then
spends its budget chasing a board it cannot finish. Arm B asks the turn-1
search for the goal's interruption cards only (S:P Little Knight, Sequence,
Desirae, ... -- every code that ever counted as a piece on a stored board),
a smaller board it might complete through the hand traps.

  A  the stored turn 1 (full goal), as the games played it
  B  turn 1 re-solved with the interruption-only goal, same duel and seed

Both boards then face the enemy's turn-2 kill check exactly as the games
run it (`--goal-win`, OTK_MS, the DEFENCES family: lost only if every
defence is beaten), REPS times with paired seeds. Positions whose goal is
all interruptions (B == A) are skipped.

Runs where `DUELROOM_DEVICE` says (skills/remote.py):
    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/t1_backup_bench.py [limit]
Resumable: results.jsonl under OUT.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, game, remote, trainer  # noqa: E402

DECK, ENEMY = "imported", "Angelechy-Meta"
OUT = ROOT / "results" / "experiments" / "t1_backup"
REPS = 3
_lock = threading.Lock()


def kill_check(start, prefix, enemy_target, our_respond, scratch, seed):
    """The first half of game.turn2: lost iff every defence is beaten."""
    base = ["--start", start, "--root", prefix, "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2"]
    for v in game.DEFENCES:
        k = game._solve(prefix, base + game.defender_args(our_respond, hold_until=v)
                        + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)],
                        scratch, f"otk-h{v}", game.OTK_MS, seed)
        if not (k["solution"] and game._winner(k["solution"]) == 1):
            return False
    return True


def main(limit=None):
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
    ours = game.respond_codes(deck, cards, extra=trainer.prefs(conn, DECK)["interrupters"])
    hands = {r["hand_index"]: r for r in conn.execute(
        "SELECT hand_index, hand, best_goal, best_record FROM trainer_hands "
        "WHERE deck_hash=? AND best_goal IS NOT NULL", (dh,))}
    rows = conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 AND "
        "device='server' AND ok=1 ORDER BY enemy_seed, hand_index", (dh, ENEMY)).fetchall()
    pieces = {c for r in rows for c in json.loads(r["pieces"] or "[]")}
    if limit:
        rows = rows[:int(limit)]
    OUT.mkdir(parents=True, exist_ok=True)
    res_file = OUT / "results.jsonl"
    done = {(d["hand"], d["seed"]) for d in map(json.loads, res_file.read_text().splitlines())} \
        if res_file.exists() else set()

    def one(r):
        h, e = r["hand_index"], r["enemy_seed"]
        if (h, e) in done:
            return
        goal = trainer.split_goal(json.loads(hands[h]["best_goal"]))[0]
        goal_b = [t for t in goal if int(str(t).split("@")[0]) in pieces]
        rec = {"hand": h, "seed": e, "goal": goal, "goal_b": goal_b, "device": dev.name}
        if not goal_b or goal_b == goal:
            rec["skip"] = "goal is all interruptions" if goal_b else "no interruption in goal"
        else:
            scratch = OUT / f"h{h}" / f"e{e}"
            t1 = game.turn1(deck, enemy_deck, json.loads(hands[h]["hand"]), goal_b, e,
                            scratch / "t1b", game.engine.SOLVE_MS, e, theirs,
                            start=trainer.hand_start(hands[h]["best_record"]))
            if t1 is None:
                rec["skip"] = "arm B turn 1 returned nothing"
            else:
                rec["b_reached"] = int(t1["reached"])
                rec["a_reached"] = r["reached"]
                rec["a_pieces"] = len(json.loads(r["pieces"] or "[]"))
                rec["b_pieces"] = len(game._pieces(t1["board"], 0, cards))
                rec["a_lost"], rec["b_lost"] = [], []
                for k in range(REPS):
                    rec["a_lost"].append(int(kill_check(r["start"], r["prefix"], enemy_target, ours,
                                                        scratch / f"a{k}", e + 100 * k)))
                    rec["b_lost"].append(int(kill_check(str(t1["start"]), str(t1["prefix"]),
                                                        enemy_target, ours, scratch / f"b{k}",
                                                        e + 100 * k)))
        with _lock, res_file.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"h{h} e{e}: {rec.get('skip') or ('A lost %s  B lost %s  B reached %d' % (rec['a_lost'], rec['b_lost'], rec['b_reached']))}",
              flush=True)

    def guarded(r):
        try:
            one(r)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- reported, retried on the next run
            print(f"h{r['hand_index']} e{r['enemy_seed']}: failed: {exc}", flush=True)

    with ThreadPoolExecutor(dev.workers) as ex:
        list(ex.map(guarded, rows))
    summary(res_file)


def summary(res_file):
    recs = [json.loads(l) for l in res_file.read_text().splitlines()]
    run = [d for d in recs if "a_lost" in d]
    if not run:
        print("nothing compared yet")
        return
    n = len(run) * REPS
    a = sum(sum(d["a_lost"]) for d in run)
    b = sum(sum(d["b_lost"]) for d in run)
    pos_a = sum(1 for d in run if sum(d["a_lost"]) > sum(d["b_lost"]))
    pos_b = sum(1 for d in run if sum(d["b_lost"]) > sum(d["a_lost"]))
    print(f"{len(run)} positions x {REPS} kill checks ({len(recs) - len(run)} skipped)")
    print(f"  lost on turn 2:  A (full goal) {a}/{n} = {a / n:.0%}   "
          f"B (interruptions only) {b}/{n} = {b / n:.0%}")
    print(f"  positions where B lost less: {pos_a}, more: {pos_b}")
    print(f"  goal reached:  A {sum(d['a_reached'] for d in run)}/{len(run)}   "
          f"B {sum(d['b_reached'] for d in run)}/{len(run)}")
    print(f"  interruption pieces on board (mean):  A {sum(d['a_pieces'] for d in run) / len(run):.2f}"
          f"   B {sum(d['b_pieces'] for d in run) / len(run):.2f}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        summary(OUT / "results.jsonl")
    else:
        main(*sys.argv[1:2])
