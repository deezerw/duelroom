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
   `--board-remove` relaxes the goal instead of demanding destruction.
   What is expressible, and is the better question anyway, is whether the
   opponent going second still reaches their own payoff. "Stopped" here
   means their engine did not function through your board -- not that you
   survived at life points. There is no OTK check anywhere in this module.

   THAT QUESTION IS UNSUPPORTED on this solver (ISSUES.md #4), and the
   half of this module that asks it -- `defend` and `rank` -- says so on
   every call. `rank_boards` at the bottom is the ranking that works: it
   asks the same thing of `--fire`, which measures over forty hands per
   cell whether a deck's own line survives one of your interruptions.

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
import zlib
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
    from . import remote
    remote.route_ygorl()     # the job's device (a no-op on the server)
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

def _attack_one(args):
    """One going-second search. Top level so the pool can pickle it."""
    start, prefix, target, solve_ms, seed, scratch, tag, deck, hand = args
    res = breaking.attack(start, prefix, target, solve_ms=solve_ms, seed=seed,
                          threads=1, scratch=scratch)
    res.update({"tag": tag, "deck": deck, "hand": hand})
    return res


def defend(our_deck, our_hand, candidate, their_decks, hands=4,
           draws=DEFENCE_DRAWS, solve_ms=None, scratch=None, log=None):
    """Put one candidate board in front of the meta, with its own control.

    `their_decks` is [(deck_path, payoff_target)]. Every position is built
    twice from the SAME template seed -- once with the board, once with us
    passing and doing nothing -- so the two differ only in the board. That
    is what the 40-vanilla-monster floor could never be: it changed the
    decks, the seed and the attacker's hand at once.

    EVERY NUMBER HERE IS UNSUPPORTED. The position is right now, and
    `breaking.check_root` proves it per position; the SEARCH from it is
    what is broken. See `breaking.UNSUPPORTED`.
    """
    from concurrent.futures import ProcessPoolExecutor

    say = log or (lambda m: None)
    solve_ms = engine.SOLVE_MS if solve_ms is None else solve_ms
    scratch = Path(scratch or tempfile.mkdtemp(prefix="duelroom-gauntlet-"))
    work, rows, built = [], [], {}
    for deck_path, target in their_decks:
        name = Path(deck_path).stem
        for h in range(hands):
            # crc32, not hash(): Python salts hash() of anything holding a
            # str per process, so a scratch directory could not be matched
            # back to the candidate that made it -- exactly what you want
            # when a search misbehaves.
            tagdir = (scratch / f"{name}_h{h}_"
                      f"{zlib.crc32(repr(candidate.board_key()).encode()) % 10**8}")
            for tag, want_board in (("board", True), ("control", False)):
                try:
                    pos = breaking.second_position(
                        our_deck, deck_path, candidate.targets, seed=3000 + h,
                        scratch=tagdir / tag, solve_ms=solve_ms,
                        board=want_board, builder_hand=our_hand)
                except (breaking.SolverFailed, breaking.RootNotPlayable) as e:
                    say(f"  {name} hand {h} [{tag}]: {e}")
                    rows.append({"deck": name, "hand": None, "tag": tag,
                                 "error": str(e)})
                    continue
                if pos is None:
                    # The candidate was reached against the PASSIVE
                    # opponent and not against this deck. A real finding.
                    say(f"  {name} hand {h} [{tag}]: our board did not come "
                        f"together against this deck")
                    rows.append({"deck": name, "hand": None, "tag": tag,
                                 "error": "board not reached vs this deck"})
                    continue
                st, pre, root = pos
                built[(name, h, tag)] = root
                for d in range(draws):
                    work.append((st, pre, target, solve_ms, 5000 + d,
                                 tagdir / tag / f"atk{d}", tag, name, h))

    say(f"  {len(work)} going-second searches ({hands} hands x {draws} draws "
        f"x {len(their_decks)} deck(s) x board+control)")
    acc = {}
    with ProcessPoolExecutor(max_workers=engine.DEFAULT_WORKERS,
                             initializer=engine.be_nice) as ex:
        for res in ex.map(_attack_one, work):
            cell = acc.setdefault((res["deck"], res["hand"], res["tag"]),
                                  {"through": 0, "searched": 0, "draws": 0,
                                   "failed": 0})
            cell["draws"] += 1
            cell["through"] += bool(res["through"])
            cell["searched"] += bool(res["searched"])
            cell["failed"] += res["returncode"] != 0

    for (name, h, tag), cell in sorted(acc.items()):
        root = built.get((name, h, tag)) or {}
        rows.append({
            "deck": name, "hand": h, "tag": tag,
            # Best-of-N for the OPPONENT: any draw through counts as
            # through. Deliberately generous -- a human holding those six
            # cards gets more than one think -- so a board only scores
            # well when nothing found a line.
            "through": cell["through"] > 0, "draws": cell["draws"],
            "searched": cell["searched"], "solver_failed": cell["failed"],
            "board_cards": root.get("board_cards"),
            "attacker_hand": root.get("attacker_hand")})

    def rate(tag):
        tested = [r for r in rows
                  if r.get("hand") is not None and r.get("tag") == tag]
        if not tested:
            return None, 0, 0
        stopped = sum(1 for r in tested if not r["through"])
        return stopped / len(tested), stopped, len(tested)

    board_rate, stopped, tested = rate("board")
    ctrl_rate, c_stopped, c_tested = rate("control")
    return {"candidate": candidate.to_json(), "rows": rows,
            "tested": tested, "stopped": stopped, "stop_rate": board_rate,
            "control_tested": c_tested, "control_stopped": c_stopped,
            "control_rate": ctrl_rate,
            # A board is worth something only if it stops more than the
            # same position with nothing on the field. Equal means the
            # attacker's own turn-2 task was the obstacle.
            "beats_control": (board_rate is not None and ctrl_rate is not None
                              and board_rate > ctrl_rate)}


def rank(our_deck, our_hand, their_decks, keep=3, hands=4, draws=DRAWS,
         defence_draws=DEFENCE_DRAWS, solve_ms=None, log=None):
    """Find the candidate boards, then rank them by what they stop.

    UNSUPPORTED on this solver -- `rank_boards` is the ranking that works.
    Kept, and kept correct, because the question it asks is the right one
    and the only thing missing is a search that can answer it: see
    `breaking.UNSUPPORTED` and ISSUES.md #4. When that is fixed this
    becomes the measurement again with no redesign.

    Each candidate now carries its OWN paired control -- the same
    position with us passing -- so the separate 40-vanilla-monster floor
    is gone. That floor changed the decks, the seed and the attacker's
    hand all at once, so a difference against it was never attributable
    to the board.

    The `base` rung -- build nothing, keep what the hand holds -- is
    still carried as a candidate whenever the hand has one, because "a
    board that achieves as much as two held hand traps" is a real
    comparison and the heuristic score does not make it.
    """
    say = log or (lambda m: None)
    say("  ! UNSUPPORTED: " + breaking.UNSUPPORTED)
    found = candidate_boards(our_deck, our_hand, keep=None, draws=draws,
                             solve_ms=solve_ms, log=say)
    if not found:
        return {"hand": list(our_hand), "candidates": [], "results": []}
    cands = found[:keep]
    base = next((c for c in found if c.label == "base"), None)
    if base is not None and base not in cands:
        cands.append(base)
        say(f"  + [base]: {base.interruptions} int "
            f"(do nothing, hold what the hand holds)")

    out = []
    for i, c in enumerate(cands, 1):
        say(f"\nboard {i}/{len(cands)}: {c.interruptions} int / {c.negates} "
            f"neg  [{c.label}]")
        res = defend(our_deck, our_hand, c, their_decks, hands=hands,
                     draws=defence_draws, solve_ms=solve_ms, log=say)
        res["is_base"] = c is base
        # Say it as it lands. A full run is tens of minutes of solver and
        # the caller prints the table only at the end, so without this the
        # whole middle of the run looks like nothing is happening.
        say(f"  -> stopped {res['stopped']}/{res['tested']}, its control "
            f"{res['control_stopped']}/{res['control_tested']}"
            if res["tested"] else "  -> no hand could be tested")
        out.append(res)
    out.sort(key=lambda r: (-(r["stop_rate"] if r["stop_rate"] is not None
                              else -1), -r["candidate"]["interruptions"]))

    # A control that stops everything is the defect, not a result: the
    # attacker could not assemble its payoff against an EMPTY field, so
    # nothing below is about anybody's board.
    controls = [r["control_rate"] for r in out
                if r.get("control_rate") is not None]
    broken = bool(controls) and min(controls) >= 1.0
    if broken:
        say("\n  !! NO RANKING POSSIBLE. An EMPTY field stopped every hand,")
        say("     so the attacker's own turn-2 search is the obstacle, not")
        say("     any board. ISSUES.md #4.")
    rates = {r["stop_rate"] for r in out if r["stop_rate"] is not None}
    return {"deck": Path(our_deck).stem, "hand": list(our_hand),
            "candidates": [c.to_json() for c in cands], "results": out,
            "discriminated": len(rates) > 1,
            "control_is_broken": broken,
            "unsupported": breaking.UNSUPPORTED,
            "beats_control": any(r.get("beats_control") for r in out)}


def save(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1))
    return path


# --- ranking a board by what the meta plays THROUGH ----------------------
#
# `rank` above asks the question directly -- deal the meta a going-second
# hand and see whether it still functions -- and the pinned solver cannot
# answer it. Established 2026-10-26 with the harness finally correct
# (`breaking.split_position`, a verified board on the field, the attacker
# holding six): the same hand and target that SOLVE from an ordinary turn
# 1 reach nothing from a resumed root, on either seat and at any turn
# depth. ISSUES.md #4 has the three-case proof. So the going-second
# ranking stays unsupported.
#
# This is the same question asked of machinery that works. A board is a
# set of interruptions; `--fire` already measures, over forty hands per
# cell, whether a deck's own turn-1 line still reaches its payoff when a
# given card is played at every legal window of it. That is "does this
# interruption stop that deck", which is what a board is for. The matrix
# is already computed (`scenarios.run_matrix`, results/handtrap_matrix_*)
# so ranking a board costs no solver time at all.
#
# WHAT IT DOES NOT SEE, stated here because the number must never be read
# as the whole board: `--fire` plays a card from the opponent's HAND, so
# only the pieces your board holds in hand are measured this way. A
# field negate -- Apollousa, a big Synchro -- is not a card anyone can
# activate from hand, and no flag on this solver plays one for you. Those
# pieces are counted and named separately, never folded into the score,
# because a weight chosen here would be exactly the invented number this
# project keeps having to retract.

MEASURABLE = "hand"
"""The only `where` a fire test reproduces: the card is held and used.

`board_interruptions` labels every piece field / set / hand / grave. A
piece in HAND is the fire test's own situation, so its measured survival
transfers exactly. The others are a different situation and are reported
untested rather than approximated.
"""


def split_pieces(pieces):
    """(measured, untested) from a candidate's (code, where, negate) list.

    Returns two lists of (code, where, negate). `untested` is not a
    failure -- it is the part of the board this method structurally
    cannot see, and the caller must print it beside the score.
    """
    measured = [tuple(p) for p in pieces if p[1] == MEASURABLE]
    untested = [tuple(p) for p in pieces if p[1] != MEASURABLE]
    return measured, untested


def piece_through(cell):
    """P(their line gets through ONE piece), from its matrix cell.

    `1 - window_rate * (1 - survival)`: the card has to have a legal
    window AND the line has to fail through it, which is the same
    expectation `scenarios.weighted_handtrap_ranking` scores a maining
    decision on. A card that is never live does not stop anything, and
    scoring its stop-rate alone would rank it as if it did.

    None when the cell carries no survival -- the card was never tested
    against this deck, which is not the same as it doing nothing.
    """
    if not cell:
        return None
    surv = cell.get("survival")
    if surv is None:
        return None
    return 1.0 - (cell.get("window_rate") or 0.0) * (1.0 - surv)


def board_vs_deck(measured, cells):
    """One (board, deck) cell: per-piece detail and the board's share.

    `through` multiplies the pieces, which ASSUMES THEY ARE INDEPENDENT
    and they are not -- a handtrap landing changes the line the next one
    faces, usually making it shorter and easier to interrupt again. The
    product is therefore an upper bound on getting through, i.e. a lower
    bound on the board. It is reported with the per-piece numbers beside
    it so the reader can see what it is made of, and `tested` says how
    many pieces it actually rests on.
    """
    detail, through = [], 1.0
    tested = 0
    for code, where, negate in measured:
        cell = (cells or {}).get(code) or (cells or {}).get(str(code))
        p = piece_through(cell)
        detail.append({"code": code, "where": where, "negate": bool(negate),
                       "through": p,
                       "survival": (cell or {}).get("survival"),
                       "window_rate": (cell or {}).get("window_rate"),
                       "tested_hands": (cell or {}).get("tested")})
        if p is not None:
            through *= p
            tested += 1
    return {"through": through if tested else None, "tested_pieces": tested,
            # Pieces that COULD be fired but have no cell against this
            # deck: the matrix only holds `scenarios.HANDTRAPS`, so a
            # board holding anything else is scored over fewer pieces
            # than it has. Named, because "measured" was printing them.
            "no_cell": [d["code"] for d in detail if d["through"] is None],
            "pieces": detail}


def rank_boards(candidates, matrix, weights=None, log=None):
    """Rank candidate end boards by how little of the meta plays through.

    `matrix` is `scenarios.handtrap_matrix`'s output, `weights` the
    locals.toml share per archetype. A board that blanks a deck nobody in
    your room plays is worth nothing, which is the whole reason that file
    exists -- so the headline is the weighted figure, and each deck's own
    number is kept so a single matchup can be read directly.

    Like `weighted_handtrap_ranking`, the weighted mean divides by the
    weight of the decks the board was actually measured against, not by
    the whole stated meta, and `coverage` says what that was.
    """
    say = log or (lambda m: None)
    weights = weights or {}
    rows = []
    for cand in candidates:
        measured, untested = split_pieces(cand.pieces)
        per_deck, wsum, wtot = {}, 0.0, 0.0
        for deck, data in (matrix or {}).items():
            res = board_vs_deck(measured, data.get("cells"))
            per_deck[deck] = res
            w = weights.get(deck, 0.0)
            if w and res["through"] is not None:
                wsum += w * res["through"]
                wtot += w
        rows.append({
            "candidate": cand.to_json(),
            "measured": [list(p) for p in measured],
            "untested": [list(p) for p in untested],
            "per_deck": per_deck,
            # None, not 1.0: a board with nothing measurable has no score,
            # and a 1.0 would rank it as the worst board rather than the
            # unmeasured one.
            "through": (wsum / wtot) if wtot else None,
            "coverage": wtot,
        })
        say(f"  {cand.interruptions} int / {cand.negates} neg [{cand.label}]: "
            f"{len(measured)} piece(s) measurable, {len(untested)} not")
    # Least gets through first; an unscored board goes last rather than
    # sorting as if it were perfect.
    rows.sort(key=lambda r: (r["through"] is None,
                             r["through"] if r["through"] is not None else 1.0))
    return rows
