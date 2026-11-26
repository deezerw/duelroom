"""Turn 1's starter fallbacks: does falling back to a remembered board save us?

User, 2026-11-21: "would it help to let the ai memorize lines? like remember what it
can get from 1 engraver, or 1 engraver plus a black chaos" -> the starter tables
(`daemon.py starters imported 1|2`) and game.turn1_fallbacks: when the enemy breaks
the full goal, turn 1 is re-solved toward the best boards the tables know this hand's
2- and 1-card parts make, and the first board a look-ahead kill check
(FALLBACK_PICK_X, its own seed) leaves alive is kept.

Positions: stored going-first turn 1s that missed the full goal (a sample). Arm
"full" = the stored board (the games' turn 1); arm "fallback" = turn1_fallbacks from
it. Both through the games' kill check at the games' budget and the GAME'S seed
(eseed * 100), paired.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/t1_starters.py [positions] [fallbacks]
    venv/bin/python tools/experiments/t1_starters.py summary
    # every turn 1 (game.T1_PICK_ALWAYS, starter + breadth goals), any enemy / device label:
    venv/bin/python tools/experiments/t1_starters.py 40 6 always blue_eyes_oct_2026 server
    venv/bin/python tools/experiments/t1_starters.py summary results-always.jsonl
Needs the starter tables (starters 1 and 2). Resumable (results.jsonl).
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

DECK, ENEMY, DEVICE = "imported", "Angelechy-Meta", "duelpc"
OUT = ROOT / "results" / "experiments" / "t1_starters"
POSITIONS, FALLBACKS = 40, 2
_lock = threading.Lock()


def main(limit=POSITIONS, n=FALLBACKS, mode="missed", enemy=ENEMY, label=DEVICE):
    """mode "missed": positions that missed the full goal, starter goals only (the
    original question). "always" (game.T1_PICK_ALWAYS): every position, starter +
    breadth goals; results go to results-always.jsonl."""
    always = mode == "always"
    game.T1_PICK_ALWAYS = always
    from ygorl.engine.duel import default_cards
    dev = remote.current()
    remote.prepare(dev)
    conn = db.connect()
    cards = default_cards()
    deck, enemy_deck = engine.DECKS / f"{DECK}.ydk", engine.DECKS / f"{enemy}.ydk"
    dh = trainer.deck_hash(deck)
    if not conn.execute("SELECT COUNT(*) FROM trainer_hands WHERE deck_hash IN (?,?) AND best_goal "
                        "IS NOT NULL", (trainer.starter_key(dh, 1), trainer.starter_key(dh, 2))).fetchone()[0]:
        raise SystemExit("no starter tables yet: daemon.py starters imported 1 / 2")
    enemy_target = game.scenarios.target_for(enemy) or _neutral(enemy, cards)
    theirs = game.enemy_respond_codes(enemy_deck, cards)
    prefs = trainer.prefs(conn, DECK)["interrupters"]
    ours_hand = set(game.respond_codes(deck, cards, extra=prefs))
    hands = {r["hand_index"]: dict(r) for r in conn.execute(
        "SELECT hand_index, hand, best_goal, best_record FROM trainer_hands WHERE deck_hash=?", (dh,))}
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 AND device=? AND ok=1 "
        + ("" if always else "AND reached=0 ") + "ORDER BY hand_index, enemy_seed", (dh, enemy, label))
            if r["prefix"] and Path(r["prefix"]).exists() and r["hand_index"] in hands]
    rows = random.Random(21).sample(rows, min(int(limit), len(rows)))
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / ("results-always.jsonl" if always else "results.jsonl")
    done = ({(d.get("enemy", ENEMY), d["hand"], d["eseed"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    jobs = [r for r in rows if (enemy, r["hand_index"], r["enemy_seed"]) not in done]
    print(f"{enemy}: {len(rows)} positions ({'every turn 1' if always else 'missing the full goal'}), "
          f"{len(jobs)} to run, {n} fallback(s), "
          f"on {dev.name}", flush=True)
    ms = game.OTK_MS * game.OTK_X

    def one(r):
        h = hands[r["hand_index"]]
        hand = json.loads(h["hand"])
        e = r["enemy_seed"]
        scratch = OUT / "scratch" / f"h{r['hand_index']}-e{e}"
        try:
            board = game.replay(r["prefix"], until_turn=2).board
            t1 = {"start": r["start"], "prefix": r["prefix"], "board": board, "reached": bool(r["reached"])}
            fb = game.t1_candidates(conn_local(), dh, deck, hand, json.loads(h["best_goal"]))
            t = game.turn1_fallbacks(t1, fb, deck, enemy_deck, hand, e, scratch / "fb",
                                     engine.SOLVE_MS, e, theirs, enemy_target, ours_hand,
                                     start=trainer.hand_start(h["best_record"]),
                                     sets=game.free_sets(deck, cards, prefs), cards=cards, n=int(n))
            rec = {"enemy": enemy, "reached": int(t1["reached"]), "hand": r["hand_index"], "eseed": e,
                   "fallbacks_known": len(fb),
                   "picked": t.get("fallback", 0), "device": dev.name, "ms": ms}
            for arm, b in (("full", t1), ("fallback", t)):
                if arm == "fallback" and b is t1:
                    rec["fallback_lost"] = rec["full_lost"]     # the same board: the same check
                    continue
                ours = set(game._pieces(b["board"], 0, cards)) | ours_hand
                rec[f"{arm}_lost"] = int(game.kill_check(b["start"], b["prefix"], enemy_target, ours,
                                                         enemy_deck, scratch / arm, ms, e * 100))
                rec[f"{arm}_pieces"] = len(game._pieces(b["board"], 0, cards))
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- resumable
            print(f"h{r['hand_index']} e{e}: {type(exc).__name__}: {exc}", flush=True)
            return
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        with _lock, res.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    with ThreadPoolExecutor(4 if dev.name == "server" else dev.workers) as ex:
        list(ex.map(one, jobs))
    summary()


_tls = threading.local()


def conn_local():
    if not hasattr(_tls, "c"):
        _tls.c = db.connect()       # sqlite3 binds a connection to its thread
    return _tls.c


def _neutral(enemy, cards):
    from skills.deck import Deck
    main = Deck.from_ydk(engine.DECKS / f"{enemy}.ydk").main
    return f"{next(x for x in main if cards.get(x) is not None and cards.get(x).type & 0x2)}@mzone"


def summary(name="results.jsonl"):
    f = OUT / name
    if not f.exists():
        print("no results yet")
        return
    recs = [json.loads(l) for l in f.read_text().splitlines()]
    groups = {}
    for d in recs:                         # per enemy, and (always mode) reached or not
        groups.setdefault(d.get("enemy", ENEMY), []).append(d)
    for enemy, rs in sorted(groups.items()):
        parts = [("all", rs)]
        if any("reached" in d for d in rs):
            parts += [("full goal reached", [d for d in rs if d.get("reached")]),
                      ("full goal missed", [d for d in rs if not d.get("reached")])]
        for label, sub in parts:
            if sub:
                _line(f"{enemy} [{label}]", sub)


def _line(label, recs):
    n = len(recs)
    a = sum(d["full_lost"] for d in recs)
    b = sum(d["fallback_lost"] for d in recs)
    saved = sum(1 for d in recs if d["full_lost"] and not d["fallback_lost"])
    worse = sum(1 for d in recs if d["fallback_lost"] and not d["full_lost"])
    p = ""
    if saved + worse:
        from scipy.stats import binomtest
        p = f", sign p {binomtest(saved, saved + worse).pvalue:.3f}"
    print(f"{label}: {n} positions; killed with the full goal's board {a} ({a / n:.0%}), with the "
          f"pick {b} ({b / n:.0%}); saved {saved}, newly lost {worse}{p}; another board kept in "
          f"{sum(1 for d in recs if d['picked'])}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary(*sys.argv[2:3])
    else:
        main(*sys.argv[1:6])
