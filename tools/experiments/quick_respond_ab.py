"""QUICK_RESPOND A/B: does listing Havnis / Kashtira for the defender save boards?

`game.QUICK_RESPOND` (off) adds our hand's Quick-Effect self-summoners
(`game.quick_codes`: Tearlaments Havnis, Kashtira) to what the defender may play on
the enemy's turn 2 -- the Aqua-to-GY that sets off Kaleido-Heart. They fired 0 / 1
times in 480 games because nothing listed them.

Same stored going-first turn-1 positions whose turn-2 hand holds one of them, the
games' own kill check (`game.kill_check`: DEFENCES, majority of futures) at
game.OTK_X, same seeds; arm "off" = today, "on" = QUICK_RESPOND. Resumable
(results.jsonl). Usage: quick_respond_ab.py [run|summary] [x]. DUELROOM_DEVICE=duelpc
to run there.
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, trainer  # noqa: E402

ENEMY = "Angelechy-Meta"
OUT = ROOT / "results" / "experiments" / "quick_respond_ab"


def positions(conn, cards, deck):
    quick = set(game.quick_codes(deck))
    dh = trainer.deck_hash(deck)
    out = []
    for r in conn.execute("SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 "
                          "AND ok=1 ORDER BY hand_index, enemy_seed", (dh, ENEMY)):
        r = dict(r)
        if not (r["prefix"] and Path(r["prefix"]).exists()):
            continue
        board = game.replay(r["prefix"], until_turn=2).board
        hand = {c["code"] if isinstance(c, dict) else c for c in board["players"][0].get("hand") or []}
        if hand & quick:
            out.append((r, set(game._pieces(board, 0, cards))))
    return out


def main(mode="run", x=None):
    if mode == "summary":
        return summary()
    from ygorl.engine.duel import default_cards
    from skills import remote
    dev = remote.current()
    remote.prepare(dev)
    conn, cards = db.connect(), default_cards()
    deck, enemy_deck = engine.DECKS / "imported.ydk", engine.DECKS / f"{ENEMY}.ydk"
    enemy_target = game.scenarios.target_for(ENEMY)
    extra = trainer.prefs(conn, "imported")["interrupters"]
    todo = positions(conn, cards, deck)
    ms = game.OTK_MS * int(x or game.OTK_X)
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["hand"], d["eseed"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    print(f"{len(todo)} positions hold Havnis/Kashtira, {ms // 1000} s a defence, on {dev.name}",
          flush=True)
    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock = threading.Lock()

    def one(job):
        r, pieces, ours_hand, arm = job
        scratch = OUT / "scratch" / f"h{r['hand_index']}-e{r['enemy_seed']}-{arm}"
        try:
            lost = game.kill_check(r["start"], r["prefix"], enemy_target, pieces | ours_hand,
                                   enemy_deck, scratch, ms, r["enemy_seed"] * 100)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- resumable: retried on the next run
            print(f"h{r['hand_index']} e{r['enemy_seed']} {arm}: {type(exc).__name__}: {exc}",
                  flush=True)
            return
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        rec = {"hand": r["hand_index"], "eseed": r["enemy_seed"], "arm": arm, "lost": int(lost),
               "ms": ms, "device": dev.name, "method": game.METHOD}
        with lock, res.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    for arm in ("off", "on"):
        # QUICK_RESPOND is read only by respond_codes, here: the arm's set goes in the job
        game.QUICK_RESPOND = arm == "on"
        ours_hand = set(game.respond_codes(deck, cards, extra=extra))
        jobs = [(r, p, ours_hand, arm) for r, p in todo
                if (r["hand_index"], r["enemy_seed"], arm) not in done]
        with ThreadPoolExecutor(1 if dev.name == "server" else dev.workers) as ex:
            list(ex.map(one, jobs))
    summary()


def summary():
    f = OUT / "results.jsonl"
    if not f.exists():
        return print("no results yet")
    by = {}
    for d in map(json.loads, f.read_text().splitlines()):
        by.setdefault((d["hand"], d["eseed"]), {})[d["arm"]] = d["lost"]
    pairs = [v for v in by.values() if len(v) == 2]
    saved = sum(1 for v in pairs if v["off"] and not v["on"])
    lost = sum(1 for v in pairs if v["on"] and not v["off"])
    print(f"{len(pairs)} paired positions: QUICK_RESPOND saved {saved}, lost {lost} "
          f"(off lost {sum(v['off'] for v in pairs)}, on lost {sum(v['on'] for v in pairs)})")


if __name__ == "__main__":
    main(*sys.argv[1:])
