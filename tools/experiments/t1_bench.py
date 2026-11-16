"""Turn-1 A/B: why does our turn 1 miss the trainer's goal so often?

Positions: stored going-first turn 1s of a matchup where the enemy fired NOTHING
(from tools/experiments/t1_fired.py's JSONL) and the goal was still not reached,
so the hand traps cannot be the reason. Every arm re-solves the SAME turn 1
(`game.turn1`: same hand, goal, enemy template, defender), differing in budget
or warm start:

  A  60 s     the games' recipe, a fresh solver seed (how often a redraw hits)
  C  180 s    the trainer's budget (TRAIN_MS)
  R  60 s     + --adapt <the trainer's best line for this hand> (warm start)
  O  60 s     the TRAINER'S DECK ORDER under the hand (the game's recipe forces
              the hand onto the .ydk FILE order; the trainer solved on the hand
              seed's shuffle, and 23 of 24 best lines break on the file order)
  Q  60 s     O + --adapt the best line
  S  60 s     the trainer's DUEL: its order AND its core seed (in-line shuffles
              follow the core's randomness; 14 of 24 lines still broke on the
              order alone) -- what game.turn1 now does
  T  60 s     S + --adapt the best line
  L  60 s     S + the trainer's best line as --approach-line with
              --finisher-min 40 s. Needs a binary with the UNAPPLIED
              patches/0005-approach-line.patch on top of 0002-0004, and the
              line written by the removed game.line_file (git history).

Measured 2026-11-14 on 40 positions (paired, all S-arm duels): S reached 2,
T 4, L 5; L vs S +5 -2 on the goal and 9 more / 9 fewer pieces -- noise, so
neither --adapt nor the line-first patch is in the games. A, O, Q, R ran on
part of the set only. Our turn 1 loses to the enemy's interruptions, which
the defender now actually fires (see t1_fired.py), not to a weak search.

    venv/bin/python tools/experiments/t1_bench.py <fired.jsonl> [limit] [arms] [deck] [enemy]

Resumable: results.jsonl under OUT.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, trainer  # noqa: E402

OUT = Path("/tmp/claude-1000/-home-deezer/beb19f01-23ee-46b2-b103-b63615d8323f"
           "/scratchpad/t1/bench")
# arm: (ms, adapt, trainer's deck order)
ARMS = {"A": (60_000, False, False), "C": (180_000, False, False),
        "R": (60_000, True, False), "O": (60_000, False, True), "Q": (60_000, True, True),
        "S": (60_000, False, "duel"), "T": (60_000, True, "duel"),
        "L": (60_000, False, "line")}
EXTRA = {"L": ["--finisher-min", "40000"]}
WORKERS = 4


def best_line_replay(conn, dh, hand_index, out):
    """The trainer's best line for the hand written as a .yrpX (for --adapt)."""
    from ygorl.engine.replay import Replay
    row = conn.execute("SELECT best_record FROM trainer_hands WHERE deck_hash=? "
                       "AND hand_index=?", (dh, hand_index)).fetchone()
    rec = json.loads(row["best_record"])
    start = rec["start"]
    rep = Replay(seed=0, first=0, rule_flags=start.get("rule_flags", 0),
                 player=start.get("player", {}), shuffle_decks=False,
                 decks=start["decks"],
                 responses=[bytes.fromhex(r) for r in rec["lines"][0]["responses"]],
                 seed_words=start.get("core_seed"))
    out.parent.mkdir(parents=True, exist_ok=True)
    rep.to_yrpx(out, names=("you", "goldfish"))
    return out


def main(fired, limit=40, arms="AC", deck="imported", enemy="Angelechy-Meta"):
    conn = db.connect()
    deck_path = ROOT / "assets" / "decks" / f"{deck}.ydk"
    enemy_deck = ROOT / "assets" / "decks" / f"{enemy}.ydk"
    dh = trainer.deck_hash(deck_path)
    theirs = game.enemy_respond_codes(enemy_deck)
    positions = [json.loads(l) for l in Path(fired).read_text().splitlines() if l]
    positions = [p for p in positions if not p["fired"] and not p["reached"]]
    positions = positions[::max(1, len(positions) // int(limit))][:int(limit)]
    OUT.mkdir(parents=True, exist_ok=True)
    res_path = OUT / "results.jsonl"
    done = set()
    if res_path.exists():
        for l in res_path.read_text().splitlines():
            r = json.loads(l)
            done.add((r["arm"], r["hand"], r["seed"]))
    hands = {r["hand_index"]: r for r in conn.execute(
        "SELECT hand_index, hand, best_goal, best_record FROM trainer_hands "
        "WHERE deck_hash=?", (dh,))}

    def one(job):
        arm, p = job
        if (arm, p["hand"], p["seed"]) in done:
            return
        ms, adapt, order = ARMS[arm]
        row = hands[p["hand"]]
        goal = trainer.split_goal(json.loads(row["best_goal"]))[0]
        scratch = OUT / arm / f"h{p['hand']}" / f"e{p['seed']}"
        extra = list(EXTRA.get(arm, []))
        hand = json.loads(row["hand"])
        our_deck = deck_path
        start = None
        if order in ("duel", "line"):
            start = trainer.hand_start(row["best_record"])
            if order == "duel":
                start["line"] = []       # the duel alone, no known line
        elif order:
            # The trainer's order, hand at the tail. Written hand-first so the
            # solver's "remove the first copy of each hand card, append the
            # hand" leaves exactly the trainer's rest in the trainer's order.
            from skills.deck import Deck
            full = json.loads(row["best_record"])["start"]["decks"]["a"]["main"]
            tail = full[-len(hand):]
            assert sorted(tail) == sorted(hand)
            d = Deck.from_ydk(deck_path)
            our_deck = scratch / "ordered.ydk"
            scratch.mkdir(parents=True, exist_ok=True)
            Deck(name=d.name, main=tail + full[:-len(hand)], extra=list(d.extra),
                 side=list(d.side)).write_ydk(our_deck)
            hand = tail
        if adapt:
            line = best_line_replay(db.connect(), dh, p["hand"],
                                    OUT / "lines" / f"h{p['hand']}.yrpX")
            extra = ["--adapt", str(line)]
        t0 = time.time()
        if start:
            t1 = game.turn1(our_deck, enemy_deck, hand, goal, p["seed"], scratch, ms,
                            p["seed"] * 10 + "ACROQSTL".index(arm) + 1, theirs,
                            start=start, extra=extra)
            if t1:
                from ygorl.engine.duel import default_cards
                t1 = {"reached": t1["reached"],
                      "pieces": game._pieces(t1["board"], 0, default_cards())}
        else:
            t1 = turn1(our_deck, enemy_deck, hand, goal, p["seed"],
                       scratch, ms, p["seed"] * 10 + "ACROQSTL".index(arm) + 1, theirs, extra)
        out = {"arm": arm, "hand": p["hand"], "seed": p["seed"],
               "reached": int(bool(t1 and t1["reached"])),
               "pieces": len(t1["pieces"]) if t1 else None,
               "wall": round(time.time() - t0, 1)}
        with open(res_path, "a") as f:
            f.write(json.dumps(out) + "\n")
        print(out, flush=True)

    jobs = [(a, p) for a in arms for p in positions]
    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(one, jobs))
    summary(res_path)


def turn1(our_deck, enemy_deck, hand, goal, enemy_seed, scratch, ms, seed, theirs, extra):
    """game.turn1 with extra solver flags; returns {reached, pieces}."""
    from ygorl.engine.duel import default_cards
    from skills import breaking
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    tpl = breaking.template_vs(scratch / "tpl.yrpX", our_deck, enemy_deck,
                               seed=enemy_seed, shuffle=True)
    args = ["--no-ref", "--deck", Path(our_deck).resolve(), "--hand", "|".join(map(str, hand))]
    for t in goal:
        args += ["--target", t]
    args += game.defender_args(theirs) + extra
    r = game._solve(tpl, args, scratch, "t1", ms, seed)
    src = r["solution"] or r["approach"]
    if src is None:
        return None
    pos = game.replay(src, until_turn=2)
    return {"reached": r["solution"] is not None,
            "pieces": game._pieces(pos.board, 0, default_cards())}


def summary(res_path):
    rows = [json.loads(l) for l in res_path.read_text().splitlines() if l]
    by = {}
    for r in rows:
        by.setdefault(r["arm"], {})[(r["hand"], r["seed"])] = r
    for arm, d in sorted(by.items()):
        n = len(d); k = sum(r["reached"] for r in d.values())
        print(f"arm {arm}: reached {k}/{n}, mean pieces "
              f"{sum(r['pieces'] or 0 for r in d.values()) / max(n, 1):.2f}, "
              f"mean wall {sum(r['wall'] for r in d.values()) / max(n, 1):.0f} s")


if __name__ == "__main__":
    if sys.argv[1:] == ["summary"]:
        summary(OUT / "results.jsonl")
    else:
        main(*sys.argv[1:])
