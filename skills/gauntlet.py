"""Rank your end boards by what they actually STOP.

The ladder in `handlab.solve_for_hand` picks a board with a hand-written
score -- interruptions, then negations, counted flat. That score says a
Droll held in hand is worth a negate on the field, and it has no opinion
at all about which of two 3-interruption boards is harder to play
against. The question it is standing in for is answerable instead of
guessable: deal the meta a going-second hand and see whether it still
assembles its payoff.

So the objective here is MEASURED. One hand of yours produces several
distinct end boards; each board is put in front of several opposing
decks, each dealt several going-second hands; the board that stops the
most hands wins.

Three facts shape the implementation, and each one was measured rather
than assumed:

1. **The solver is a randomized search.** Same hand, same target set,
   same 60 s budget, different solver seed: an elfnote hand returned end
   boards of 3, 3, 3, 3, 5 and 6 interruptions over six draws, and a
   despia pair solved on 3 of 6 draws and reported "no line" on the
   other 3. duelroom took exactly one draw per goal and believed it.
   Drawing several seeds is therefore both how the board gets better and
   how the three CANDIDATES are generated in the first place.

2. **Breaking a board is not expressible as a solver goal.**
   `--board-remove` relaxes the goal instead of demanding destruction
   (see `breaking.going_second_args`). What is expressible, and is the
   better question anyway, is whether the opponent going second still
   reaches their own payoff. "Stopped" here means their engine did not
   function through your board -- not that you survived at life points.
   There is no OTK check anywhere in this module.

3. **The opponent's search is the same randomized search**, so a single
   draw on the defence side is a coin flip too. The opponent gets
   best-of-N: a hand counts as getting through if ANY draw gets through.
   That is deliberately generous -- a human holding those six cards gets
   more than one think -- and it means a board only scores well when
   nothing found a line, which is the direction an honest ranking should
   err in.
"""

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import breaking, engine

# Draws per goal when generating candidate boards. Four is where the
# measured spread stops paying: the elfnote hand above reached its best
# board (6 interruptions) once in six draws, and best-of-4 recovers most
# of that, while every draw is a full solver budget.
DRAWS = 4

# Draws the OPPONENT gets per going-second hand. Their search carries the
# same variance, and the measured P(solve) on a borderline goal was 50%
# per 60 s draw, so one draw would make "stopped" a coin flip.
DEFENCE_DRAWS = 3


@dataclass
class Candidate:
    """One distinct end board reached from the hand, and how to rebuild it."""

    targets: tuple          # the goal that produced it (a conjunction)
    label: str              # which rung of the ladder it came from
    interruptions: int      # the old heuristic score, kept for comparison
    negates: int
    pieces: tuple           # (code, where, negate) of what the board interrupts with
    draws: int              # how many of the draws reached this board
    solve_s: float

    def key(self):
        return (self.interruptions, self.negates)

    def board_key(self):
        """Identity of the BOARD, so two draws of the same thing collapse."""
        return tuple(sorted((c, w, bool(n)) for c, w, n in self.pieces))

    def to_json(self):
        return {"targets": list(self.targets), "label": self.label,
                "interruptions": self.interruptions, "negates": self.negates,
                "pieces": [list(p) for p in self.pieces],
                "draws": self.draws, "solve_s": round(self.solve_s, 1)}


def candidate_boards(deck_path, hand, keep=3, draws=DRAWS, pieces=4,
                     solve_ms=None, threads=1, log=None):
    """The `keep` most distinct end boards this hand can reach (all, if None).

    Every goal on the ladder is solved `draws` times with different
    solver seeds, and distinct end boards are collected rather than only
    the best one -- the point is to produce ALTERNATIVES to test, not a
    single winner picked by the score being replaced.
    """
    from concurrent.futures import ProcessPoolExecutor

    say = log or (lambda m: None)
    solve_ms = engine.SOLVE_MS if solve_ms is None else solve_ms
    goals = _goals(deck_path, hand, pieces)
    say(f"{len(goals)} goal(s) on the ladder, {draws} draws each "
        f"= {len(goals) * draws} solves of {solve_ms // 1000}s")

    work = [(deck_path, hand, g, label, solve_ms, 7000 + i, threads)
            for g, label in goals for i in range(draws)]
    found = {}
    with ProcessPoolExecutor(max_workers=engine.DEFAULT_WORKERS,
                             initializer=engine.be_nice) as ex:
        for res in ex.map(_one_draw, work):
            if res is None:
                continue
            cand = Candidate(**res)
            key = cand.board_key()
            if key in found:
                found[key].draws += 1
                found[key].solve_s += cand.solve_s
            else:
                found[key] = cand
    out = sorted(found.values(), key=lambda c: (-c.key()[0], -c.key()[1],
                                                -c.draws))
    cut = len(out) if keep is None else min(keep, len(out))
    say(f"{len(out)} distinct board(s) reached"
        + ("" if keep is None else f"; keeping {cut}"))
    for c in out[:cut]:
        say(f"  {c.label:16s} {c.interruptions} int / {c.negates} neg "
            f"({c.draws}/{draws * len(goals)} draws)")
    return out if keep is None else out[:keep]


def _goals(deck_path, hand, pieces):
    """The ladder's goals for this hand: base, base+piece, and the climb.

    Mirrors `handlab.solve_for_hand`'s plan, but enumerated UP FRONT so
    every rung can be drawn in parallel instead of the climb waiting on
    the rung below it. The climb's dependency -- only stack what already
    solved -- is traded away on purpose: a goal that cannot be reached
    costs one wasted budget, and the cores were idle anyway.
    """
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import blocking_plan

    cards = default_cards()
    deck = load_ydk(Path(deck_path))
    plan = blocking_plan(deck.main, deck.extra, list(hand), cards)
    goals = []
    heads = [p.target for p in plan.pieces[:pieces]]
    for p in heads:
        goals.append((tuple([*plan.base, p]), f"piece:{p}"))
    # The stacks, built off the best-ranked head rather than off whatever
    # happened to solve first. `deck_pieces` already ranks engine pieces
    # and negations first, so this is the ladder's own preference order.
    for n in range(2, len(heads) + 1):
        goals.append((tuple([*plan.base, *heads[:n]]), f"stack:{n}"))
    if plan.base:
        goals.append((tuple(plan.base), "base"))
    return goals


def _one_draw(args):
    """One (goal, seed) solve. Top level so the spawn pool can pickle it."""
    deck_path, hand, targets, label, solve_ms, seed, threads = args
    from ygorl.engine.duel import default_cards
    from ygorl.solver import HandJob, Workdir, find_solver
    from ygorl.solver.batch import solve_hand
    from ygorl.solver.blocking import board_interruptions

    cards = default_cards()
    scratch = Path(tempfile.mkdtemp(prefix="duelroom-draw-"))
    job = HandJob(
        deck_path=Path(deck_path).resolve(), hand_index=0, hand_seed=seed,
        targets=tuple(targets), workdir=Workdir.create(scratch / "wd").path,
        scratch=scratch / "job", solve_ms=solve_ms, threads=threads,
        hand=tuple(hand), lines=4, binary=find_solver(), solver_seed=seed,
        timeout_s=solve_ms / 1000 + 300)
    try:
        demo = solve_hand(job, cards=cards)
    finally:
        # Same leak as sweep._solve_one: solve_hand cleans its own `job`
        # subdir, never the mkdtemp root.
        shutil.rmtree(scratch, ignore_errors=True)
    if demo.status != "solved" or not demo.lines:
        return None
    scored = [(board_interruptions(ln.board, cards), ln) for ln in demo.lines]
    score, _ = max(scored, key=lambda t: t[0].key())
    return {"targets": tuple(targets), "label": label,
            "interruptions": score.interruptions, "negates": score.negates,
            "pieces": tuple(tuple(p) for p in score.pieces), "draws": 1,
            "solve_s": demo.solver.get("wall_s", 0.0)}


# --- putting a board in front of the meta --------------------------------

def our_board_start(our_deck, their_deck, our_hand, our_targets, seed,
                    scratch, solve_ms=None):
    """A `--start` replay where WE have built `our_targets` and passed.

    `breaking.build_start` does this for the opponent's board, from a
    sampled hand and a single target. Two things have to change to test a
    CANDIDATE board: the hand is the one the user is actually holding, not
    one sampled from a seed, and the goal is the candidate's full
    conjunction rather than one card. Everything else -- the template
    carrying both real decks, and re-emitting the line with the turn
    closed so `--start` accepts it -- is load-bearing and unchanged; see
    `breaking.build_start` for why each step is there.

    LIMITATION, stated because it is easy to misread the output: what gets
    tested is a board CONTAINING the candidate's targets, not the exact
    board `candidate_boards` saw. The candidate was found against
    PASSIVE_OPPONENT and has to be re-solved here against the real deck,
    and a target conjunction pins only the named cards -- whatever else
    the line happens to leave behind can differ. The comparison between
    candidates is therefore a comparison between GOALS, which is the
    thing a pilot can actually choose, and the interruption counts carried
    on the candidate are from the earlier solve.
    """
    from ygorl.engine.replay import Replay, load_yrp
    from ygorl.solver.combo_solver import Workdir
    from ygorl.solver.demo import convert_line

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    wd = Workdir.create(scratch / "workdir")
    sols = scratch / "sol"
    sols.mkdir(exist_ok=True)

    # shuffle=True for the same reason as `inert_start`: it is what makes
    # `seed` vary the opposing hand. Our own hand is unaffected -- the
    # solver substitutes it from --hand.
    tpl = breaking.template_vs(scratch / "tpl.yrpX", our_deck, their_deck,
                               seed=seed, shuffle=True)
    cmd = [str(engine.SOLVER_BIN), str(tpl), "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    cmd += ["--no-ref", "--deck", str(Path(our_deck).resolve()),
            "--hand", "|".join(map(str, our_hand))]
    for t in our_targets:
        cmd += ["--target", str(t)]
    cmd += ["--solve-ms", str(solve_ms or engine.SOLVE_MS),
            "--threads", "1", "--outdir", str(sols)]
    breaking._run_solver(cmd, f"our_board_start({Path(our_deck).stem})")

    found = sorted(sols.glob("*.yrp"))
    if not found:
        return None
    line = convert_line(load_yrp(found[0]), [str(t) for t in our_targets],
                        close_turn=True)
    base = Replay.from_yrp(found[0])
    ext = Replay(seed=base.seed, first=base.first, rule_flags=base.rule_flags,
                 player=base.player, shuffle_decks=base.shuffle_decks,
                 decks=base.decks, responses=list(line.responses),
                 seed_words=base.seed_words)
    start = scratch / f"start_{seed}.yrpX"
    ext.to_yrpx(start, names=("us", "them"))
    return start


# A card with no effect at all, so a board holding one interrupts nothing
# -- AND summonable from a hand of nothing else, which is the half the
# first version got wrong. It used Blue-Eyes White Dragon, ygorl's own
# PASSIVE_OPPONENT card, which is level 8: a hand of five of them cannot
# normal summon one without two tributes, so the floor board never built
# and all three decks reported "inert board not built". A floor that
# cannot be built measures nothing, and it fails in exactly the shape of
# a real result.
INERT = 43096270  # Alexandrite Dragon -- Normal Monster, level 4, no script


def inert_start(their_deck, seed, scratch, solve_ms=None):
    """A `--start` replay where player 0 built a board that DOES NOTHING.

    The `base` rung is not a floor, which is the trap this exists to
    close. "Do nothing, hold what the hand holds" sounded like the
    do-nothing control, and then the hand it holds turns out to be Droll
    and Effect Veiler -- two live interactions on the opponent's turn. A
    board scoring the same as that control is not a board that achieves
    nothing; it is a board that achieves as much as two held hand traps.

    So the real floor is a deck of 40 vanilla monsters that normal summons
    one and passes: a body on the field, zero interruption anywhere. The
    opponent's stop rate against THIS is how hard their own turn-2 task
    is at this budget, independent of anything you did. Without that
    number a stop rate has no zero -- and `gauntlet`'s first two real
    boards both stopped 18/18, which is either two excellent boards or a
    task nobody could complete.

    Built the same way as `breaking.floodgate_start`, which does this with
    a deck of 40 floodgates.
    """
    from ygorl.engine.duel import Deck as YDeck
    from ygorl.engine.replay import Replay, load_yrp
    from ygorl.solver.combo_solver import Workdir
    from ygorl.solver.demo import convert_line

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    wd = Workdir.create(scratch / "workdir")
    sols = scratch / "sol"
    sols.mkdir(exist_ok=True)

    inert = YDeck((INERT,) * 40, (), (), "inert")
    ydk = scratch / "inert.ydk"
    ydk.write_text("#created by duelroom\n#main\n"
                   + "\n".join([str(INERT)] * 40) + "\n#extra\n!side\n")

    # shuffle=True so `seed` actually deals player 1 a hand. Without it
    # the hand is the tail of their decklist, the same for every seed.
    tpl = breaking.template_vs(scratch / "tpl.yrpX", inert, their_deck,
                               seed=seed, shuffle=True)
    cmd = [str(engine.SOLVER_BIN), str(tpl), "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    cmd += ["--no-ref", "--deck", str(ydk.resolve()),
            "--hand", "|".join([str(INERT)] * 5),
            "--target", f"{INERT}@atk",
            "--solve-ms", str(solve_ms or engine.SOLVE_MS),
            "--threads", "1", "--outdir", str(sols)]
    breaking._run_solver(cmd, f"inert_start(seed {seed})")

    found = sorted(sols.glob("*.yrp"))
    if not found:
        return None
    line = convert_line(load_yrp(found[0]), [f"{INERT}@atk"], close_turn=True)
    base = Replay.from_yrp(found[0])
    ext = Replay(seed=base.seed, first=base.first, rule_flags=base.rule_flags,
                 player=base.player, shuffle_decks=base.shuffle_decks,
                 decks=base.decks, responses=list(line.responses),
                 seed_words=base.seed_words)
    out = scratch / f"inert_{seed}.yrpX"
    ext.to_yrpx(out, names=("inert", "them"))
    return out


def floor(their_decks, hands=4, draws=DEFENCE_DRAWS, solve_ms=None,
          scratch=None, log=None):
    """How often each opposing deck gets through a board that does nothing.

    The zero point for every stop rate `defend` reports. A board that
    stops 18 of 18 means nothing until this says the floor stops fewer.
    Same hands and same draws as `defend`, so the two are comparable.
    """
    from concurrent.futures import ProcessPoolExecutor

    from ygorl.solver.batch import sample_hand

    say = log or (lambda m: None)
    solve_ms = engine.SOLVE_MS if solve_ms is None else solve_ms
    scratch = Path(scratch or tempfile.mkdtemp(prefix="duelroom-floor-"))
    work, rows = [], []
    for deck_path, target in their_decks:
        name = Path(deck_path).stem
        theirs = breaking.to_ydeck(deck_path)
        # The start replay carries player 1's deck AND the hand the core
        # deals them; `--deck` and `--hand` do not reach player 1. Proven
        # 2026-10-24: the solver dealt the last five entries of the
        # ORIGINAL .ydk even when `--deck` pointed at a rewritten copy. So
        # the reordered decklist has to go into the REPLAY, which means
        # one board build per hand -- exactly what
        # `breaking.going_second` already did before `defend` "optimised"
        # it into one build per deck.
        for h in range(hands):
            their_hand, _ = sample_hand(theirs, seed=2000 + h, size=6)
            sd = scratch / f"{name}_h{h}"
            sd.mkdir(parents=True, exist_ok=True)
            # The SEED deals the opposing hand now that the template
            # shuffles. `deck_with_hand` is no longer how the hand is
            # chosen -- the shuffle would undo the ordering anyway -- so
            # the deck goes in unmodified and the seed varies per hand.
            dealt = deck_path
            try:
                start = inert_start(dealt, seed=3000 + h,
                                    scratch=sd / "floor", solve_ms=solve_ms)
            except breaking.SolverFailed as e:
                say(f"  {name} hand {h}: floor board failed — {e}")
                rows.append({"deck": name, "hand": None, "error": str(e)})
                continue
            if start is None:
                say(f"  {name} hand {h}: could not summon a vanilla monster "
                    f"— the floor itself is broken, not the deck")
                rows.append({"deck": name, "hand": None,
                             "error": "inert board not built"})
                continue
            for d in range(draws):
                work.append((start, dealt, their_hand, target, solve_ms,
                             5000 + d, name, h))

    say(f"  floor: {len(work)} going-second searches against a board that "
        f"does nothing")
    results = {}
    with ProcessPoolExecutor(max_workers=engine.DEFAULT_WORKERS,
                             initializer=engine.be_nice) as ex:
        for (args, res) in zip(work, ex.map(_attack, [w[:6] for w in work])):
            acc = results.setdefault((args[6], args[7]),
                                     {"through": 0, "searched": 0})
            acc["through"] += bool(res["through"])
            acc["searched"] += bool(res["searched"])
    for (name, h), acc in sorted(results.items()):
        rows.append({"deck": name, "hand": h, "through": acc["through"] > 0,
                     "searched": acc["searched"]})
    tested = [r for r in rows if r.get("hand") is not None]
    through = sum(1 for r in tested if r["through"])
    # A floor that stops EVERYTHING is the known solver defect, not a
    # result. Established 2026-10-24: the going-second mode never summons
    # from the extra deck. kewl-tune reaches its target in 10 of 12 hands
    # going FIRST and 0 of 3 going second against one vanilla monster --
    # and 0 of 3 for the CHEAPEST extra-deck monster in its deck too (a
    # level 4 Synchro), with the turn-cut patch applied so the rollouts
    # run freely. The solver says it itself: "no extra-deck monster
    # summoned" over 38,871 rollouts.
    if tested and through == 0:
        say("  ! a board that does NOTHING stopped every hand. That is the")
        say("    known going-second defect, not a measurement: the mode never")
        say("    summons from the extra deck (see ISSUES.md #4). Treat every")
        say("    stop rate from this run as meaningless.")
    return {"rows": rows, "tested": len(tested), "through": through,
            "stopped": len(tested) - through,
            # True when the floor stopped everything -- the signature of
            # the defect, so callers can refuse rather than report.
            "floor_is_broken": bool(tested) and through == 0,
            "stop_rate": (len(tested) - through) / len(tested)
            if tested else None}


def _attack(args):
    """One opposing going-second hand against a fixed start. Pool entry."""
    start, their_deck, their_hand, their_target, solve_ms, seed = args
    import subprocess

    from ygorl.solver.combo_solver import Workdir

    scratch = Path(tempfile.mkdtemp(prefix="duelroom-atk-"))
    wd = Workdir.create(scratch / "workdir")
    out = scratch / "gs"
    out.mkdir(exist_ok=True)
    # `--hand` never reaches player 1 -- the core draws off the end of the
    # decklist and nothing is shuffled. Put the hand where it will be
    # drawn. See breaking.deck_with_hand for the proof.
    dealt = breaking.deck_with_hand(their_deck, their_hand,
                                    scratch / "dealt.ydk")
    args_ = breaking.going_second_args(start, dealt, their_hand,
                                       their_target, solve_ms)
    cmd = [str(engine.SOLVER_BIN), args_[0], "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    cmd += args_[1:] + ["--threads", "1", "--seed", str(seed),
                        "--outdir", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=engine.env())
    wrote = list(out.glob("*.yrp"))
    # KEEP the solver's report. Throwing it away is why "0 of 72 hands got
    # through" could not be explained: the report prints the goal it is
    # actually chasing ("--- target board POSTED/EDITED ---") and the
    # starting position, which is the only direct evidence of what this
    # mode is doing. `ygorl.solver.batch` writes a solver.log for the same
    # reason; this path did not, and every investigation of it was blind.
    log = scratch / "solver.log"
    try:
        log.write_text((r.stdout or "") + "\n--- stderr ---\n"
                       + (r.stderr or ""), encoding="utf-8")
    except OSError:
        log = None
    return {"searched": breaking.searched(r.stdout or ""),
            "through": bool(wrote), "returncode": r.returncode,
            "log": str(log) if log else None,
            # The one line that says what the goal was, so a caller does
            # not have to open the file to find out.
            "target_mode": next((ln.strip() for ln in (r.stdout or "").splitlines()
                                 if "target board" in ln), None)}


def defend(our_deck, our_hand, candidate, their_decks, hands=4,
           draws=DEFENCE_DRAWS, solve_ms=None, scratch=None, log=None):
    """Put one candidate board in front of the meta.

    `their_decks` is [(deck_path, payoff_target)]. Returns a dict with a
    row per (deck, hand) and the share of hands the board stopped.

    The board is built ONCE per opposing deck and reused across their
    hands -- the whole point is to compare boards, so the board cannot be
    allowed to vary between the hands that are testing it.
    """
    from concurrent.futures import ProcessPoolExecutor

    from ygorl.solver.batch import sample_hand

    say = log or (lambda m: None)
    solve_ms = engine.SOLVE_MS if solve_ms is None else solve_ms
    scratch = Path(scratch or tempfile.mkdtemp(prefix="duelroom-gauntlet-"))
    rows, work = [], []
    for deck_path, target in their_decks:
        name = Path(deck_path).stem
        theirs = breaking.to_ydeck(deck_path)
        # One board build per HAND, not per deck. The start replay carries
        # player 1's deck and the hand the core deals them -- `--deck` and
        # `--hand` do not reach player 1 (see breaking.deck_with_hand), so
        # the only way to vary the opposing hand is to put a reordered
        # decklist into the REPLAY. `breaking.going_second` always did it
        # this way; building the start once per deck and varying `--hand`
        # was my optimisation, and it silently made `hands=N` one hand
        # tested N times.
        for h in range(hands):
            their_hand, _ = sample_hand(theirs, seed=2000 + h, size=6)
            sd = (scratch / f"{name}_h{h}_"
                  f"{abs(hash(candidate.board_key())) % 10**8}")
            sd.mkdir(parents=True, exist_ok=True)
            # Same as `floor`: the seed deals their hand now.
            dealt = deck_path
            try:
                start = our_board_start(our_deck, dealt, our_hand,
                                        candidate.targets, seed=3000 + h,
                                        scratch=sd, solve_ms=solve_ms)
            except breaking.SolverFailed as e:
                say(f"  {name} hand {h}: SOLVER FAILED building our board — {e}")
                rows.append({"deck": name, "hand": None, "error": str(e)})
                continue
            if start is None:
                # The candidate was reached against the PASSIVE opponent and
                # not against this deck. A real finding, not an error.
                say(f"  {name} hand {h}: our board did not come together "
                    f"against this deck")
                rows.append({"deck": name, "hand": None,
                             "error": "board not reached vs this deck"})
                continue
            for d in range(draws):
                work.append((start, dealt, their_hand, target, solve_ms,
                             5000 + d, name, h))

    say(f"  {len(work)} going-second searches "
        f"({hands} hands x {draws} draws x {len(their_decks)} deck(s))")
    results = {}
    with ProcessPoolExecutor(max_workers=engine.DEFAULT_WORKERS,
                             initializer=engine.be_nice) as ex:
        for (args, res) in zip(work, ex.map(_attack, [w[:6] for w in work])):
            name, h = args[6], args[7]
            acc = results.setdefault((name, h), {"searched": 0, "through": 0,
                                                 "draws": 0, "failed": 0})
            acc["draws"] += 1
            acc["searched"] += bool(res["searched"])
            acc["through"] += bool(res["through"])
            acc["failed"] += res["returncode"] != 0

    for (name, h), acc in sorted(results.items()):
        # Best-of-N for the OPPONENT: any draw through counts as through.
        rows.append({"deck": name, "hand": h, "through": acc["through"] > 0,
                     "draws": acc["draws"], "searched": acc["searched"],
                     "solver_failed": acc["failed"]})
    tested = [r for r in rows if r.get("hand") is not None]
    stopped = sum(1 for r in tested if not r["through"])
    return {"candidate": candidate.to_json(), "rows": rows,
            "tested": len(tested), "stopped": stopped,
            "stop_rate": stopped / len(tested) if tested else None}


def rank(our_deck, our_hand, their_decks, keep=3, hands=4, draws=DRAWS,
         defence_draws=DEFENCE_DRAWS, solve_ms=None, log=None):
    """The whole thing: find the candidate boards, then rank them by what
    they stop rather than by the heuristic score.

    The `base` rung -- set the hand's traps, keep its hand traps, build
    nothing -- is carried as a CONTROL whenever the hand has one, even if
    it does not make the top `keep` on the old score. Without it a stop
    rate has no scale: "this board stopped 8 of 12 hands" is only a
    statement about the board if the do-nothing board stops fewer. The
    first version of this ranked three real boards, every one of them
    stopped everything at a 20 s attacker budget, and the output looked
    like a result. Measuring emptiness is the characteristic failure of
    this project, so the control is not optional.
    """
    say = log or (lambda m: None)
    found = candidate_boards(our_deck, our_hand, keep=None, draws=draws,
                             solve_ms=solve_ms, log=say)
    if not found:
        return {"hand": list(our_hand), "candidates": [], "results": []}
    cands = found[:keep]
    control = next((c for c in found if c.label == "base"), None)
    if control is not None and control not in cands:
        cands.append(control)
        say(f"  + control [base]: {control.interruptions} int "
            f"(do nothing, hold what the hand holds)")
    elif control is None:
        say("  ! no `base` rung for this hand — no control board available")

    out = []
    for i, c in enumerate(cands, 1):
        tag = " (control)" if c is control else ""
        say(f"\nboard {i}/{len(cands)}: {c.interruptions} int / {c.negates} neg"
            f"  [{c.label}]{tag}")
        res = defend(our_deck, our_hand, c, their_decks, hands=hands,
                     draws=defence_draws, solve_ms=solve_ms, log=say)
        res["control"] = c is control
        # Say it as it lands. A full run is tens of minutes of solver and
        # the caller prints the table only at the end, so without this the
        # whole middle of the run looks like nothing is happening.
        say(f"  -> stopped {res['stopped']}/{res['tested']} hands"
            if res["tested"] else "  -> no hand could be tested")
        out.append(res)
    out.sort(key=lambda r: (-(r["stop_rate"] if r["stop_rate"] is not None
                              else -1), -r["candidate"]["interruptions"]))
    # The zero point, measured the same way. Without it "stopped 18 of 18"
    # cannot be told apart from "nobody could have done it anyway".
    say("\nfloor: the same hands against a board that does nothing")
    fl = floor(their_decks, hands=hands, draws=defence_draws,
               solve_ms=solve_ms, log=say)
    say(f"  -> the floor stops {fl['stopped']}/{fl['tested']}")

    if fl.get("floor_is_broken"):
        say("\n  !! NO RANKING POSSIBLE. The floor -- a board of vanilla")
        say("     monsters -- stopped every hand, which is the known")
        say("     going-second defect (ISSUES.md #4: the mode never summons")
        say("     from the extra deck). Nothing below measures board strength.")
    rates = {r["stop_rate"] for r in out if r["stop_rate"] is not None}
    best = max((r["stop_rate"] for r in out
                if r["stop_rate"] is not None), default=None)
    return {"deck": Path(our_deck).stem, "hand": list(our_hand),
            "candidates": [c.to_json() for c in cands], "results": out,
            "discriminated": len(rates) > 1,
            "control_rate": next((r["stop_rate"] for r in out
                                  if r.get("control")), None),
            "floor": fl, "floor_rate": fl["stop_rate"],
            # A board is worth something only if it beats a board that does
            # nothing. Equal to the floor means the opponents' own turn-2
            # task was the obstacle, not anything you built.
            "beats_floor": (best is not None and fl["stop_rate"] is not None
                            and best > fl["stop_rate"])}


def save(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1))
    return path
