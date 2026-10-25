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

import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import engine, gauntlet

DRAWS = gauntlet.DRAWS

# Budget for the BREADTH round, where the job is to find out which goals
# are reachable at all rather than to squeeze the best board out of one.
#
# Measured over every attempt in the stored sweep (952 of them):
#
#   attempts that SOLVED (632): median 12.5s, mean 16.8s, max 57.5s
#   attempts that FAILED (320): median 60.2s -- a failure ALWAYS burns the
#                               whole budget, by construction
#   64% of all solves finish within 15s; 75% within 20s
#
# and 64% of the sweep's 8.2 solver-hours went to failures. So the full
# budget buys almost nothing on the breadth pass: the goals it finds are
# mostly found in the first quarter of it, and the rest is spent proving
# a negative that the depth round re-examines anyway. Goals that solve in
# triage are kept outright -- their line is verified the same way, so
# nothing is lost -- and goals that merely made progress are promoted to
# the full budget by `allocate`.
#
# NOT a licence to run the whole sweep cheap. The depth round keeps the
# full budget precisely because the borderline goals need it: a despia
# pair measured hits at 17.7s, 32.8s and 45.4s across seeds, every one of
# which a 15s budget would have called a brick.
TRIAGE_MS = 15_000


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
    try:
        demo = solve_hand(job, cards=cards)
    finally:
        # `solve_hand` removes its own `job` subdirectory, not the mkdtemp
        # ROOT we handed it, so every draw leaked one directory. Measured
        # 2026-10-24: 914 of them under /tmp going back two days, 122 MB,
        # and a sweep adds one per (hand, goal, draw) -- hundreds per run.
        shutil.rmtree(scratch, ignore_errors=True)
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


def _map_solves(batch, workers):
    """Run a wave of solves, yielding results as they finish.

    A seam, not an abstraction: the wave structure (breadth, then stacks,
    then depth) is where the sweep's decisions live, and it had no test
    because exercising it meant hours of solver. Tests replace this with a
    serial stub and check the waves, which is the part that can be wrong
    in a way no amount of solver time would reveal.
    """
    with ProcessPoolExecutor(max_workers=workers,
                             initializer=engine.be_nice) as ex:
        with _children_die_with_us(ex):
            yield from ex.map(_solve_one, batch)


@contextlib.contextmanager
def _children_die_with_us(ex):
    """Terminate the pool's workers if this process is asked to stop.

    A pool's spawn children are reparented to PID 1 when their parent
    dies and then never exit, because they sit blocked on a queue nobody
    will write to again. Measured twice on 2026-10-24: six aged six days,
    and six more aged four hours that appeared the moment one funnel
    parent was killed. `doctor` reports them now; this stops making them.

    SIGTERM and SIGINT are what a `kill` or a Ctrl-C actually sends, and
    both are catchable. SIGKILL is not, and nothing in Python can cover
    it -- a `kill -9` will still orphan the workers, which is why the
    `doctor` check stays.

    Handlers are restored on the way out, and anything already installed
    is chained rather than dropped: a caller that set its own SIGTERM
    handler must still see it.
    """
    previous = {}

    def stop(signum, frame):
        for proc in list(getattr(ex, "_processes", {}).values() or []):
            try:
                proc.terminate()
            except (OSError, AttributeError):
                pass
        old = previous.get(signum)
        if callable(old):
            old(signum, frame)
        elif old == signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, stop)
            except (ValueError, OSError):
                # Not the main thread, or the platform refuses: the
                # doctor check is the fallback.
                pass
        yield
    finally:
        for sig, old in previous.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass


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


def _breadth_goals(deck_path, hand, pieces):
    """Breadth asks about the PIECES, not the stacks.

    A target set is a conjunction, so it is monotone: if the search cannot
    place a single card of {base, A}, it cannot place {base, A, B} either
    -- the second is strictly harder. Running the prefix stacks in the
    breadth round therefore spends budget on goals whose impossibility the
    piece goals already prove, and those stacks are the expensive failures
    (a failure always burns the whole budget).

    Worse, it defeated `combinations_to_try`: that function skips goals
    already tried, so the prefix stacks AB, ABC, ABCD crowded out the
    evidence-based combinations it exists to propose. The first despia
    sweep reported "0 of them combinations the enumeration could not
    reach" for exactly this reason.

    So breadth asks only base and base+piece. The stacks come after, built
    from the pieces that actually solved.
    """
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import blocking_plan

    cards = default_cards()
    deck = load_ydk(Path(deck_path))
    plan = blocking_plan(deck.main, deck.extra, list(hand), cards)
    goals = [(tuple([*plan.base, p.target]), f"piece:{p.password}")
             for p in plan.pieces[:pieces]]
    if plan.base:
        goals.append((tuple(plan.base), "base"))
    return goals


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
        for targets, label in _breadth_goals(deck_path, hand, pieces):
            for d in range(draws):
                out.append((deck_path, name, i, hs, list(hand), list(targets),
                            label, solve_ms, d))
    return out, hand_info


def _checkpoint(per_hand, hand_info, out, draws, say):
    """Rewrite the per-hand bests so far beside `out`, as .partial.jsonl.

    Not the real output: a later round may still improve a hand, so these
    are provisional. They exist so that a run killed at round three is
    salvageable rather than wasted.
    """
    partial = Path(str(out) + ".partial")
    rows = []
    for i in sorted(per_hand):
        acc = per_hand[i]
        if not acc["best"]:
            continue
        rec = acc["best"][1]
        hs, hand = hand_info[i]
        rec.setdefault("hand", hand)
        rec["hand_index"], rec["hand_seed"] = i, hs
        rows.append(json.dumps(rec))
    if not rows:
        return
    try:
        partial.write_text("\n".join(rows) + "\n")
    except OSError as e:
        say(f"    (could not checkpoint: {e})")


def solve_deck(deck_path, hands=8, draws=DRAWS, pieces=4, solve_ms=None,
               seed=0, first_hand=0, out=None, workers=None, log=None,
               triage_ms=None, patience=None):
    """Sweep one deck's opening hands, keeping the best board per hand.

    Returns the records written this run.

    **Nothing is written until every wave has finished.** The docstring
    used to claim records append "as each hand completes, so a rerun
    resumes", and that was false: a hand's best board is not known until
    the last depth round that could improve it, so the append loop runs
    at the very end. A 20-hand sweep of the active deck sat at depth round
    three with 545 seconds of solver spent and an output file that did not
    exist yet.

    `resume_path` mitigates it: after each depth round the per-hand bests
    so far are rewritten there, so an interrupted run can be salvaged
    instead of repeated. Resumption across RUNS still works at hand
    granularity -- `done_keys` skips hands already in `out`.
    """
    from ygorl.cards.ydk import load_ydk
    from ygorl.solver.batch import done_keys

    # A sweep is tens of minutes of solver, and its progress is the only
    # sign it is alive. Python block-buffers stdout when it is redirected
    # to a file, so a `log=print` caller saw an EMPTY log for the whole
    # run -- which looks exactly like a hung job, and cost a debugging
    # detour that way.
    def say(m):
        if log:
            log(m)
            sys.stdout.flush()
    engine.require()
    engine.preflight([deck_path])
    solve_ms = engine.SOLVE_MS if solve_ms is None else solve_ms
    # Never triage ABOVE the real budget: with solve_ms below TRIAGE_MS
    # (a smoke run) a fixed 15s would make breadth the expensive round.
    triage_ms = min(TRIAGE_MS, solve_ms) if triage_ms is None \
        else min(triage_ms, solve_ms)
    # With a single draw there IS no depth round, so nothing gets promoted
    # to the full budget and triage would simply be a cheap run -- the one
    # failure mode this project names outright. Breadth takes the whole
    # budget instead.
    if draws <= 1:
        triage_ms = solve_ms
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
                                 pieces, triage_ms, 1, done)
    if not work:
        say(f"{name}: every hand already in {out.name}")
        return []
    n_hands = len({w[2] for w in work})
    n_goals = len(work) // max(1, n_hands)
    say(f"{name}: {n_hands} hand(s), {n_goals} goals each, {draws} draws "
        f"across {workers} worker(s) — breadth at {triage_ms // 1000}s, "
        f"depth at {solve_ms // 1000}s")

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
        for res in _map_solves(batch, workers):
            collect(res)
            done_n += 1
            if done_n % max(1, len(batch) // 5) == 0:
                say(f"  {phase}: {done_n}/{len(batch)} solves, "
                    f"{time.monotonic() - start:.0f}s")

    run(work, "breadth")

    # Wave two: the STACKS, built from the pieces that actually solved,
    # still at the triage budget. These are new goals, so they are asked
    # cheaply first for the same reason the pieces were.
    combine, n_combos = [], 0
    for i, acc in per_hand.items():
        hs, hand = hand_info[i]
        for targets, label in combinations_to_try(acc["attempts"]):
            n_combos += 1
            combine.append((deck_path, name, i, hs, hand, list(targets),
                            label, triage_ms, 0))
    if combine:
        say(f"  stacks: {n_combos} combination(s) of pieces that solved, "
            f"at {triage_ms // 1000}s")
        run(combine, "stacks")
    else:
        say("  stacks: no hand reached two pieces separately")

    # Wave three: the full budget, in ROUNDS, dropping each hand when its
    # board stops improving.
    #
    # A flat `draws` spends the same on a hand that peaked immediately as
    # on one still climbing. Measured on 10 despia hands at draws=4: the
    # eventual best board was already found before 59% of the solves ran
    # (95 of 160), and at one draw 9 of the 10 hands were already there.
    # Over the same hands the extra draws bought +0.10 mean interruptions
    # for 4x the solver time -- a bad trade, and one a fixed `draws` can
    # neither notice nor avoid.
    #
    # `patience=None` means NO early stopping, and that is the default
    # because the measurement did not support switching it on. Same ten
    # despia hands, all four arms:
    #
    #   single draw @60s          mean 2.50 int   14.6 solver-min
    #   two-wave,  4 draws        mean 2.60 int   61.9
    #   three-wave, 4 draws       mean 2.60 int   38.4
    #   three-wave + patience 2   mean 2.50 int   25.3
    #
    # Early stopping cut 38.4 to 25.3 minutes and gave back the entire
    # quality gain: hand 3 reached (4,3) in both flat arms and fell to
    # (3,3) with patience=2. It was set to 2 specifically to protect that
    # hand, justified from where its improvement landed in the TWO-wave
    # run -- and the three-wave restructure moved it into a different
    # round, so the reasoning did not survive its own refactor.
    #
    # Pass `patience` explicitly to trade board quality for time with your
    # eyes open. The wave restructure above is the free part (38.4 vs 61.9
    # minutes at identical 2.60), and it stays on.
    draw_n = {i: 1 for i in per_hand}
    flat = {i: 0 for i in per_hand}
    active = set(per_hand)
    for rnd in range(1, draws):
        if not active:
            say(f"  depth: every hand plateaued after round {rnd - 1}")
            break
        batch = []
        before = {}
        for i in sorted(active):
            hs, hand = hand_info[i]
            acc = per_hand[i]
            before[i] = acc["best"][0] if acc["best"] else (0, 0)
            # draws=2 asks `allocate` for exactly one round's worth.
            for targets, label in allocate(acc["attempts"], 2, n_goals):
                draw_n[i] += 1
                batch.append((deck_path, name, i, hs, hand, list(targets),
                              label, solve_ms, draw_n[i]))
        if not batch:
            say("  depth: nothing solved and nothing came close — no re-draws")
            break
        say(f"  depth round {rnd}: {len(batch)} draws at "
            f"{solve_ms // 1000}s over {len(active)} hand(s)"
            + (" still climbing" if patience is not None else ""))
        run(batch, f"depth r{rnd}")
        for i in sorted(active):
            now = per_hand[i]["best"][0] if per_hand[i]["best"] else (0, 0)
            if now > before[i]:
                flat[i] = 0
            else:
                flat[i] += 1
                # patience=None means no early stopping at all; comparing
                # against it raised TypeError the moment it became the
                # default, which is what the wave test caught.
                if patience is not None and flat[i] >= patience:
                    active.discard(i)
        say(f"    improved: {sum(1 for i in before if flat[i] == 0)}, "
            f"still climbing: {len(active)}")
        # Checkpoint the bests so far. Hours of solver with nothing on
        # disk is how a killed job loses everything.
        _checkpoint(per_hand, hand_info, out, draws, say)

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
          out=None, summary=None, workers=None, log=None, triage_ms=None,
          patience=None):
    """`engine.solve_blocking`, multi-draw. Writes JSONL and the summary."""
    say = log or (lambda m: None)
    decks = [decks] if isinstance(decks, (str, Path)) else list(decks)
    out = Path(out) if out else engine.ROOT / "results" / "boards_multi.jsonl"
    summary = Path(summary) if summary else \
        engine.ROOT / "results" / "boards_multi_summary.json"
    for d in decks:
        # Every knob `solve_deck` takes is reachable from here. A
        # parameter that exists and cannot be set is this project's
        # signature bug -- `engine.duel`'s arguments were audited for
        # exactly this, and the fire list was written and then dropped.
        solve_deck(d, hands=hands, draws=draws, pieces=pieces,
                   solve_ms=solve_ms, seed=seed, out=out, workers=workers,
                   log=say, triage_ms=triage_ms, patience=patience)
    records = [json.loads(l) for l in out.read_text().splitlines()
               if l.strip()] if out.exists() else []
    table = summarize(records)
    summary.write_text(json.dumps({"decks": table}, indent=1))
    say(f"\n{len(records)} record(s) -> {out}")
    for name, row in sorted(table.items()):
        say(f"  {name:28} {row['solved']}/{row['hands']} solved, "
            f"mean {row['mean_interruptions']} interruptions per solved hand, "
            f"{row['wall_per_hand_s']:.0f}s/hand")
    return {"decks": table}

def draws_curve(jsonl_path, deck_name=None):
    """What each additional draw actually bought, per deck.

    The budget has `ladder`; `draws` had nothing, so `DRAWS` was a guess
    applied to every deck. Measured on despia, four draws bought +0.10
    mean interruptions for 2.6x the solver time with one hand of ten
    improving; on an elfnote hand the same machinery beat the stored
    library outright. Two decks, opposite answers -- so the value of
    draws is a per-deck question and needs measuring like the budget.

    This costs NO solver time: every record stores each attempt with its
    draw index, so the running best over the order the solves were RUN is
    exact and recoverable after the fact.

    Returns {deck: {"hands", "best", "at_draw", "wasted", "solves"}} where
    `at_draw[k]` is how many hands had already reached their eventual best
    board by the end of draw k.
    """
    out = {}
    for raw in Path(jsonl_path).read_text().splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        d = rec.get("deck")
        name = d.get("name") if isinstance(d, dict) else str(d)
        if deck_name and name != deck_name:
            continue
        b = (rec.get("solver") or {}).get("blocking") or {}
        atts = b.get("attempts") or []
        if not atts:
            continue
        row = out.setdefault(name, {"hands": 0, "solves": 0, "best": [],
                                    "at_draw": {}, "wasted": 0})
        row["hands"] += 1
        row["solves"] += len(atts)
        # The order they ran: breadth (draw 0) first, then the depth rounds.
        order = sorted(atts, key=lambda a: (a.get("draw", 0),
                                            a.get("label", "")))
        best, first_at = (0, 0), None
        by_draw = {}
        for i, a in enumerate(order, 1):
            if a.get("status") == "solved":
                k = (a.get("interruptions") or 0, a.get("negates") or 0)
                if k > best:
                    best, first_at = k, i
            by_draw[a.get("draw", 0)] = best[0]
        row["best"].append(best[0])
        row["wasted"] += len(order) - (first_at or len(order))
        for k, v in by_draw.items():
            row["at_draw"].setdefault(k, []).append(v == best[0])
    for row in out.values():
        row["mean_best"] = (round(sum(row["best"]) / len(row["best"]), 2)
                            if row["best"] else 0.0)
        row["at_draw"] = {k: f"{sum(v)}/{len(v)}"
                          for k, v in sorted(row["at_draw"].items())}
        row["wasted_share"] = (round(row["wasted"] / row["solves"], 3)
                               if row["solves"] else 0.0)
    return out
