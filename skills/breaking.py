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
                scripts=None):
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
        shuffle_decks=False,
        decks={"a": _deck_dict(theirs), "b": _deck_dict(ours)},
        responses=[],
    )
    rep.to_yrpx(path, names=(theirs.name or "them", ours.name or "us"),
                **_engine_kwargs(cards, scripts))
    return Path(path)


def going_second_args(start_replay, our_deck_path, our_hand, target,
                      solve_ms=None, turns=2):
    """Solver arguments for "can we still do our thing through their board".

    `--board-remove` does NOT mean this. It edits the captured goal so a
    card is no longer REQUIRED (`main.cpp` sets `c.present = false` on the
    target board) -- goal relaxation, not destruction. The help's "stop
    requiring this card" says so; it is easy to read as the opposite.
    Breaking a board is therefore not expressible in the solver's goal
    language, which only talks about what `--player`'s own final board
    contains.

    The question that matters is expressible, and is arguably the better
    one: going second, into their real board, can we still assemble our
    payoff? If their interruptions stop us we fail; if we play through,
    we succeed. That is the handtrap matrix generalised from one card to
    a whole board.

    `--deck` and `--hand` are REQUIRED alongside `--start`. Without them
    the solver enters "repair mode" -- it tries to re-reach the
    reference's own target, has nothing to search with, and exits 0 after
    the analysis phase without ever searching. That silent no-op cost an
    afternoon; the giveaway is output with no "probe:" or
    "bounded-discrepancy search" line.
    """
    return [
        str(Path(start_replay).resolve()),
        "--deck", str(Path(our_deck_path).resolve()),
        "--hand", "|".join(str(c) for c in our_hand),
        "--player", "1",
        "--turns", str(turns),
        "--target", str(target),
        "--solve-ms", str(solve_ms or engine.SOLVE_MS),
    ]


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

# Kinds that give up priority / end the phase, best first. The solver's
# own replays stop when the combo stops, so `--start` rejects them with
# "END OF THE RECORDING: the line does not get through the turn" -- it
# needs a replay where the turn has actually passed to us.
_YIELD_KINDS = ("end", "finish", "pass", "cancel", "no", "to_bp", "to_ep")


class ScriptedThenYield:
    """Replay recorded action indices, then end the turn as soon as possible.

    ygorl's own ScriptedAgent raises IndexError once its log runs out,
    which is right for verification and useless here: after the combo we
    still need the turn to pass so the opponent (us) becomes the turn
    player.
    """

    def __init__(self, actions):
        self._actions = list(actions)
        self._next = 0
        self.exhausted_at = None

    def act(self, point):
        if self._next < len(self._actions):
            idx = self._actions[self._next]
            self._next += 1
            return idx
        if self.exhausted_at is None:
            self.exhausted_at = point.index
        for kind in _YIELD_KINDS:
            for a in point.actions:
                if a.kind == kind:
                    return a.index
        return point.actions[0].index if point.actions else 0


def action_kinds_seen(point):
    """Debug helper: the distinct action kinds offered at a decision."""
    return sorted({a.kind for a in point.actions})


# --- driver ---------------------------------------------------------------

def build_start(their_deck, our_deck, their_target, seed, scratch,
                solve_ms=None):
    """Solve their turn 1 against our deck, then close the turn.

    Returns the path to a replay that `--start` accepts, or None if their
    hand bricked. Two steps are load-bearing: the template must carry our
    deck as side b (otherwise the opponent is 40 vanilla monsters), and
    the solved line must be re-emitted with `close_turn=True` (otherwise
    `--start` rejects it -- "the line does not get through the turn").
    """
    import subprocess
    from ygorl.engine.replay import Replay, load_yrp
    from ygorl.solver.batch import sample_hand
    from ygorl.solver.combo_solver import Workdir
    from ygorl.solver.demo import convert_line

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    wd = Workdir.create(scratch / "workdir")
    sols = scratch / "sol"
    sols.mkdir(exist_ok=True)

    tpl = template_vs(scratch / "tpl.yrpX", their_deck, our_deck, seed=seed)
    theirs = to_ydeck(their_deck)
    hand, _ = sample_hand(theirs, seed=seed)

    cmd = [str(engine.SOLVER_BIN), str(tpl), "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    cmd += ["--no-ref", "--deck", str(Path(their_deck).resolve()),
            "--hand", "|".join(map(str, hand)),
            "--target", str(their_target),
            "--solve-ms", str(solve_ms or engine.SOLVE_MS),
            "--threads", "1", "--outdir", str(sols)]
    subprocess.run(cmd, capture_output=True, text=True, env=engine.env(),
                   timeout=None)

    found = sorted(sols.glob("*.yrp"))
    if not found:
        return None
    line = convert_line(load_yrp(found[0]), [str(their_target)],
                        close_turn=True)
    base = Replay.from_yrp(found[0])
    ext = Replay(seed=base.seed, first=base.first, rule_flags=base.rule_flags,
                 player=base.player, shuffle_decks=base.shuffle_decks,
                 decks=base.decks, responses=list(line.responses),
                 seed_words=base.seed_words)
    start = scratch / f"start_{seed}.yrpX"
    ext.to_yrpx(start, names=("them", "us"))
    return start


def going_second(their_deck, our_deck, their_target, our_target, hands=4,
                 solve_ms=None, scratch=None):
    """Can our deck assemble its payoff going second into their board?

    One record per hand: whether their turn-1 board was built at all,
    whether the solver searched, and whether we got through.
    """
    import subprocess
    from ygorl.solver.batch import sample_hand
    from ygorl.solver.combo_solver import Workdir

    scratch = Path(scratch or (engine.ROOT / "results" / "going_second"))
    rows = []
    ours_deck = to_ydeck(our_deck)
    for seed in range(hands):
        sd = scratch / f"h{seed}"
        start = build_start(their_deck, our_deck, their_target, seed, sd,
                            solve_ms)
        if start is None:
            rows.append({"seed": seed, "their_board": False,
                         "searched": False, "broke": False})
            continue
        wd = Workdir.create(sd / "workdir")
        out = sd / "gs"
        out.mkdir(exist_ok=True)
        # Going second draws for turn, so six cards rather than five.
        hand, _ = sample_hand(ours_deck, seed=1000 + seed, size=6)
        cmd = [str(engine.SOLVER_BIN)] + going_second_args(
            start, our_deck, hand, our_target, solve_ms)
        cmd = [cmd[0], cmd[1], "--workdir", str(wd.path)]
        for d in wd.scriptdirs:
            cmd += ["--scriptdir", str(d)]
        cmd += going_second_args(start, our_deck, hand, our_target,
                                 solve_ms)[1:]
        cmd += ["--threads", "2", "--outdir", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=engine.env(), timeout=None)
        wrote = list(out.glob("*.yrp"))
        rows.append({
            "seed": seed,
            "their_board": True,
            "searched": searched(r.stdout or ""),
            "broke": bool(wrote),
            "hand": hand,
        })
    return rows
