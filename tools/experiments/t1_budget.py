"""Does more turn-1 search make a board that survives a CONVERGED kill check?

The budget plateau (STAGE11, 2026-11-18) called our turn 1 converged at 1x:
lost 42 / 42 / 39% at 1 / 2 / 4x. But "lost" there was the 1x kill check,
which misses most kills (38% vs 93% at 16x). Against the 16x check our boards
die 84% of the time going first (STAGE15, 2026-11-19), so the question is
asked again: same positions, same seeds, turn 1 at L x SOLVE_MS, then the
games' kill check (OTK_MS x game.OTK_X, every defence) on the board it made.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/t1_budget.py [positions] [--levels 1,4]
    venv/bin/python tools/experiments/t1_budget.py summary
Resumable: results.jsonl under OUT, one line per (hand, enemy hand, level).
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
sys.path.insert(0, str(ROOT / "tools" / "experiments"))

from skills import db, engine, game, remote, trainer  # noqa: E402
import budget_plateau as bp  # noqa: E402

DECK, ENEMY = "imported", "Angelechy-Meta"
OUT = ROOT / "results" / "experiments" / "t1_budget"
LEVELS = (1, 4)
POSITIONS = 48
_lock = threading.Lock()


def main(limit=POSITIONS, levels=LEVELS):
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
    pos = random.Random(0).sample(pos, min(int(limit), len(pos)))
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["hand"], d["eseed"], d["level"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    jobs = [(h, e, L) for h, e in pos for L in levels if (h["hand_index"], e, L) not in done]
    print(f"{len(pos)} positions x {len(levels)} levels: {len(jobs)} to run on {dev.name}", flush=True)

    def one(job):
        h, e, L = job
        tag = f"h{h['hand_index']}-e{e}-L{L}"
        scratch = OUT / "scratch" / tag
        try:
            goal = trainer.split_goal(json.loads(h["best_goal"]))[0]
            t1 = game.turn1(deck, enemy_deck, json.loads(h["hand"]), goal, e, scratch,
                            engine.SOLVE_MS * L, e, theirs,
                            start=trainer.hand_start(h["best_record"]))
            rec = {"hand": h["hand_index"], "eseed": e, "level": L, "ok": int(t1 is not None),
                   "device": dev.name, "otk_ms": game.OTK_MS * game.OTK_X}
            if t1:
                pieces = game._pieces(t1["board"], 0, cards)
                lost = bp.kill(t1["start"], t1["prefix"], enemy_target,
                               set(pieces) | set(ours_hand), scratch / "kill",
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
            print(f"{job[0]['hand_index']} e{job[1]} L{job[2]}: failed: {type(exc).__name__}: {exc}",
                  flush=True)

    with ThreadPoolExecutor(dev.workers) as ex:
        list(ex.map(guarded, jobs))
    summary()


def summary():
    recs = [json.loads(l) for l in (OUT / "results.jsonl").read_text().splitlines()]
    by = {}
    for d in recs:
        if d.get("ok"):
            by.setdefault((d["hand"], d["eseed"]), {})[d["level"]] = d
    levels = sorted({d["level"] for d in recs})
    paired = [v for v in by.values() if all(L in v for L in levels)]
    print(f"{len(paired)} positions with every level (kill check {recs[0]['otk_ms'] // 1000} s a defence)")
    for L in levels:
        xs = [v[L] for v in paired]
        print(f"  turn 1 at {L}x: killed {sum(x['lost'] for x in xs) / max(1, len(xs)):.0%}, "
              f"pieces {sum(x['pieces'] for x in xs) / max(1, len(xs)):.2f}, "
              f"full goal {sum(x['reached'] for x in xs) / max(1, len(xs)):.0%}")
    if len(levels) >= 2:
        a, b = levels[0], levels[-1]
        saved = sum(1 for v in paired if v[a]["lost"] and not v[b]["lost"])
        lost = sum(1 for v in paired if not v[a]["lost"] and v[b]["lost"])
        print(f"  {a}x -> {b}x: {saved} board(s) now survive, {lost} now die")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        args, levels = [a for a in sys.argv[1:] if not a.startswith("--")], LEVELS
        if "--levels" in sys.argv:
            levels = tuple(int(x) for x in sys.argv[sys.argv.index("--levels") + 1].split(","))
            args = [a for a in args if a != sys.argv[sys.argv.index("--levels") + 1]]
        main(*(args[:1] or [POSITIONS]), levels=levels)
