"""Going-second board breaking.

Stage 4's second half. The opponent assembles their turn-1 board; we are
dealt a going-second hand and search for a line that removes their key
interruption.

Why this needs new code
-----------------------
The solver's going-second flags are first class --
``--start <replay> --player 1 --turns 2 --board-remove <card>`` -- but
``--start`` needs a replay containing BOTH decks, and the demonstrations
stored by the board library contain only the piloted one. ygorl solves
every deck against :data:`PASSIVE_OPPONENT`, a deck of 40 copies of one
vanilla monster, so the opponent in those records has no real deck.

Three ways of transplanting a stored line into a two-deck duel were tried
and all three fail. Do not retry them:

1. Keep the start, responses and seed, swap deck b for ours -> the duel
   errors on turn 1. Player 1 contributes real responses (18 of 72 in the
   sampled line), and those bytes are only valid for the deck they were
   recorded against.
2. Filter to player-0 responses and replay under the ``solo`` curriculum,
   which suppresses the opponent during the learner's turn -> also errors.
   The curriculum changes which decisions are presented, so the recorded
   responses no longer line up with the decision stream.
3. Our deck as b with the full original response log -> errors, same
   cause as (1).

Only the original configuration replays. The stored lines are bound to
the passive opponent they were solved against.

What works instead
------------------
``make_template`` is where the solver gets its opponent: it writes a
``.yrpX`` carrying the rule flags, seed words and deck b, and the solver
substitutes our deck and hand into side a. It hardcodes
``PASSIVE_OPPONENT``. :func:`template_vs` is the same function with deck b
under our control, so a solve run against it produces lines -- and
replays -- in a duel where both sides are real decks. Those replays are
then valid ``--start`` inputs.

The cost is that the opponent's board must be re-solved per opposing deck
rather than reused from the library.

Status (2026-10-18)
-------------------
VERIFIED end to end up to the search itself:

* :func:`template_vs` writes a template carrying both real decks
  (a=kewl-tune 40/15, b=sky-striker 40/15).
* The solver runs against it and writes solution replays that carry both
  decks and replay cleanly.
* Those replays stop when the combo stops, so ``--start`` first rejected
  them: "END OF THE RECORDING: the line does not get through the turn".
  ``ygorl.solver.demo.convert_line`` already solves this -- it takes
  ``close_turn=True`` (its default) and re-emits the line with the turn
  closed. 24 responses became 30 and the end board moved to turn 2.
* Rebuilt as a replay, the solver now ACCEPTS the start and reports
  "target board (end of turn, player 1)".

BLOCKED on the last step: with ``--player 1 --turns 2 --board-remove``
the solver completes its reference analysis and exits 0 without entering
a search, writing no solutions. ``--target-subset`` and ``--board-add``
variants behave the same; ``--solve --target-subset`` exits 1. Its own
report shows our side untouched -- "player 1 total: 55, left in
hand+deck+extra: 55, consommees: 0".

So the remaining question is how the going-second mode expects to be
driven, not whether the start state is right. Worth noting that ygorl's
spike doc lists ``--fire`` and ``--adapt`` as untested upstream, so this
path may simply be unexercised. Read the solver's own sources for how
``--player 1`` reaches its search before guessing at more flags.
"""

import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

from ygorl.engine.duel import Deck as YDeck, DuelConfig
from ygorl.engine.replay import Replay
from ygorl.solver.batch import _deck_dict, _engine_kwargs

from . import engine
from .deck import Deck


def to_ydeck(deck, name=None):
    """duelroom Deck -> ygorl Deck."""
    if isinstance(deck, (str, Path)):
        deck = Deck.from_ydk(deck)
    return YDeck(tuple(deck.main), tuple(deck.extra), tuple(deck.side),
                 name or deck.name)


def template_vs(path, their_deck, our_deck, seed, config=None, cards=None,
                scripts=None, shuffle=False):
    """Write a solver template whose opponent is a real deck.

    Mirrors ``ygorl.solver.batch.make_template`` except that deck b is
    ours instead of PASSIVE_OPPONENT. The solver fills side a with the
    deck and hand it is given, so `their_deck` only supplies the name
    here; pass it for a readable replay header.
    """
    config = config or DuelConfig()
    theirs = to_ydeck(their_deck)
    ours = to_ydeck(our_deck)
    rep = Replay(
        seed=seed,
        first=0,
        rule_flags=config.rule_flags,
        player=asdict(config.player),
        # shuffle_decks=False is upstream's choice and it is right for the
        # SOLVING player, whose deck and hand the solver substitutes via
        # --deck/--hand anyway. It is wrong for the other seat: with no
        # shuffle, player 1's opening hand is simply the tail of its
        # decklist, identical for every seed, which is why every
        # going-second trial tested one hand -- and for kewl-tune that
        # hand is four handtraps and a Synchro spell, which cannot
        # assemble its payoff at any budget. Measured with shuffle on,
        # seeds 0 and 7 deal different and functional hands.
        shuffle_decks=shuffle,
        decks={"a": _deck_dict(theirs), "b": _deck_dict(ours)},
        responses=[],
    )
    rep.to_yrpx(path, names=(theirs.name or "them", ours.name or "us"),
                **_engine_kwargs(cards, scripts))
    return Path(path)


def deck_with_hand(deck_path, hand, out_path):
    """Write a copy of `deck_path` whose main deck ENDS with `hand`.

    `--hand` does not reach the going-second player. Proven 2026-10-24:
    the solver's own "starting position (player 1)" block listed five
    cards nobody asked for -- Synchro Overtake and three Effect Veiler and
    a Dominus Purge -- while `--hand` carried six different ones, and two
    different template seeds produced the SAME five. Those five are the
    last five entries of kewl-tune.ydk, reversed: `template_vs` sets
    shuffle_decks=False and the core runs with DUEL_PSEUDO_SHUFFLE, so
    nothing is ever shuffled and the hand is drawn off the end of the
    list.

    The consequence was that every going-second trial tested one hand --
    whatever sits at the bottom of the decklist file -- so `hands=6` was
    the same hand six times, and for kewl-tune that hand is four handtraps
    and a Synchro spell, which cannot assemble its payoff. Hence 0 of 72
    searches getting through, and a board of vanilla monsters "stopping"
    the whole meta.

    So the hand is placed where the core will actually draw it, which is
    what `ygorl.solver.batch` already does for the solitaire path (it
    writes the shuffled order out as a per-hand .ydk). Order inside the
    hand does not matter -- only which cards are in it.
    """
    deck = Deck.from_ydk(Path(deck_path))
    rest = list(deck.main)
    for code in hand:
        if code in rest:
            rest.remove(code)
        else:
            raise ValueError(
                f"{Path(deck_path).stem}: {code} is not in the main deck, so "
                f"it cannot be dealt as part of the opening hand")
    out = Deck(name=deck.name, main=rest + list(hand),
               extra=list(deck.extra), side=list(deck.side))
    out.write_ydk(Path(out_path))
    return Path(out_path)


def going_second_args(*_args, **_kw):
    """REMOVED. It drove the solver in SYNTHETIC-START mode.

    The start replay went in as the positional argument -- which is a
    TEMPLATE, not a position -- with `--deck`/`--hand` beside it, and
    that mode builds a fresh duel. The solver says so in its own report,
    "=== synthetic start ===", and queried at the root the fields of both
    players were empty. So every going-second number this project ever
    printed was measured with no board on the field, and `--deck` and
    `--hand` reached side a while `--player 1` pointed the search at side
    b, whose hand came from the template's decklist order. That is why
    four separate fixes each "changed the hand" and none changed the
    result, and why a board of one vanilla monster stopped 18 of 18
    hands: nothing was ever in front of them.

    `attack_args` is the supported shape. Kept as a raiser because the
    broken one was copied into three call sites, and a function that
    still returns a command line is a function that gets copied again.
    """
    raise NotImplementedError(
        "going_second_args drove the solver in synthetic-start mode, which "
        "builds a FRESH duel and puts no board on the field. Use "
        "attack_args(reference, start, prefix, target) with a position from "
        "second_position(); see ISSUES.md #4.")


SEARCH_MARKERS = ("probe:", "bounded-discrepancy")


def searched(stdout):
    """True if the solver actually entered its search phase.

    Guards against the repair-mode no-op above, where exit status is 0
    and no solutions are written for a reason that has nothing to do with
    the deck.
    """
    low = stdout.lower()
    return any(m in low for m in SEARCH_MARKERS)


# --- extending a solved line to the end of its turn ----------------------
#
# `ScriptedThenYield` and `action_kinds_seen` lived here and were dead:
# `convert_line(close_turn=True)` superseded them and nothing referenced
# either. Removed 2026-10-24. The fact they existed to document is kept,
# because it will bite the next person the same way:
#
#   The solver's own replays STOP when the combo stops, so `--start`
#   rejects them -- "END OF THE RECORDING: the line does not get through
#   the turn". It needs a replay where the turn has actually passed, which
#   is why `build_start` and `inert_start` both re-emit their line with
#   close_turn=True before handing it over.


# --- driver ---------------------------------------------------------------

class SolverFailed(RuntimeError):
    """The solver exited non-zero. NOT the same as finding no line."""


def _run_solver(cmd, what):
    """Run the solver and refuse to confuse a crash with a null result.

    These calls used capture_output with no check, so a non-zero exit
    produced no .yrp and the caller reported "their hand bricked" -- a
    crashed solver and a deck that genuinely cannot combo looked
    identical in the output. Everything downstream (board-break rates,
    floodgate survival) would have quietly absorbed solver failures as
    deck weakness.
    """
    r = subprocess.run(cmd, capture_output=True, text=True, env=engine.env())
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise SolverFailed(f"{what}: exit {r.returncode} — " + " | ".join(tail))
    return r


# --- the going-second POSITION ------------------------------------------
#
# `going_second_args` above says what was wrong with the old shape and why
# it now refuses. This is the supported one.
#
# `--start` plus `--approach` is how the solver is told to search from a
# position a duel has already reached -- it is what
# `ygorl.solver.combo_solver.SolveRequest` uses for continuations:
#
#   <reference> --start <start.yrpX> --approach <prefix.yrpX> --no-ref
#              --target <payoff> --player 1
#
# `start` carries the duel's parameters with NO responses; `prefix` is the
# same header plus the responses that reach the position. The solver roots
# its finisher on the approach's own duel, so the search begins where the
# prefix ended. VERIFIED by replaying the prefix and querying the core:
# the board builder has a board and the attacker holds six cards, five
# plus the draw for turn. `check_root` is that check and it runs on every
# position this module builds.

ATTACKER = 1
"""Engine seat of the going-second player.

Both templates here put the board builder in side a, and `first=0` seats
side a as engine player 0, so the attacker is always player 1. Named
rather than spelled "1" at four call sites.
"""


class RootNotPlayable(RuntimeError):
    """The position the search would be rooted on is not a real turn 2."""


def split_position(source, responses, out_dir, names=("them", "us")):
    """A (start, prefix) pair sharing ONE header, for --start/--approach.

    `source` is a Replay whose header is the duel to start from -- in
    practice the solver's own output replay, because its responses are
    only valid against the decklist order IT wrote (see `template_vs`).
    Both files therefore carry that same order, which is what makes a
    board prefix and a do-nothing control PAIRED: same decks, same seed,
    same cards dealt to the attacker, differing only in what the board
    builder did with its turn.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def emit(name, rs):
        rep = Replay(seed=source.seed, first=source.first,
                     rule_flags=source.rule_flags, player=source.player,
                     shuffle_decks=source.shuffle_decks, decks=source.decks,
                     responses=list(rs), seed_words=source.seed_words)
        path = out_dir / name
        rep.to_yrpx(path, names=names)
        return path

    return emit("start.yrpX", []), emit("prefix.yrpX", responses)


def pass_turn_responses(start_path, cards=None):
    """Responses for "the board builder does NOTHING and passes turn 1".

    The paired control for every stop rate here. `convert_line` with an
    empty response list is exactly this: it replays nothing and then
    takes passive actions until the turn flips.
    """
    from ygorl.engine.replay import load_yrp
    from ygorl.solver.demo import convert_line

    line = convert_line(load_yrp(Path(start_path)), [], responses=[],
                        close_turn=True, cards=cards)
    return list(line.responses)


def root_state(prefix_path, cards=None):
    """The position a search rooted on this prefix actually starts from.

    Replays the prefix and queries the core for each player's field and
    hand. NOTHING in this project looked at this before, which is how a
    whole feature came to report stop rates measured against an empty
    field on both sides.
    """
    from ygorl.engine import constants as C
    from ygorl.engine import messages as M

    rep = Replay.from_yrp(Path(prefix_path))
    duel = rep.duel(cards=cards)

    class _Watch:
        """Keeps the last field query: `replay` closes the duel."""

        def __init__(self):
            self.last = None

        def on_start(self, core):
            self._grab(core)

        def on_buffer(self, core, buf):
            self._grab(core)

        def _grab(self, core):
            try:
                self.last = M.decode_message(
                    bytes([C.MSG_RELOAD_FIELD]) + core.query_field())
            except Exception:                              # noqa: BLE001
                pass

    watch = _Watch()
    res = duel.replay(rep.responses, observer=watch)
    if watch.last is None:
        raise RootNotPlayable(f"{Path(prefix_path).name}: no field state")
    out = {"reason": getattr(res, "reason", None), "players": []}
    for pl in watch.last.players:
        d = pl if isinstance(pl, dict) else vars(pl)
        field = [c for c in (list(d.get("mzone") or [])
                             + list(d.get("szone") or [])) if c]
        out["players"].append({"field": len(field), "hand": d.get("hand"),
                               "main": d.get("main"), "grave": d.get("grave"),
                               "extra": d.get("extra")})
    return out


def check_root(prefix_path, expect_board, cards=None):
    """Refuse a position that cannot measure anything. Returns the state.

    Two ways a going-second position is not one, both of which held
    silently for weeks:

    * the attacker never drew for turn, so the "turn 2" never began;
    * the board under test is not on the field at all.

    `expect_board` is False for the do-nothing control, where an empty
    field is the whole point.
    """
    st = root_state(prefix_path, cards=cards)
    if len(st["players"]) < 2:
        raise RootNotPlayable(
            f"{Path(prefix_path).name}: the core reported "
            f"{len(st['players'])} player(s), not 2")
    me = st["players"][ATTACKER]
    builder = st["players"][1 - ATTACKER]
    hand = me.get("hand") or 0
    if hand < 6:
        raise RootNotPlayable(
            f"{Path(prefix_path).name}: the attacker holds {hand} cards, so "
            f"it has not drawn for turn -- this is not a turn 2")
    if expect_board and not builder["field"]:
        raise RootNotPlayable(
            f"{Path(prefix_path).name}: the board builder's field is EMPTY, "
            f"so there is no board to measure")
    st["attacker_hand"] = hand
    st["board_cards"] = builder["field"]
    return st


def attack_args(reference, start, prefix, target, solve_ms=None,
                finisher_ms=None, player=ATTACKER, turns=2):
    """Solver arguments for "can the attacker play through this board".

    `--no-ref` is not optional: without it the goal comes from the
    reference instead of `--target`, and the solver spends the run
    re-reaching the reference's own board ("repair mode") and exits 0
    having searched nothing.

    `--finisher-min` is load-bearing for a different reason. The NRPA
    phase is rooted on the START duel, where the board builder has not
    played yet, so its rollouts are spent on a position nobody asked
    about; the phase that searches the approach -- the real turn 2 -- is
    the finisher. Measured on a 20 s budget: 15 s of NRPA against 5 s of
    finisher, with 100% of the NRPA rollouts cut at the turn boundary.
    """
    solve_ms = engine.SOLVE_MS if solve_ms is None else solve_ms
    if finisher_ms is None:
        finisher_ms = int(solve_ms * 0.75)
    return [
        str(Path(reference).resolve()),
        "--start", str(Path(start).resolve()),
        "--approach", str(Path(prefix).resolve()),
        "--no-ref",
        "--target", str(target),
        "--player", str(player),
        "--turns", str(turns),
        "--solve-ms", str(solve_ms),
        "--finisher-min", str(min(finisher_ms, solve_ms)),
    ]


UNSUPPORTED = (
    "the going-second SEARCH cannot solve a position it solves going first: "
    "with the position verified correct (a board on the field, the attacker "
    "holding six), the same hand and target reach nothing from a resumed "
    "root on either seat at any turn depth. See ISSUES.md #4. Use "
    "`gauntlet` to rank boards instead; it measures the same question with "
    "machinery that works."
)
"""Why every number below is not to be believed yet.

The POSITION is now right -- that is new, and `second_position` proves it
by querying the core. What is still wrong is the search from it, and it is
in the vendored solver, so this stays a refusal rather than a caveat.
"""


def attack(start, prefix, target, solve_ms=None, seed=0, threads=2,
           scratch=None, binary=None, finisher_ms=None):
    """Run ONE going-second search from a prepared position.

    Shared by `going_second`, `floodgate_check` and `gauntlet.defend`, so
    the invocation exists once. Every one of them had its own copy of the
    broken one, and the fixes landed in different subsets of the three.
    """
    import subprocess

    from ygorl.solver.combo_solver import Workdir

    # The scratch directory is always REPORTED, never silently dropped:
    # it holds the solver.log, which is the only direct evidence of what
    # the mode did, and an unreported mkdtemp is the leak `sweep._solve_one`
    # and `gauntlet._one_draw` both had to be fixed for.
    scratch = Path(scratch or tempfile.mkdtemp(prefix="duelroom-atk-"))
    scratch.mkdir(parents=True, exist_ok=True)
    wd = Workdir.create(scratch / "workdir")
    out = scratch / "gs"
    out.mkdir(exist_ok=True)
    args_ = attack_args(prefix, start, prefix, target, solve_ms=solve_ms,
                        finisher_ms=finisher_ms)
    cmd = [str(binary or engine.SOLVER_BIN), args_[0],
           "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    cmd += args_[1:] + ["--threads", str(threads), "--seed", str(seed),
                        "--outdir", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=engine.env())
    # KEEP the report. Throwing it away is why "0 of 72 hands got through"
    # went unexplained for days: the starting-position block is the only
    # direct evidence of what the mode is doing, and it is what finally
    # showed the field empty on both sides.
    log = scratch / "solver.log"
    try:
        log.write_text((r.stdout or "") + "\n--- stderr ---\n"
                       + (r.stderr or ""), encoding="utf-8")
    except OSError:
        log = None
    res = {"searched": searched(r.stdout or ""),
           "through": bool(list(out.glob("*.yrp"))),
           "returncode": r.returncode,
           "log": str(log) if log else None,
           "scratch": str(scratch)}
    return res


def going_second(their_deck, our_deck, their_target, our_target, hands=4,
                 solve_ms=None, scratch=None, control=True):
    """Can our deck assemble its payoff going second into their board?

    One record per hand against the board, and -- unless `control` is off
    -- one against the PAIRED CONTROL, the same position with the builder
    passing. Without the control a stop rate has no zero: "they got
    through 0 of 4" is only a statement about the board if they get
    through more when there is no board, and for weeks they did not.

    EVERY NUMBER HERE IS UNSUPPORTED; see `UNSUPPORTED`.
    """
    scratch = Path(scratch or (engine.ROOT / "results" / "going_second"))
    rows = []
    for seed in range(hands):
        sd = scratch / f"h{seed}"
        row = {"seed": seed}
        for board in ((True, False) if control else (True,)):
            key = "board" if board else "control"
            try:
                pos = second_position(their_deck, our_deck, their_target,
                                      seed, sd / key, solve_ms, board=board)
            except (SolverFailed, RootNotPlayable) as e:
                row[key] = {"error": str(e)}
                continue
            if pos is None:
                row[key] = {"error": "their hand found no line"}
                continue
            st, pre, root = pos
            res = attack(st, pre, our_target, solve_ms=solve_ms,
                         seed=1000 + seed, scratch=sd / key / "atk")
            res["board_cards"] = root["board_cards"]
            res["attacker_hand"] = root["attacker_hand"]
            row[key] = res
        rows.append(row)
    return rows


# --- floodgate check ------------------------------------------------------

def floodgate_check(our_deck, our_target, floodgates=None, hands=2,
                    solve_ms=None, scratch=None):
    """Does the deck still function under each common floodgate?

    Locals run floodgates far more than top tables do, and this is cheap
    next to a full gauntlet.

    Rewritten onto `second_position`. The old version built the start the
    same broken way as everything else AND never got its fix: when the
    dealt decklist was moved into the start replay for `going_second`,
    this call site kept passing `--deck our_deck` with a sampled
    `--hand`, so every floodgate was tested against the same hand -- the
    one at the bottom of the decklist -- however many `hands` were asked
    for. It sat four lines from the fix for three weeks.

    EVERY NUMBER HERE IS UNSUPPORTED; see `UNSUPPORTED`.
    """
    from ygorl.engine.duel import Deck as YDeck
    from .scenarios import FLOODGATES

    floodgates = floodgates or FLOODGATES
    scratch = Path(scratch or (engine.ROOT / "results" / "floodgate"))
    rows = []
    for code, name in floodgates.items():
        functioned = tested = errors = 0
        # A deck of 40 copies, so turn 1 reliably ends with one set.
        # Targeting it face-DOWN is the realistic case and the only legal
        # one: a Continuous Trap cannot activate the turn it is set, so
        # what you face going second is a set card that flips during your
        # turn.
        flood = YDeck((int(code),) * 40, (), (), f"flood-{code}")
        for seed in range(hands):
            sd = scratch / f"{code}_{seed}"
            sd.mkdir(parents=True, exist_ok=True)
            ydk = sd / "flood.ydk"
            ydk.write_text("#created by duelroom\n#main\n"
                           + "\n".join([str(code)] * 40)
                           + "\n#extra\n!side\n")
            try:
                pos = second_position(ydk, our_deck, f"{code}@szone:fd",
                                      seed, sd, solve_ms)
            except (SolverFailed, RootNotPlayable):
                errors += 1
                continue
            if pos is None:
                continue
            st, pre, _ = pos
            res = attack(st, pre, our_target, solve_ms=solve_ms,
                         seed=2000 + seed, scratch=sd / "atk")
            if not res["searched"]:
                continue
            tested += 1
            functioned += res["through"]
        rows.append({"card": code, "name": name, "tested": tested,
                     "functioned": functioned, "errors": errors,
                     "rate": functioned / tested if tested else None})
    return rows


def second_position(builder_deck, attacker_deck, builder_target, seed,
                    scratch, solve_ms=None, board=True, cards=None,
                    builder_hand=None):
    """The whole going-second position, start and approach both.

    `board=True`: the builder solves its turn 1, the turn is closed, and
    the attacker is to move into the result. `board=False` is the PAIRED
    CONTROL -- the same decks, the same seed and the same cards dealt to
    the attacker, with the builder simply passing. Only the board differs
    between the two, which is what makes a difference attributable to it.

    That control replaces the deck of 40 vanilla monsters this module and
    `gauntlet` used to build. A separate deck changes the decks, the seed
    and the attacker's hand all at once, so a stop rate measured against
    it was never comparable with one measured against a board.

    `builder_target` is one target or a conjunction of them, and
    `builder_hand` forces the builder's opening hand -- which is what
    `gauntlet` needs, where the builder is you holding specific cards
    rather than a deck sampled from a seed.

    Returns (start, prefix, root_state), or None when the builder's own
    hand found no line -- a real finding about that hand, not an error.
    Raises `RootNotPlayable` if the position is not a turn 2 the attacker
    can act in, so a meaningless search is never run.
    """
    from ygorl.engine.replay import load_yrp
    from ygorl.solver.batch import sample_hand
    from ygorl.solver.combo_solver import Workdir
    from ygorl.solver.demo import convert_line

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    wd = Workdir.create(scratch / "workdir")
    sols = scratch / "sol"
    sols.mkdir(exist_ok=True)

    # The builder is side a, hence engine player 0, hence the seat
    # `--deck` and `--hand` actually reach. That is not a detail: the
    # synthetic start substitutes them into side a and nowhere else, so
    # building the board from any other seat silently searches a hand
    # nobody chose. See `going_second_args` for what that cost.
    tpl = template_vs(scratch / "tpl.yrpX", builder_deck, attacker_deck,
                      seed=seed, shuffle=True)
    if builder_hand is None:
        builder_hand, _ = sample_hand(to_ydeck(builder_deck), seed=seed)
    targets = ([str(builder_target)] if isinstance(builder_target, (str, int))
               else [str(t) for t in builder_target])
    if not targets:
        raise ValueError("a board needs at least one target to build")
    cmd = [str(engine.SOLVER_BIN), str(tpl), "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    cmd += ["--no-ref", "--deck", str(Path(builder_deck).resolve()),
            "--hand", "|".join(map(str, builder_hand))]
    for t in targets:
        cmd += ["--target", t]
    cmd += ["--solve-ms", str(solve_ms or engine.SOLVE_MS),
            "--threads", "1", "--outdir", str(sols)]
    _run_solver(cmd, f"second_position({Path(builder_deck).stem}, seed {seed})")

    found = sorted(sols.glob("*.yrp"))
    if not found:
        return None
    # The header has to be the OUTPUT replay's: its responses are only
    # valid against the decklist order it wrote, and the control has to
    # share that header or it is not paired.
    source = Replay.from_yrp(found[0])
    line = convert_line(load_yrp(found[0]), targets, close_turn=True,
                        cards=cards)
    bare, _ = split_position(source, [], scratch / "bare")
    responses = (list(line.responses) if board
                 else pass_turn_responses(bare, cards=cards))
    start, prefix = split_position(source, responses,
                                   scratch / ("board" if board else "control"))
    root = check_root(prefix, expect_board=board, cards=cards)
    return start, prefix, root
