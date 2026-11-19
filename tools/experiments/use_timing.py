"""Best time to use, measured: fire or hold at the SAME window (user's end goal).

skills/timing.py reads which targets your cards happened to hit in the games,
which the solver's own choices confound (S:P "stopping" anything on their
turn read -71: it fires there only when they push). Here every window is a
paired experiment instead. For one stored going-first game per position:

  1. the enemy's turn-2 line of its first kill check (the kill if it killed
     you, its best approach if your board held), as tools/ml/game_kills.py
     read it;
  2. every window on it where one of your pieces could answer
     (patch 0006 --opp-branches): the other reply there -- fire where the
     script passed, pass (or another piece) where it fired;
  3. the enemy's kill search again from each branch, same flags, seed and
     budget (OTK_MS x LEVEL).

At a window the line's own reply has the line's outcome (killed or not) and
the branch's reply has the re-search's. Summary per (your card, their card
it answers): how much less often they kill you when you fire it there than
when you hold it, paired by window.

The script plays the rest of the turn after the branch (one reply changes,
not the whole policy), and the attacker re-plans around it with full search.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/use_timing.py [positions]
    venv/bin/python tools/experiments/use_timing.py summary
Resumable: results.jsonl under OUT, one line per branch.
"""

import json
import shutil
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, remote, trainer  # noqa: E402

DECK, ENEMY = "imported", "Angelechy-Meta"
OUT = ROOT / "results" / "experiments" / "use_timing"
KILLS = ROOT / "results" / "ml" / "game_kills.jsonl"
LEVEL = 8       # the enemy re-search: 8x OTK_MS (16x converges further; the pilot fits a day)
MAX_BRANCHES = 8
_lock = threading.Lock()


def positions(conn, dh):
    """One stored game per (hand, enemy hand): run 0's first-defence kill
    check, with its turn-1 start and prefix."""
    t1 = {(r["hand_index"], r["enemy_seed"]): dict(r) for r in conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 "
        "AND device='server' AND ok=1", (dh, ENEMY))}
    out = []
    for d in map(json.loads, KILLS.read_text().splitlines()):
        if (d["deck_hash"] != dh or d["enemy"] != ENEMY or d["device"] != "server"
                or d["run"] != 0 or not d.get("kill")):
            continue
        k = next((x for x in d["kill"] if x["defence"] == game.DEFENCES[0]), None)
        pos = t1.get((d["hand_index"], d["enemy_seed"]))
        if k and k.get("line") and Path(k["line"]).exists() and pos:
            out.append({**pos, "line": k["line"], "lost": k["lost"], "run": 0})
    return sorted(out, key=lambda p: (p["enemy_seed"], p["hand_index"]))


def distinct(branches):
    """One branch per situation: the same trigger, monsters, reply and
    alternative again later on the line is the same question asked twice."""
    seen, out = set(), []
    for b in branches:
        k = (b["trigger"], b.get("mons"), b["chosen"], b["alt"])
        if k not in seen:
            seen.add(k)
            out.append(b)
    return out


def main(limit=None):
    from ygorl.engine.duel import default_cards

    dev = remote.current()
    remote.prepare(dev)
    conn = db.connect()
    cards = default_cards()
    deck = engine.DECKS / f"{DECK}.ydk"
    dh = trainer.deck_hash(deck)
    enemy_target = game.scenarios.target_for(ENEMY)
    ours_hand = game.respond_codes(deck, cards, extra=trainer.prefs(conn, DECK)["interrupters"])
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = set()
    if res.exists():
        done = {(d["hand"], d["eseed"], d["k"]) for d in map(json.loads, res.read_text().splitlines())}
    todo_pos = positions(conn, dh)[:int(limit) if limit else None]

    jobs = []
    for p in todo_pos:
        respond = set(json.loads(p["pieces"] or "[]")) | set(ours_hand)
        flags = ["--start", p["start"], "--root", p["prefix"], "--no-ref",
                 "--target", enemy_target, "--player", "1", "--turns", "2"] \
            + game.defence_args(respond, game.DEFENCES[0])
        scratch = OUT / "scratch" / f"h{p['hand_index']}-e{p['enemy_seed']}"
        try:
            brs = distinct(game._branches(p["line"], flags, scratch, "br"))[:MAX_BRANCHES]
        except Exception as exc:  # noqa: BLE001 -- that position is skipped, reported
            print(f"h{p['hand_index']} e{p['enemy_seed']}: no branches: {exc}", flush=True)
            continue
        for b in brs:
            if (p["hand_index"], p["enemy_seed"], b["k"]) not in done:
                jobs.append((p, flags, b, scratch))
    print(f"{len(todo_pos)} positions, {len(jobs)} branch(es) to search on {dev.name} "
          f"({dev.workers} at once), {len(done)} done", flush=True)

    def one(job):
        p, flags, b, scratch = job
        seed = p["enemy_seed"] * 100 + p["run"]          # the game's kill check seed
        args = [a if a != p["prefix"] else b["file"] for a in flags] \
            + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)]
        r = game._solve(b["file"], args, scratch, f"b{b['k']}", game.OTK_MS * LEVEL, seed)
        killed = int(bool(r["solution"]) and game._winner(r["solution"]) == 1)
        rec = {"hand": p["hand_index"], "eseed": p["enemy_seed"], "k": b["k"],
               "at": b.get("at"), "trigger": b["trigger"], "mons": b.get("mons"),
               "chosen": b["chosen"], "alt": b["alt"], "line_lost": p["lost"],
               "branch_lost": killed, "level": LEVEL, "device": dev.name}
        with _lock, res.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    def guarded(job):
        try:
            one(job)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- retried on the next run
            print(f"h{job[0]['hand_index']} e{job[0]['enemy_seed']} b{job[2]['k']}: "
                  f"failed: {type(exc).__name__}: {exc}", flush=True)

    with ThreadPoolExecutor(dev.workers) as ex:
        list(ex.map(guarded, jobs))
    for p in todo_pos:
        shutil.rmtree(OUT / "scratch" / f"h{p['hand_index']}-e{p['enemy_seed']}",
                      ignore_errors=True)
    summary()


def summary():
    """Per (your card, their trigger): killed when you fire vs when you hold, by window."""
    from skills import card_db

    conn = db.connect()
    def nm(c, none="pass"):
        return (card_db.resolve(conn, c) or {"name": str(c)})["name"] if c else none
    recs = [json.loads(l) for l in (OUT / "results.jsonl").read_text().splitlines()]
    pair = defaultdict(list)        # (piece, trigger) -> [(killed if fired, killed if held)]
    for d in recs:
        fired = d["chosen"] or d["alt"]          # one of the two replies fires a piece
        if not fired or (d["chosen"] and d["alt"]):
            continue                              # piece vs piece: not fire-or-hold
        lost_fire = d["line_lost"] if d["chosen"] else d["branch_lost"]
        lost_hold = d["branch_lost"] if d["chosen"] else d["line_lost"]
        pair[(fired, d["trigger"])].append((lost_fire, lost_hold))
    print(f"{len(recs)} branch searches; fire vs hold at the same window "
          f"(their kill rate, lower is better for you)")
    rows = []
    for (piece, trig), xs in pair.items():
        f = sum(a for a, _ in xs) / len(xs)
        h = sum(b for _, b in xs) / len(xs)
        rows.append((h - f, piece, trig, len(xs), f, h))
    for gain, piece, trig, n, f, h in sorted(rows, reverse=True):
        print(f"  {nm(piece):32} on {nm(trig, 'an open window'):34} {n:3} windows  "
              f"killed {f:4.0%} if fired, {h:4.0%} if held  -> {gain * 100:+.0f}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*sys.argv[1:2])
