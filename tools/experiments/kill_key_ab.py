"""Patch 0012 A/B: does the defender taking the enemy's kill keys first save boards?

kill_keys.py: Angelechy's turn-2 kills hinge on Angelechy Enlisted (Kusanagi's
Tuner in 94% of 383) and Destrier (54%), while our defender took the highest-
ATK target (S:P banished Enlisted in 48 of 125 uses). With --opp-key the
target pick takes those first.

Both arms on the SAME build (0011 + 0012), 0011 on in both; arm "ATK" without
--opp-key, arm "key" with it. Stored going-first turn-1 positions whose board
holds S:P, Dragostapelia or Lars (a sample), the games' kill check at the
games' budget, same seeds.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/kill_key_ab.py [positions] [x]
    venv/bin/python tools/experiments/kill_key_ab.py summary
Resumable (results.jsonl).
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

ENEMY, DEVICE = "Angelechy-Meta", "duelpc"
BIN = ROOT / "vendor/ygorl/build/combo-solver-0012/bin/combosolver"
OUT = ROOT / "results" / "experiments" / "kill_key_ab"
TARGETERS = {29301450, 69946549, 35103106}     # S:P Little Knight, Dragostapelia, Lars
ARMS = ("ATK", "key")
POSITIONS = 60


def kill(arm, r, ours, enemy_deck, enemy_target, scratch, ms):
    game.NEGATE_LIVE = True
    game.KILL_KEYS_LIVE = arm == "key"
    base = ["--start", str(r["start"]), "--root", str(r["prefix"]), "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2"] + game.own_turn_args(enemy_deck)
    assert ("--opp-key" in base) == (arm == "key"), "the arms differ in --opp-key only"
    for v in game.DEFENCES:
        k = game._solve(r["prefix"], base + game.defence_args(ours, v)
                        + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)],
                        scratch, f"{arm}-h{v}", ms, r["enemy_seed"] * 100, binary=BIN)
        if not (k["solution"] and game._winner(k["solution"]) == 1):
            return False
    return True


def main(limit=POSITIONS, x=None):
    from ygorl.engine.duel import default_cards
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
    todo = []
    for r in rows:
        if not (r["prefix"] and Path(r["prefix"]).exists()):
            continue
        board = game.replay(r["prefix"], until_turn=2).board
        on = {m["code"] for m in board["players"][0]["mzone"]}
        if on & TARGETERS:
            todo.append((r, set(game._pieces(board, 0, cards)) | ours_hand | (on & TARGETERS)))
    todo = random.Random(12).sample(todo, min(int(limit), len(todo)))
    ms = game.OTK_MS * int(x or game.OTK_X)
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["hand"], d["eseed"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    lock = threading.Lock()
    print(f"{len(todo)} positions, {ms // 1000} s a defence, on {dev.name}", flush=True)

    def one(job):
        r, ours, arm = job
        scratch = OUT / "scratch" / f"h{r['hand_index']}-e{r['enemy_seed']}-{arm}"
        try:
            lost = kill(arm, r, ours, enemy_deck, enemy_target, scratch, ms)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- resumable
            print(f"h{r['hand_index']} e{r['enemy_seed']} {arm}: {type(exc).__name__}: {exc}", flush=True)
            return
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        rec = {"hand": r["hand_index"], "eseed": r["enemy_seed"], "arm": arm, "lost": int(lost),
               "ms": ms, "device": dev.name}
        with lock, res.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    for arm in ARMS:            # one arm at a time: the switches are module globals
        jobs = [(r, ours, arm) for r, ours in todo if (r["hand_index"], r["enemy_seed"], arm) not in done]
        with ThreadPoolExecutor(1 if dev.name == "server" else dev.workers) as ex:
            list(ex.map(one, jobs))
    summary()


def summary():
    f = OUT / "results.jsonl"
    if not f.exists():
        print("no results yet")
        return
    by = {}
    for d in map(json.loads, f.read_text().splitlines()):
        by.setdefault((d["hand"], d["eseed"]), {})[d["arm"]] = d
    pairs = [v for v in by.values() if all(a in v for a in ARMS)]
    n = max(1, len(pairs))
    a = sum(v["ATK"]["lost"] for v in pairs)
    b = sum(v["key"]["lost"] for v in pairs)
    saved = sum(1 for v in pairs if v["ATK"]["lost"] and not v["key"]["lost"])
    worse = sum(1 for v in pairs if v["key"]["lost"] and not v["ATK"]["lost"])
    print(f"{len(pairs)} positions: killed by ATK pick {a} ({a / n:.0%}), key pick {b} ({b / n:.0%}); "
          f"saved {saved}, newly lost {worse}")
    if saved + worse:
        from scipy.stats import binomtest
        print(f"  sign p = {binomtest(saved, saved + worse).pvalue:.3f}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*sys.argv[1:3])
