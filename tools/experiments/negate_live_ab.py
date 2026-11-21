"""Patch 0011 A/B: do our negate-only pieces waiting for a live target save boards?

Traced 2026-11-21: on Angelechy's turn 2, Evolzar Lars negated a Normal Summoned
Mulcharmy Purulia (no field effect) in 5 of its 12 uses and Dragostapelia put
its counter on a hand-trap body in 10 of 73, all lost games. Patch 0011 holds
those pieces while every face-up card of the attacker is such a body
(`game.NEGATE_ONLY`, `game.field_dead`).

Same stored going-first turn-1 positions (the board holds Lars or
Dragostapelia), the games' kill check (every DEFENCES script, a loss needs all
beaten), same seeds; arm "old" = the live binary, "new" = the 0011 build.

Two steps (2026-11-21: at 1x = 20 s the kill check killed NONE of the first 4
positions in either arm -- the games' defender holds the trainer's marked
interrupters too, a set Super Poly, and only the games' 16x attacker beats them
-- so a 1x A/B cannot show a save):
    screen     the new arm at 1x: in which positions does a negate wait at all?
               (holds = 0: the two arms play the same, nothing to compare)
    ab [x]     both arms at x (default game.OTK_X, the games' budget) on the
               screened positions with holds > 0
    summary
Resumable (screen.jsonl, results.jsonl). DUELROOM_DEVICE=duelpc to run there.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, trainer  # noqa: E402

ENEMY, DEVICE = "Angelechy-Meta", "duelpc"
NEW_BIN = ROOT / "vendor/ygorl/build/combo-solver-0011/bin/combosolver"
OUT = ROOT / "results" / "experiments" / "negate_live_ab"
PIECES = set(game.NEGATE_ONLY)


def kill(arm, t1, ours, enemy_deck, enemy_target, scratch, ms):
    # NEGATE_LIVE is a module global: the arms never run side by side
    game.NEGATE_LIVE = arm == "new"
    binary = NEW_BIN if arm == "new" else game.ROOTED_BIN
    base = ["--start", str(t1["start"]), "--root", str(t1["prefix"]), "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2"] + game.own_turn_args(enemy_deck)
    lost, holds = True, 0
    for v in game.DEFENCES:
        k = game._solve(t1["prefix"], base + game.defence_args(ours, v)
                        + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)],
                        scratch, f"{arm}-h{v}", ms, t1["enemy_seed"] * 100, binary=binary)
        text = Path(k["log"]).read_text(errors="replace")
        holds += sum(int(l.split("defender:")[1].split()[0]) for l in text.splitlines()
                     if "negate(s) held" in l)
        if not (k["solution"] and game._winner(k["solution"]) == 1):
            lost = False
    return lost, holds


def main(mode="screen", x=None):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ygorl.engine.duel import default_cards

    from skills import remote
    dev = remote.current()
    remote.prepare(dev)
    conn = db.connect()
    cards = default_cards()
    deck, enemy_deck = engine.DECKS / "imported.ydk", engine.DECKS / f"{ENEMY}.ydk"
    dh = trainer.deck_hash(deck)
    enemy_target = game.scenarios.target_for(ENEMY)
    ours_hand = set(game.respond_codes(deck, cards, extra=trainer.prefs(conn, "imported")["interrupters"]))
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 AND device=? AND ok=1 "
        "ORDER BY hand_index, enemy_seed", (dh, ENEMY, DEVICE))]
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / ("screen.jsonl" if mode == "screen" else "results.jsonl")
    done = ({(d["hand"], d["eseed"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    ms = game.OTK_MS * (1 if mode == "screen" else int(x or game.OTK_X))
    arms = ("new",) if mode == "screen" else ("old", "new")
    keep = None
    if mode != "screen":
        scr = OUT / "screen.jsonl"
        keep = ({(d["hand"], d["eseed"]) for d in map(json.loads, scr.read_text().splitlines())
                 if d["holds"]} if scr.exists() else set())
    todo = []
    for r in rows:
        if not (r["prefix"] and Path(r["prefix"]).exists()):
            continue
        if keep is not None and (r["hand_index"], r["enemy_seed"]) not in keep:
            continue
        board = game.replay(r["prefix"], until_turn=2).board
        pieces = set(game._pieces(board, 0, cards))
        on = {m["code"] for m in board["players"][0]["mzone"]}
        if on & PIECES:
            todo.append((r, pieces | ours_hand | (on & PIECES)))
    lock = threading.Lock()
    print(f"{mode}: {len(todo)} positions, {ms // 1000} s a defence, on {dev.name}", flush=True)

    def one(job):
        r, ours, arm = job
        t1 = {"start": r["start"], "prefix": r["prefix"], "enemy_seed": r["enemy_seed"]}
        scratch = OUT / "scratch" / f"h{r['hand_index']}-e{r['enemy_seed']}-{arm}-{mode}"
        try:
            lost, holds = kill(arm, t1, ours, enemy_deck, enemy_target, scratch, ms)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- resumable: retried on the next run
            print(f"h{r['hand_index']} e{r['enemy_seed']} {arm}: {type(exc).__name__}: {exc}", flush=True)
            return
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        rec = {"hand": r["hand_index"], "eseed": r["enemy_seed"], "arm": arm, "lost": int(lost),
               "holds": holds, "ms": ms, "device": dev.name}
        with lock, res.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    for arm in arms:      # one arm at a time: NEGATE_LIVE is global
        jobs = [(r, ours, arm) for r, ours in todo if (r["hand_index"], r["enemy_seed"], arm) not in done]
        with ThreadPoolExecutor(1 if dev.name == "server" else dev.workers) as ex:
            list(ex.map(one, jobs))
    summary()


def summary():
    s = OUT / "screen.jsonl"
    if s.exists():
        recs = [json.loads(l) for l in s.read_text().splitlines()]
        print(f"screen: {len(recs)} positions, a negate waited in {sum(1 for d in recs if d['holds'])}")
    f = OUT / "results.jsonl"
    if not f.exists():
        print("no results yet")
        return
    by = {}
    for d in map(json.loads, f.read_text().splitlines()):
        by.setdefault((d["hand"], d["eseed"]), {})[d["arm"]] = d
    pairs = [v for v in by.values() if "old" in v and "new" in v]
    n = max(1, len(pairs))
    old = sum(v["old"]["lost"] for v in pairs)
    new = sum(v["new"]["lost"] for v in pairs)
    saved = sum(1 for v in pairs if v["old"]["lost"] and not v["new"]["lost"])
    worse = sum(1 for v in pairs if v["new"]["lost"] and not v["old"]["lost"])
    held = sum(1 for v in pairs if v["new"]["holds"])
    print(f"{len(pairs)} positions: killed old {old} ({old / n:.0%}), new {new} ({new / n:.0%}); "
          f"saved {saved}, newly lost {worse}; a negate held in {held}")
    if saved + worse:
        from scipy.stats import binomtest
        print(f"  sign p = {binomtest(saved, saved + worse).pvalue:.3f}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*(sys.argv[1:3] or ["screen"]))
