"""RESPOND_ALL A/B: does letting EVERY card of our deck answer the enemy's turn save boards?

User, 2026-11-26 ("the ai hasnt made a board like mine"): the defender plays only what
`game.respond_codes` lists -- script-read interruptions, hand traps, the trainer's marked
pieces -- so Mind Shuffle's swap, Perlereino's destroy and the Tearlaments' GY Fusion
triggers (Sulliek sends one -> Fusion -> shuffle -> Perlereino) never fire on the enemy's
turn. `game.RESPOND_ALL` lists the whole deck (the core offers only what is legal).

Stored going-first turn-1 positions (a seeded sample), the games' own kill check
(`game.kill_check`: DEFENCES, majority of futures) at game.OTK_X, the game's seed; arm
"today" = respond_codes as live, "all" = RESPOND_ALL. Paired, resumable (results.jsonl).

    venv/bin/python tools/experiments/respond_all_ab.py run <enemy> [positions] [device-label]
    venv/bin/python tools/experiments/respond_all_ab.py summary
DUELROOM_DEVICE=duelpc to run there.
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

DECK = "imported"
OUT = ROOT / "results" / "experiments" / "respond_all_ab"
POSITIONS = 40
ARMS = ("today", "all")


def positions(conn, enemy, label, limit):
    dh = trainer.deck_hash(engine.DECKS / f"{DECK}.ydk")
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 AND device=? AND ok=1 "
        "ORDER BY hand_index, enemy_seed", (dh, enemy, label))
            if r["prefix"] and Path(r["prefix"]).exists()]
    return random.Random(26).sample(rows, min(int(limit), len(rows)))


def main(enemy, limit=POSITIONS, label="server"):
    from ygorl.engine.duel import default_cards
    dev = remote.current()
    remote.prepare(dev)
    conn, cards = db.connect(), default_cards()
    deck, enemy_deck = engine.DECKS / f"{DECK}.ydk", engine.DECKS / f"{enemy}.ydk"
    enemy_target = game.scenarios.target_for(enemy) or _neutral(enemy, cards)
    extra = trainer.prefs(conn, DECK)["interrupters"]
    todo = positions(conn, enemy, label, limit)
    ms = game.OTK_MS * game.OTK_X
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["enemy"], d["hand"], d["eseed"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    sets = {}
    for arm in ARMS:
        game.RESPOND_ALL = arm == "all"
        sets[arm] = set(game.respond_codes(deck, cards, extra=extra))
    game.RESPOND_ALL = False
    print(f"{enemy}: {len(todo)} positions, {ms // 1000} s a defence, on {dev.name}; "
          f"'all' adds {len(sets['all'] - sets['today'])} cards", flush=True)
    lock = threading.Lock()

    def one(job):
        r, arm = job
        board = game.replay(r["prefix"], until_turn=2).board
        ours = set(game._pieces(board, 0, cards)) | sets[arm]
        scratch = OUT / "scratch" / f"{enemy}-h{r['hand_index']}-e{r['enemy_seed']}-{arm}"
        try:
            lost = game.kill_check(r["start"], r["prefix"], enemy_target, ours, enemy_deck,
                                   scratch, ms, r["enemy_seed"] * 100)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- resumable: retried on the next run
            print(f"h{r['hand_index']} e{r['enemy_seed']} {arm}: {type(exc).__name__}: {exc}", flush=True)
            return
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        rec = {"enemy": enemy, "hand": r["hand_index"], "eseed": r["enemy_seed"], "arm": arm,
               "lost": int(lost), "ms": ms, "device": dev.name, "method": game.METHOD}
        with lock, res.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    jobs = [(r, arm) for r in todo for arm in ARMS
            if (enemy, r["hand_index"], r["enemy_seed"], arm) not in done]
    with ThreadPoolExecutor(4 if dev.name == "server" else dev.workers) as ex:
        list(ex.map(one, jobs))
    summary()


def _neutral(enemy, cards):
    # a deck with no payoff target: the duel's board target nothing matches (duel.plan)
    from skills.deck import Deck
    main = Deck.from_ydk(engine.DECKS / f"{enemy}.ydk").main
    spell = next(x for x in main if cards.get(x) is not None and cards.get(x).type & 0x2)
    return f"{spell}@mzone"


def summary():
    f = OUT / "results.jsonl"
    if not f.exists():
        return print("no results yet")
    by = {}
    for d in map(json.loads, f.read_text().splitlines()):
        by.setdefault(d["enemy"], {}).setdefault((d["hand"], d["eseed"]), {})[d["arm"]] = d["lost"]
    from scipy.stats import binomtest
    for enemy, pos in by.items():
        pairs = [v for v in pos.values() if len(v) == 2]
        saved = sum(1 for v in pairs if v["today"] and not v["all"])
        worse = sum(1 for v in pairs if v["all"] and not v["today"])
        p = binomtest(saved, saved + worse).pvalue if saved + worse else 1.0
        print(f"{enemy}: {len(pairs)} paired positions; killed today {sum(v['today'] for v in pairs)}, "
              f"all {sum(v['all'] for v in pairs)}; saved {saved}, newly lost {worse} (sign p {p:.3f})")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*sys.argv[2:])
