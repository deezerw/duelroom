"""Many turn-1 boards per position, each through the real kill check (Stage 11 step 2).

The data Part 2.2 lacked: a board model learnt from ONE board per hand can
only learn which hands are strong. Here every (hand, enemy hand) gets K
distinct end boards, made by asking turn 1 for different goals with
different seeds, and every board is labelled by the enemy's real turn-2
kill check (lost iff every defence is beaten). Within one position the
hand and the enemy hand are fixed, so what differs is the board alone.

  variants   full        the trained goal (what the games play)
             int         the goal's interruption cards only
             minus<i>    the goal without its i-th card
  x REPS seeds each. Identical boards (same cards in the same places) are
  stored once with every variant that made them, and checked once.

Each board keeps its closed turn-1 replay (start + prefix, a few KB), so it
can be labelled again later -- turn 3, another enemy -- without re-solving.

Each kill check also records `kill` (one entry per defence tried,
`kill_detail`): time to the first win, wins, states searched, the LP taken
when it did not kill, the enemy cards played, ours that fired, what we
stopped, and the line itself -- far more to learn from than lost/not.

    DUELROOM_DEVICE=duelpc venv/bin/python tools/ml/board_data.py [enemy hands] [limit]
    venv/bin/python tools/ml/board_data.py summary
    DUELROOM_DEVICE=duelpc venv/bin/python tools/ml/board_data.py recheck
Resumable: boards.jsonl + replays under OUT.
"""

import hashlib
import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, remote, trainer  # noqa: E402

DECK, ENEMY = "imported", "Angelechy-Meta"
OUT = ROOT / "results" / "ml" / "boards"
REPS = 2
ENEMY_HANDS = 10
# Budgets: the games' until the plateau test (tools/experiments/
# budget_plateau.py) says a search needs more.
T1_MS = engine.SOLVE_MS
OTK_MS = game.OTK_MS * 4     # Stage 15: the plateau says 1x misses most kills
_lock = threading.Lock()


def signature(board):
    """Our side of the closed turn 1, order-free where the game is."""
    me = board["players"][0]
    key = {z: sorted(map(str, me.get(z) or [])) for z in
           ("hand", "grave", "banished", "extra_faceup")}
    key["mzone"] = [str(x) for x in me.get("mzone") or []]   # columns matter
    key["szone"] = [str(x) for x in me.get("szone") or []]
    key["lp"] = me.get("lp")
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def free_targets(conn, hand, goal):
    """Opening-hand cards whose whole play is putting them down -- Field and
    Continuous Spells face up, Traps set -- and that the goal leaves out
    (Stage 15: 31% of the games' turn-1 boards ended with a Scream, a
    Perlereino or a Sulliek still in hand). Quick-Plays stay in hand."""
    from skills import card_db

    have = {int(str(t).split("@")[0]) for t in goal}
    out = []
    for c in dict.fromkeys(int(x) for x in hand):
        r = card_db.resolve(conn, c)
        if not r or c in have:
            continue
        if r["type"].startswith("Trap"):
            out.append(f"{c}@szone:fd")
        elif r["type"].startswith("Spell") and r["race"] in ("Field", "Continuous"):
            out.append(f"{c}@szone")
    return out


def variants(goal, pieces, free=()):
    out = [("full", goal)]
    ints = [t for t in goal if int(str(t).split("@")[0]) in pieces]
    if ints and ints != goal:
        out.append(("int", ints))
    if free:
        out.append(("free", goal + list(free)))
        if ints and ints != goal:
            out.append(("intfree", ints + list(free)))
    if len(goal) > 1:
        out += [(f"minus{i}", goal[:i] + goal[i + 1:]) for i in range(len(goal))]
    return out


def turn2_read(path):
    """{played, fired, stops} of the enemy's turn 2 in a kill-check replay
    (`stops.Reader`, our seat 0): the enemy cards it played, our cards that
    chained, and what we stopped ([kind, by, code])."""
    import ygorl.engine.messages as M
    from ygorl.engine.replay import Replay

    from skills.stops import Reader

    rep, rd, fired = Replay.from_yrp(Path(path)), Reader(0), []

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                rd.feed(m)
                if (type(m).__name__ == "Chaining" and rd.turn == 2 and m.code
                        and m.loc.controller == 0):
                    fired.append(m.code)

    rep.duel().replay(rep.responses, observer=_Obs())
    return {"played": sorted(c for c, t in rd.played.items() if t == 2),
            "fired": fired, "stops": [s[1:] for s in rd.stops if s[0] == 2]}


def kill_detail(k, prefix, defence, lost, keep):
    """One kill check, labelled past won-or-not (Stage 15, "richer label"):
    how hard the search worked (`game.solve_stats`), the LP it took when it
    did not kill, what was played and stopped along its line (the kill, or
    its best approach), and that line kept under `keep` for later reads."""
    d = {"defence": defence, "lost": lost, **game.solve_stats(k["log"])}
    line = k["solution"] if lost else k["approach"]
    d["damage"] = 8000 if lost else game._damage(prefix, k["approach"], 1)
    if line:
        kept = (shutil.copy(line, Path(keep) / f"kill-h{defence}{Path(line).suffix}")
                if keep else line)      # None: read it where it is (tools/ml/game_kills.py)
        d["line"] = str(kept)
        try:
            d.update(turn2_read(kept))
        except Exception as exc:  # noqa: BLE001 -- the label stands without it
            d["read_error"] = f"{type(exc).__name__}: {exc}"
    return d


def main(enemy_hands=ENEMY_HANDS, limit=None):
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
    ours_hand = game.respond_codes(deck, cards, extra=trainer.prefs(conn, DECK)["interrupters"])
    hands = [dict(r) for r in conn.execute(
        "SELECT hand_index, hand, best_goal, best_record FROM trainer_hands "
        "WHERE deck_hash=? AND best_goal IS NOT NULL ORDER BY hand_index", (dh,))]
    # Interruption codes: every code that ever counted as a piece on a
    # stored turn-1 board (as t1_backup_bench).
    pieces = {c for r in conn.execute(
        "SELECT pieces FROM game_turn1 WHERE deck_hash=? AND first=1 AND ok=1", (dh,))
        for c in json.loads(r["pieces"] or "[]")}
    OUT.mkdir(parents=True, exist_ok=True)
    out_file = OUT / "boards.jsonl"
    done, seen = set(), {}
    if out_file.exists():
        for d in map(json.loads, out_file.read_text().splitlines()):
            done.add((d["hand"], d["eseed"], d["variant"], d["rep"]))
            if d.get("sig") and "lost" in d:
                seen[(d["hand"], d["eseed"], d["sig"])] = d["lost"]

    def write(rec):
        with _lock, out_file.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def one(h, e, name, goal, rep):
        if (h["hand_index"], e, name, rep) in done:
            return
        tag = f"h{h['hand_index']}-e{e}-{name}-r{rep}"
        scratch = OUT / "scratch" / tag
        seed = e * 100 + 60 + rep
        rec = {"hand": h["hand_index"], "eseed": e, "variant": name, "rep": rep,
               "goal": goal, "device": dev.name, "t1_ms": T1_MS, "otk_ms": OTK_MS}
        try:
            t1 = game.turn1(deck, enemy_deck, json.loads(h["hand"]), goal, e, scratch,
                            T1_MS, seed, theirs, start=trainer.hand_start(h["best_record"]))
            if t1 is None:
                rec["ok"] = 0
                write(rec)
                return
            sig = signature(t1["board"])
            ours = set(game._pieces(t1["board"], 0, cards))
            rec.update(ok=1, reached=int(t1["reached"]), sig=sig, board=t1["board"],
                       pieces=sorted(ours))
            key = (h["hand_index"], e, sig)
            with _lock:
                known = seen.get(key)
            if known is not None:
                rec.update(lost=known, dup=True)
            else:
                keep = OUT / "replays" / tag
                keep.mkdir(parents=True, exist_ok=True)
                start = shutil.copy(t1["start"], keep / "start.yrpX")
                prefix = shutil.copy(t1["prefix"], keep / "prefix.yrpX")
                rec.update(start=str(start), prefix=str(prefix))
                lost, detail = [], []
                for v in game.DEFENCES:
                    k = game._solve(prefix, ["--start", start, "--root", prefix, "--no-ref",
                                             "--target", enemy_target, "--player", "1",
                                             "--turns", "2"]
                                    + game.defence_args(ours | set(ours_hand), v)
                                    + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)],
                                    scratch, f"otk-h{v}", OTK_MS, seed)
                    lost.append(int(bool(k["solution"]) and game._winner(k["solution"]) == 1))
                    detail.append(kill_detail(k, prefix, v, lost[-1], keep))
                    if not lost[-1]:
                        break       # a defence held: not lost, as game.turn2
                rec.update(lost=int(all(lost)), per_defence=lost, kill=detail)
                with _lock:
                    seen[key] = rec["lost"]
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        write(rec)
        print(f"{tag}: {'dup ' if rec.get('dup') else ''}reached {rec['reached']} "
              f"pieces {len(rec['pieces'])} lost {rec['lost']}", flush=True)

    def guarded(job):
        try:
            one(*job)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- reported, retried on the next run
            print(f"h{job[0]['hand_index']} e{job[1]} {job[2]} r{job[3]}: failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    seeds = [1000 + k for k in range(int(enemy_hands))]
    # Position by position, so a run cut short leaves whole positions (a
    # position with one board says nothing within-hand).
    jobs = [(h, e, name, goal, rep)
            for e in seeds for h in hands
            for name, goal in variants(trainer.split_goal(json.loads(h["best_goal"]))[0], pieces,
                                       free_targets(conn, json.loads(h["hand"]),
                                                    trainer.split_goal(json.loads(h["best_goal"]))[0]))
            for rep in range(REPS)]
    if limit:
        order = list(dict.fromkeys((h["hand_index"], e) for h, e, *_ in jobs))
        allowed = set(order[:int(limit)])
        jobs = [j for j in jobs if (j[0]["hand_index"], j[1]) in allowed]
    # jobs already done are in `jobs` too (one() skips them): the whole plan
    (OUT / "plan.json").write_text(json.dumps(
        {"keys": [[j[0]["hand_index"], j[1], j[2], j[4]] for j in jobs]}))
    print(f"{len(jobs)} boards to make on {dev.name} ({dev.workers} at once), "
          f"{len(done)} done", flush=True)
    with ThreadPoolExecutor(dev.workers) as ex:
        list(ex.map(guarded, jobs))
    summary()


RECHECK_MS = game.OTK_MS * 16    # the kill plateau still climbed at 16x (STAGE11)


def recheck(workers=None):
    """Kill-check every board that SURVIVED again at RECHECK_MS (idea 3).

    A found kill is proof; a survival may only mean the search ran out of
    time, and survivors are the few boards a pick model learns most from.
    Same seeds and defences as the first check; a defence that holds ends
    the board as before. Appends {hand, eseed, sig, lost16, kill} to
    rechecks.jsonl (resumable); the first label is left as it was."""
    from ygorl.engine.duel import default_cards

    dev = remote.current()
    remote.prepare(dev)
    conn = db.connect()
    cards = default_cards()
    deck = ROOT / "assets" / "decks" / f"{DECK}.ydk"
    ours_hand = game.respond_codes(deck, cards, extra=trainer.prefs(conn, DECK)["interrupters"])
    enemy_target = game.scenarios.target_for(ENEMY)
    out_file = OUT / "rechecks.jsonl"
    done = set()
    if out_file.exists():
        done = {(d["hand"], d["eseed"], d["sig"]) for d in map(json.loads, out_file.read_text().splitlines())}
    todo = [d for d in map(json.loads, (OUT / "boards.jsonl").read_text().splitlines())
            if d.get("ok") and not d.get("dup") and d.get("lost") == 0 and d.get("prefix")
            and (d["hand"], d["eseed"], d["sig"]) not in done]
    print(f"{len(todo)} survivor(s) to re-check at {RECHECK_MS // 1000} s on {dev.name}", flush=True)

    def one(d):
        tag = f"h{d['hand']}-e{d['eseed']}-{d['variant']}-r{d['rep']}"
        scratch = OUT / "scratch" / f"recheck-{tag}"
        keep = Path(d["prefix"]).parent
        seed = d["eseed"] * 100 + 60 + d["rep"]
        ours = set(d["pieces"])
        lost, detail = [], []
        try:
            for v in game.DEFENCES:
                k = game._solve(d["prefix"], ["--start", d["start"], "--root", d["prefix"],
                                              "--no-ref", "--target", enemy_target,
                                              "--player", "1", "--turns", "2"]
                                + game.defence_args(ours | set(ours_hand), v)
                                + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)],
                                scratch, f"otk16-h{v}", RECHECK_MS, seed)
                lost.append(int(bool(k["solution"]) and game._winner(k["solution"]) == 1))
                x = kill_detail(k, d["prefix"], v, lost[-1], None)
                if x.get("line"):
                    x["line"] = str(shutil.copy(x["line"], keep / f"kill16-h{v}{Path(x['line']).suffix}"))
                detail.append(x)
                if not lost[-1]:
                    break
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        rec = {"hand": d["hand"], "eseed": d["eseed"], "sig": d["sig"], "otk_ms": RECHECK_MS,
               "lost16": int(all(lost)), "kill": detail}
        with _lock, out_file.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"{tag}: lost at 16x {rec['lost16']}", flush=True)

    def guarded(d):
        try:
            one(d)
        except remote.DeviceDown:
            raise
        except Exception as exc:  # noqa: BLE001 -- retried on the next run
            print(f"recheck h{d['hand']} e{d['eseed']}: failed: {type(exc).__name__}: {exc}",
                  flush=True)

    with ThreadPoolExecutor(int(workers or dev.workers)) as ex:
        list(ex.map(guarded, todo))
    rs = [json.loads(l) for l in out_file.read_text().splitlines()]
    print(f"re-checked {len(rs)}: {sum(r['lost16'] for r in rs)} killed at 16x "
          f"({sum(r['lost16'] for r in rs) / max(1, len(rs)):.0%} of the survivors were false)")


def progress():
    """For the Jobs page: turn 1s made of the plan main() wrote."""
    f, plan = OUT / "boards.jsonl", OUT / "plan.json"
    if not plan.exists():
        return None
    keys = {tuple(k) for k in json.loads(plan.read_text())["keys"]}
    made = {(d["hand"], d["eseed"], d["variant"], d["rep"])
            for d in map(json.loads, f.read_text().splitlines())} if f.exists() else set()
    return {"done": len(keys & made), "total": len(keys), "parts": []}


def summary():
    recs = [json.loads(l) for l in (OUT / "boards.jsonl").read_text().splitlines()]
    ok = [d for d in recs if d.get("ok")]
    by = {}
    for d in ok:
        by.setdefault((d["hand"], d["eseed"]), {})[d["sig"]] = d["lost"]
    distinct = sum(len(v) for v in by.values())
    mixed = sum(1 for v in by.values() if 0 < sum(v.values()) < len(v))
    print(f"{len(recs)} turn 1s, {len(ok)} with a board, {distinct} distinct boards "
          f"over {len(by)} positions ({distinct / max(1, len(by)):.1f} each)")
    print(f"positions where the board decides (some boards lost, some lived): "
          f"{mixed} of {len(by)}")
    for name in sorted({d["variant"][:5] for d in ok}):
        v = [d for d in ok if d["variant"].startswith(name)]
        print(f"  {name:6} {len(v):5} boards  lost {sum(d['lost'] for d in v) / len(v):.0%}"
              f"  reached {sum(d['reached'] for d in v) / len(v):.0%}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        summary()
    elif len(sys.argv) > 1 and sys.argv[1] == "recheck":
        recheck(*sys.argv[2:3])
    else:
        main(*sys.argv[1:3])
