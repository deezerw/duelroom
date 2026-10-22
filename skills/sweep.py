"""The board library, solved properly.

`engine.solve_blocking` shells out to ygorl's `tools/solve_blocking.py`,
which takes ONE draw per goal and stops at a pair. Everything the project
believes about end boards comes out of that file -- `boards`, `gate`,
`targets`, `threats`, `matrix` and `optimize` all read it -- so every one
of those numbers is a floor rather than a best. Measured on one elfnote
hand: the library records 4 interruptions; 32 draws at a QUARTER of the
budget reached 10 distinct boards topping out at 5 int / 4 neg.

This module is the same sweep with three changes, each of which was
measured rather than assumed:

1. **Several draws per goal.** The solver is a randomized search (see
   CLAUDE.md). One draw is one sample, and the sample it returns is
   usually not the best board the hand can reach, because the search
   stops at the first line satisfying the targets and anything else on
   the board is incidental.

2. **The whole ladder, enumerated up front.** Upstream climbs -- try each
   piece, then pair the two that solved -- so the rungs are serial and
   the best board it can ever ASK for is base + 2. Here every rung is a
   goal from the start, which also means every (hand, goal, draw) is
   independent work and the pool stays full. Upstream parallelises per
   HAND at one thread each, so the tail of a sweep runs on one core.

3. **Reproducible solver seeds.** No `--seed` is passed anywhere today,
   so all 280 records in `results/boards.jsonl` carry a different random
   one and no run can be repeated. Each draw's seed is derived from the
   hand seed and the goal, so a rerun does the same work.

The output is byte-compatible with upstream's: the same demonstration
JSONL, the same per-deck summary. Nothing downstream needed changing,
which is the point -- two implementations of a number is how this
project's worst bugs got in.
"""

import json
import tempfile
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import engine, gauntlet

DRAWS = gauntlet.DRAWS


def _draw_seed(hand_seed, targets, draw):
    """A reproducible solver seed for one (hand, goal, draw).

    zlib.crc32 rather than hash(): hash() of a string is salted per
    process, so a "reproducible" seed built from it would differ between
    runs -- which is the bug this is here to fix, reintroduced.
    """
    key = f"{hand_seed}|{'+'.join(targets)}|{draw}".encode()
    return zlib.crc32(key) & 0x7FFFFFFF


def _solve_one(args):
    """One (hand, goal, draw). Top level so the spawn pool can pickle it."""
    (deck_path, name, hand_index, hand_seed, hand, targets, label,
     solve_ms, draw) = args
    from ygorl.engine.duel import default_cards
    from ygorl.solver import HandJob, Workdir, find_solver
    from ygorl.solver.batch import solve_hand
    from ygorl.solver.blocking import board_interruptions

    cards = default_cards()
    scratch = Path(tempfile.mkdtemp(prefix="duelroom-sweep-"))
    seed = _draw_seed(hand_seed, targets, draw)
    job = HandJob(
        deck_path=Path(deck_path).resolve(), hand_index=hand_index,
        hand_seed=hand_seed, targets=tuple(targets),
        workdir=Workdir.create(scratch / "wd").path, scratch=scratch / "job",
        solve_ms=solve_ms, threads=1, hand=tuple(hand), lines=4,
        binary=find_solver(), solver_seed=seed,
        timeout_s=solve_ms / 1000 + 300)
    demo = solve_hand(job, cards=cards)
    rec = demo.to_json()
    attempt = {"label": label, "targets": list(targets), "draw": draw,
               "solver_seed": seed, "status": demo.status,
               "wall_s": demo.solver.get("wall_s", 0.0)}
    if demo.status != "solved" and "best_placed" in demo.solver:
        attempt["best_placed"] = demo.solver["best_placed"]
    score = None
    if demo.status == "solved" and demo.lines:
        scored = sorted(
            ((board_interruptions(ln.board, cards), i)
             for i, ln in enumerate(demo.lines)),
            key=lambda t: (t[0].key(), -t[1]), reverse=True)
        best, i = scored[0]
        # Move the best line first, as upstream's _score_demo does: every
        # consumer reads lines[0] as "the" board.
        rec["lines"].insert(0, rec["lines"].pop(i))
        score = best.to_json()
        attempt.update(score)
    return {"hand_index": hand_index, "label": label, "attempt": attempt,
            "score": score, "record": rec if score else None,
            "unsolved_record": None if score else rec}


def allocate(attempts, draws, n_goals):
    """Which goals get the remaining draws, after one draw of each.

    A flat `draws` per goal spends the same budget on a goal that cannot
    be reached as on one that can. Measured over the stored sweep: of 320
    attempts that failed, 132 (41%) never placed a SINGLE one of their
    target cards, and each burned its full budget -- 2.2 of the sweep's
    8.2 hours. Re-drawing those is the clearest waste available, because
    a goal the search cannot get one card towards is not a goal a
    different seed rescues.

    So the draws go in two rounds. Round one is breadth: one draw of
    every goal. Round two is depth, on what round one showed is worth it:

    * a goal that SOLVED earns re-draws, because re-drawing a solved goal
      is what finds the better board -- the elfnote hand went 3, 3, 3, 3,
      5, 6 interruptions across six draws of the same goal;
    * a goal that failed but placed some of its targets is a near miss and
      earns re-draws after those, ranked by how close it came;
    * a goal that placed nothing earns none.

    The budget is unchanged: `draws * n_goals` solves per hand either way.
    Returns a list of (targets, label) repeated once per draw to spend.
    """
    budget = max(0, (draws - 1) * n_goals)
    if budget == 0:
        return []
    solved, near = [], []
    for a in attempts:
        goal = (tuple(a["targets"]), a["label"])
        if a["status"] == "solved":
            # Best board first: the goal already proving the most is the
            # one most likely to prove more.
            solved.append((-(a.get("interruptions") or 0),
                           -(a.get("negates") or 0), goal))
        else:
            placed = a.get("best_placed")
            if isinstance(placed, int) and placed > 0:
                near.append((-placed / max(1, len(a["targets"])), goal))
    ranked = [g for _, _, g in sorted(solved)] + [g for _, g in sorted(near)]
    if not ranked:
        return []
    # Round-robin so the budget spreads over the worthwhile goals instead
    # of pouring every spare draw into the first one.
    return [ranked[i % len(ranked)] for i in range(budget)]


def combinations_to_try(attempts, max_new=6):
    """Stack goals worth asking for, now that we know which pieces solve.

    `gauntlet._goals` can only enumerate PREFIX stacks -- with pieces
    ranked A, B, C, D it asks for AB, ABC, ABCD and never AC, BD or BCD.
    The ranking is static (`Piece.rank`: engine pieces, then negations,
    then Extra Deck) and knows nothing about this hand, so a deck whose
    reachable pair is A+C is simply never asked whether it can make two
    interrupters. That is the other half of the problem the climb fixed
    only partly: not the solver failing to find a good board, but nobody
    requesting one.

    The breadth round answers the question the enumeration could not --
    which pieces this hand can reach at all -- so the depth round builds
    stacks out of exactly those. Bigger combinations first, because more
    interrupters is the point; combinations already tried are skipped.
    """
    from itertools import combinations

    base, solved = None, []
    tried = set()
    for a in attempts:
        tried.add(frozenset(a["targets"]))
        if a["label"].startswith("piece:") and a["status"] == "solved":
            # A piece goal is base + the one piece, so the piece is last
            # and everything before it is the base.
            solved.append(a["targets"][-1])
            if base is None:
                base = list(a["targets"][:-1])
    if base is None or len(solved) < 2:
        return []
    out = []
    for size in range(len(solved), 1, -1):
        for combo in combinations(solved, size):
            targets = [*base, *combo]
            if frozenset(targets) in tried:
                continue
            out.append((tuple(targets), f"stack:{size}"))
            if len(out) >= max_new:
                return out
    return out


def _hand_jobs(deck_path, name, hands, first_hand, seed, pieces, solve_ms,
               draws, done):
    """Every (hand, goal, draw) for one deck, skipping finished hands."""
    from ygorl.cards.ydk import load_ydk
    from ygorl.solver.batch import hand_seed as _hand_seed, sample_hand

    deck = load_ydk(Path(deck_path))
    out, hand_info = [], {}
    for i in range(first_hand, first_hand + hands):
        hs = _hand_seed(seed, name, i)
        if (name, i, "plain", None) in done:
            continue
        hand, _ = sample_hand(deck, hs)
        hand_info[i] = (hs, list(hand))
        for targets, label in gauntlet._goals(deck_path, hand, pieces):
            for d in range(draws):
                out.append((deck_path, name, i, hs, list(hand), list(targets),
                            label, solve_ms, d))
    return out, hand_info


def solve_deck(deck_path, hands=8, draws=DRAWS, pieces=4, solve_ms=None,
               seed=0, first_hand=0, out=None, workers=None, log=None):
    """Sweep one deck's opening hands, keeping the best board per hand.

    Appends demonstration JSONL as each hand completes, so a rerun
    resumes. Returns the list of records written this run.
    """
    from ygorl.cards.ydk import load_ydk
    from ygorl.solver.batch import done_keys

    say = log or (lambda m: None)
    engine.require()
    engine.preflight([deck_path])
    solve_ms = engine.SOLVE_MS if solve_ms is None else solve_ms
    workers = workers or engine.DEFAULT_WORKERS
    deck_path = Path(deck_path).resolve()
    name = load_ydk(deck_path).name or deck_path.stem
    out = Path(out) if out else engine.ROOT / "results" / "boards_multi.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    # done_keys(path, environment) -- the second argument is not optional,
    # and the keys are (deck name, hand index, variant, fire).
    done = done_keys(out, None) if out.exists() else set()

    # Round one is BREADTH: one draw of every goal of every hand. Round
    # two is DEPTH, spending the rest only where round one showed it is
    # worth spending (see `allocate`). Same budget, better placed.
    work, hand_info = _hand_jobs(deck_path, name, hands, first_hand, seed,
                                 pieces, solve_ms, 1, done)
    if not work:
        say(f"{name}: every hand already in {out.name}")
        return []
    n_hands = len({w[2] for w in work})
    n_goals = len(work) // max(1, n_hands)
    say(f"{name}: {n_hands} hand(s), {n_goals} goals each, {draws} draws, "
        f"{n_hands * n_goals * draws} solves of {solve_ms // 1000}s "
        f"across {workers} worker(s)")

    per_hand, start = {}, time.monotonic()

    def collect(res):
        acc = per_hand.setdefault(res["hand_index"],
                                  {"attempts": [], "best": None,
                                   "fallback": None})
        acc["attempts"].append(res["attempt"])
        if res["score"] is not None:
            key = (res["score"]["interruptions"], res["score"]["negates"])
            if acc["best"] is None or key > acc["best"][0]:
                acc["best"] = (key, res["record"], res["score"])
        elif acc["fallback"] is None:
            acc["fallback"] = res["unsolved_record"]

    def run(batch, phase):
        done_n = 0
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(_solve_one, batch):
                collect(res)
                done_n += 1
                if done_n % max(1, len(batch) // 5) == 0:
                    say(f"  {phase}: {done_n}/{len(batch)} solves, "
                        f"{time.monotonic() - start:.0f}s")

    run(work, "breadth")

    second, new_total = [], 0
    for i, acc in per_hand.items():
        hs, hand = hand_info[i]
        budget = max(0, (draws - 1) * n_goals)
        # NEW combinations first. A stack of three pieces that each solved
        # can reach a board no re-draw of a single piece ever will, so it
        # outranks spending the same budget on another sample of something
        # already measured.
        fresh = combinations_to_try(acc["attempts"])[:budget]
        new_total += len(fresh)
        spend = fresh + allocate(acc["attempts"], draws, n_goals)[
            :budget - len(fresh)]
        # Each repeat of a goal needs its own draw index, or it would
        # reuse the same reproducible seed and redo identical work.
        seen = {}
        for targets, label in spend:
            key = (label, tuple(targets))
            seen[key] = seen.get(key, 0) + 1
            second.append((deck_path, name, i, hs, hand, list(targets),
                           label, solve_ms, seen[key]))
    if second:
        kept = len({(w[2], tuple(w[5])) for w in second})
        say(f"  depth: {len(second)} draws over {kept} goal(s), "
            f"{new_total} of them combinations the enumeration could not "
            f"reach (of {n_hands * n_goals} breadth goals)")
        run(second, "depth")
    else:
        say("  depth: nothing solved and nothing came close — no re-draws")

    written = []
    for i in sorted(per_hand):
        acc = per_hand[i]
        rec, score = (acc["best"][1], acc["best"][2]) if acc["best"] \
            else (acc["fallback"], None)
        if rec is None:
            continue
        hs, hand = hand_info[i]
        rec.setdefault("hand", hand)
        rec["hand_index"] = i
        rec["hand_seed"] = hs
        solver = rec.setdefault("solver", {})
        solver["blocking"] = {
            "draws": draws,
            "attempts": acc["attempts"],
            "solved_attempts": sum(1 for a in acc["attempts"]
                                   if a["status"] == "solved"),
            # The spread across draws of the SAME goal is the honest
            # measure of how much a single-draw run was leaving behind.
            "best_of": _spread(acc["attempts"]),
            **(score or {}),
        }
        solver["wall_s"] = round(sum(a["wall_s"] for a in acc["attempts"]), 2)
        with out.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        written.append(rec)
        say(f"  hand {i}: {rec.get('status')} "
            + (f"{score['interruptions']} int / {score['negates']} neg"
               if score else "no board")
            + f"  ({solver['wall_s']:.0f}s of solver)")
    return written


def _spread(attempts):
    """Per goal: the interruption counts its draws reached, best first.

    This is the number that justifies the extra budget. If every draw of
    every goal returns the same board, drop `draws` back to 1 and say so.
    """
    by = {}
    for a in attempts:
        if a["status"] == "solved" and "interruptions" in a:
            by.setdefault(a["label"], []).append(a["interruptions"])
    return {k: sorted(v, reverse=True) for k, v in sorted(by.items())}


def summarize(records, cards=None):
    """Upstream's per-deck summary, unchanged, so `load_boards` still reads it.

    Deliberately a copy of `tools/solve_blocking.py:summarize` rather than
    an import: that file is vendored and the shape is a contract with
    `gate.load_boards`. If it drifts, this is the place the drift shows up
    as a test failure instead of as a quietly different number.
    """
    from collections import Counter

    if cards is None:
        from ygorl.engine.duel import default_cards
        cards = default_cards()

    def nm(code):
        return cards[code].name if code in cards else str(code)

    decks = {}
    for r in records:
        row = decks.setdefault(r["deck"]["name"],
                               {"hands": 0, "solved": 0, "wall_s": 0.0,
                                "interruptions": [], "negates": [],
                                "boards": []})
        row["hands"] += 1
        row["wall_s"] += r.get("solver", {}).get("wall_s", 0.0)
        if r["status"] != "solved":
            continue
        row["solved"] += 1
        b = r["solver"].get("blocking", {})
        row["interruptions"].append(b.get("interruptions", 0))
        row["negates"].append(b.get("negates", 0))
        side = r["lines"][0]["board"]["players"][0]
        row["boards"].append({
            "hand": r["hand_index"],
            "targets": r["targets"],
            "interruptions": [f"{nm(c)} ({w}{', negate' if n else ''})"
                              for c, w, n in b.get("pieces", [])],
            "monsters": [nm(c["code"]) for c in side["mzone"]],
            "spells_traps": [nm(c["code"])
                             + ("" if c["position"] & 0x5 else " (set)")
                             for c in side["szone"]],
            "hand_left": [nm(c) for c in side["hand"]],
        })
    for row in decks.values():
        n = row["hands"]
        row["solve_rate"] = round(row["solved"] / n, 3) if n else 0.0
        row["wall_per_hand_s"] = round(row["wall_s"] / n, 1) if n else 0.0
        row["wall_s"] = round(row["wall_s"], 1)
        row["mean_interruptions"] = (
            round(sum(row["interruptions"]) / len(row["interruptions"]), 2)
            if row["interruptions"] else 0.0)
        row["top_pieces"] = Counter(
            p.split(" (")[0] for b in row["boards"]
            for p in b["interruptions"]).most_common(6)
    return decks


def sweep(decks, hands=8, draws=DRAWS, pieces=4, solve_ms=None, seed=0,
          out=None, summary=None, workers=None, log=None):
    """`engine.solve_blocking`, multi-draw. Writes JSONL and the summary."""
    say = log or (lambda m: None)
    decks = [decks] if isinstance(decks, (str, Path)) else list(decks)
    out = Path(out) if out else engine.ROOT / "results" / "boards_multi.jsonl"
    summary = Path(summary) if summary else \
        engine.ROOT / "results" / "boards_multi_summary.json"
    for d in decks:
        solve_deck(d, hands=hands, draws=draws, pieces=pieces,
                   solve_ms=solve_ms, seed=seed, out=out, workers=workers,
                   log=say)
    records = [json.loads(l) for l in out.read_text().splitlines()
               if l.strip()] if out.exists() else []
    table = summarize(records)
    summary.write_text(json.dumps({"decks": table}, indent=1))
    say(f"\n{len(records)} record(s) -> {out}")
    for name, row in sorted(table.items()):
        say(f"  {name:28} {row['solved']}/{row['hands']} solved, "
            f"mean {row['mean_interruptions']} interruptions, "
            f"{row['wall_per_hand_s']:.0f}s/hand")
    return {"decks": table}
