"""The blunder checker: which of our decisions threw a win away?

User, 2026-11-21: the AI "made some choices that werent specifically wrong but
just unoptimal" -> brainstorm -> step 2 (user chose the order): find them
automatically instead of by reading traces.

Lethal turns first (our turn 3 going first, our turn 2 -- the break -- going
second), in games that did NOT win. W(k) = "from the position just before our
k-th decision of that turn, can the games' own lethal search still find a win?"
(`game.turn3` rooted there: --goal-win against every DEFENCES script, at the
games' budget). At the turn's start W is checked first (no win there: nothing to
throw); after the last decision the game had none. A binary search finds the k
where W flips -- about log2(decisions) searches a game instead of re-searching
every alternative of every decision -- and both sides of the flip are confirmed
with a second seed (the search is randomized: one miss would move the flip).
The confirmed decision is the blunder; the winning line found just before it says
what to play instead.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/blunders.py lethal [enemy] [limit]
    venv/bin/python tools/experiments/blunders.py summary
Resumable (results.jsonl, one line per game).
"""

import json
import os
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import breaking, db, engine, game, remote, trainer  # noqa: E402

DECK = "imported"
OUT = ROOT / "results" / "experiments" / "blunders"
CONFIRM_SEED = 777
_lock = threading.Lock()


def decisions(line_src, seat, turn):
    """Indices (into the line's responses) of `seat`'s decisions on `turn` that
    had a real choice (more than one legal action)."""
    from ygorl.engine.branch import actions_for_response
    from ygorl.engine.duel import DuelSession
    from ygorl.engine.replay import Replay
    from ygorl.solver.demo import canonical_response

    rep = Replay.from_yrp(Path(line_src))
    s = DuelSession(rep.duel())
    out = []
    for k, raw in enumerate(rep.responses):
        p = s.point
        if p is None:
            break
        if s.tracker.turn == turn and p.player == seat and len(p.actions) > 1:
            out.append(k)
        for i in actions_for_response(p.state, canonical_response(p.decision, raw)) or ():
            s.act(i)
    return out


def label_at(line_src, k, raw, seat):
    """The action response `raw` plays at the line's k-th decision, as a label."""
    from ygorl.engine.branch import actions_for_response
    from ygorl.engine.duel import DuelSession, default_cards
    from ygorl.engine.replay import Replay
    from ygorl.solver.demo import canonical_response

    from skills import duel
    rep = Replay.from_yrp(Path(line_src))
    s = DuelSession(rep.duel())
    for r in rep.responses[:k]:
        p = s.point
        for i in actions_for_response(p.state, canonical_response(p.decision, r)) or ():
            s.act(i)
    p = s.point
    plan = actions_for_response(p.state, canonical_response(p.decision, raw)) or []
    cards = default_cards()
    return " + ".join(duel.label(p.actions[i], cards, seat) for i in plan) or "?"


def main(mode="lethal", enemy="Angelechy-Meta", limit=None):
    from ygorl.engine.duel import default_cards
    from ygorl.engine.replay import Replay

    dev = remote.current()
    remote.prepare(dev)
    conn = db.connect()
    cards = default_cards()
    deck, enemy_deck = engine.DECKS / f"{DECK}.ydk", engine.DECKS / f"{enemy}.ydk"
    dh = trainer.deck_hash(deck)
    theirs = set(game.enemy_respond_codes(enemy_deck, cards))
    hands = {r["hand_index"]: dict(r) for r in conn.execute(
        "SELECT hand_index, best_goal FROM trainer_hands WHERE deck_hash=?", (dh,))}
    enemy_target = game.scenarios.target_for(enemy)
    games = [dict(g) for g in conn.execute(
        "SELECT * FROM game_results WHERE deck_hash=? AND enemy=? AND outcome='no_win' AND line IS NOT NULL "
        "AND method IS NOT NULL ORDER BY COALESCE(damage, 0) DESC, hand_index, enemy_seed, run", (dh, enemy))
             if Path(g["line"]).exists() and g["hand_index"] in hands]
    if limit:
        games = games[:int(limit)]
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["first"], d["device"], d["hand"], d["eseed"], d["run"])
             for d in map(json.loads, res.read_text().splitlines())} if res.exists() else set())
    todo = [g for g in games if (g["first"], g["device"], g["hand_index"], g["enemy_seed"], g["run"]) not in done]
    print(f"{len(games)} no-win games ({len(todo)} to check) on {dev.name}", flush=True)

    def one(g):
        first = bool(g["first"])
        seat, turn = (0, 3) if first else (1, 2)
        ms = engine.SOLVE_MS * (game.T3_X if first else game.BREAK_X)
        ms = int(ms * float(os.environ.get("BLUNDER_SCALE", "1")))   # a smoke test only: < 1 is weaker
        goal = game.live_goal(json.loads(hands[g["hand_index"]]["best_goal"]), deck)
        target = game.steer(goal, enemy_target)
        line = g["line"]
        rep = Replay.from_yrp(Path(line))
        idx = decisions(line, seat, turn)
        scratch = OUT / "scratch" / f"{int(first)}-{g['device']}-h{g['hand_index']}-e{g['enemy_seed']}-r{g['run']}"
        rec = {"first": g["first"], "device": g["device"], "hand": g["hand_index"], "eseed": g["enemy_seed"],
               "run": g["run"], "decisions": len(idx), "searches": 0}

        def W(k, seed):
            """Win reachable from just before decision k (k == len(idx): the line's end)."""
            cut = idx[k] if k < len(idx) else len(rep.responses)
            d = scratch / f"k{k}-s{seed}"
            start, prefix = breaking.split_position(rep, list(rep.responses[:cut]), d)
            board = game.replay(prefix).board
            enemy_pieces = set(game._pieces(board, 1 - seat, cards))
            rec["searches"] += 1
            t = game.turn3(start, prefix, target, theirs | enemy_pieces, d, ms, seed,
                           player=seat, turn=turn, our_deck=deck)
            return t["won"], t.get("line")

        try:
            if not idx:
                rec["verdict"] = "no decisions"
            else:
                seed = g["enemy_seed"] * 100 + g["run"] + 13
                ok0, _ = W(0, seed)
                if not ok0:
                    rec["verdict"] = "no win at the turn's start"
                else:
                    lo, hi = 0, len(idx)                  # W(lo) True, W(hi) False (the game)
                    win_line = None
                    while hi - lo > 1:
                        mid = (lo + hi) // 2
                        ok, wl = W(mid, seed)
                        if ok:
                            lo, win_line = mid, wl
                        else:
                            hi = mid
                    # confirm both sides of the flip on a second seed
                    ok_lo, wl_lo = W(lo, CONFIRM_SEED)
                    ok_hi = W(hi, CONFIRM_SEED)[0] if hi < len(idx) else False
                    if ok_lo and not ok_hi:
                        k = idx[lo]
                        best = win_line or wl_lo
                        better_raw = Replay.from_yrp(Path(best)).responses[k] if best else None
                        rec.update(verdict="blunder", decision=lo, response_index=k,
                                   chose=label_at(line, k, rep.responses[k], seat),
                                   better=label_at(line, k, better_raw, seat) if better_raw else None,
                                   win_line=str(best) if best else None)
                        if best:          # keep the winning line: the answer, watchable
                            keep = OUT / "lines" / f"{scratch.name}.yrp"
                            keep.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy(best, keep)
                            rec["win_line"] = str(keep)
                    else:
                        rec.update(verdict="unconfirmed flip", decision=lo)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- resumable
            print(f"h{g['hand_index']} e{g['enemy_seed']} r{g['run']}: {type(exc).__name__}: {exc}", flush=True)
            return
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        with _lock, res.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    with ThreadPoolExecutor(1 if dev.name == "server" else dev.workers) as ex:
        list(ex.map(one, todo))
    summary()


def summary():
    f = OUT / "results.jsonl"
    if not f.exists():
        print("no results yet")
        return
    from collections import Counter
    recs = [json.loads(l) for l in f.read_text().splitlines()]
    print(f"{len(recs)} no-win games checked:", dict(Counter(r["verdict"] for r in recs)))
    for r in recs:
        if r["verdict"] == "blunder":
            side = "first, turn 3" if r["first"] else "second, turn 2"
            print(f"  hand {r['hand'] + 1} vs enemy hand {r['eseed'] - 999} game {r['run'] + 1} ({side}, {r['device']}): "
                  f"decision {r['decision'] + 1}/{r['decisions']} -- chose: {r['chose']} | a win went through: {r['better']}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*sys.argv[1:4])
