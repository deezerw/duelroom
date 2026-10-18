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


def break_args(start_replay, remove, our_hand=None, solve_ms=None,
               add=(), turns=2):
    """Solver arguments for "break this board".

    `--player 1` optimises our turn rather than theirs, `--turns 2` lets
    the line cross the turn boundary, and each `--board-remove` says the
    final board must no longer contain that card -- which is what
    breaking a board means, expressed in the solver's own vocabulary.
    """
    args = [
        str(Path(start_replay).resolve()),
        "--player", "1",
        "--turns", str(turns),
        "--solve-ms", str(solve_ms or engine.SOLVE_MS),
    ]
    for card in ([remove] if isinstance(remove, (str, int)) else remove):
        args += ["--board-remove", str(card)]
    for card in add:
        args += ["--board-add", str(card)]
    if our_hand:
        args += ["--hand", "|".join(str(c) for c in our_hand)]
    return args


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
