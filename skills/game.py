"""Finish the game (STAGE9): your turn 1, their turn 2, your turn 3 --
or, going second, their turn 1 and your turn 2.

Going first, a hand WINS if the opponent's LP reaches 0 by the end of
turn 3 (your second turn). Going second it wins if their LP reaches 0 on
turn 2 (your first): you break their turn-1 board and kill through it. A
hand that wins no game against any enemy hand is a DUD; one that wins
under `NEAR_DUD_RATE` of them a NEAR-DUD.

Every turn is a search by the ROOTED solver (patches/0002, built by
tools/build_rooted_solver.sh). Each turn starts from the position the
previous ones reached (`--root`), which is what the pinned solver could
not do (ISSUES.md #4):

  T1  you, from your real hand, toward the trainer's best goal for it.
  T2  the enemy, into your board: first "can they kill you" (`--goal-win`,
      `OTK_MS`), then their own payoff target. A kill is a loss.
  T3  you, `--goal-win`: did you find lethal?

Going second (`first=False`):

  T1  the enemy, from the SAME opening hand per seed as going first
      (`enemy_hand`), toward its payoff target; your hand traps respond.
  T2  you, `--goal-win` as player 1, from your real hand plus your draw.

Your hand is seated on player 1 by putting it at the bottom of your
decklist (`--hand` reaches side a only; see breaking.deck_with_hand), the
rest of the deck shuffled by the enemy seed, so your turn-2 draw is fixed
per (hand, enemy hand) like everything else turn 1 decides.

THE OPPONENT MODEL, stated because every number inherits it: whoever is
not searching is a scripted DEFENDER (`--opp-greedy`, patches 0002/0004)
playing out of the cards in `--opp-respond` -- its board's interruption
pieces, the player's own "counts as an interruption" cards and the known
hand traps -- never onto its own link. `DEFENDER` picks the script:

  greedy  patch 0002: the first eligible option the core lists, first
          legal follow-up. Measured on 2400 going-first games (2026-11-09):
          it stopped nothing on 79% of the enemy's turns, and in a traced
          game spent S:P Little Knight on the first hand effect of the turn
          into an EMPTY field, banishing its own Lars with it.
  smart   patch 0004 (`defender_args`): answers in a stated order (hand
          traps, negates, the rest), holds board pieces until the attacker
          controls a monster (`--opp-hold`), and picks follow-up targets by
          value -- its weakest given up, their strongest taken.

Either script is deterministic and the searching side's rollouts see it,
so a search is free to play around it (one activation to draw the negate,
then effect-less summons into lethal). Trying a small family of defences
per turn and keeping the best is the next step (`--opp-hold-until`).

Fairness rules (STAGE8/9):
* The enemy's OPENING hand is fixed per enemy seed and SHARED by all
  your hands: the template seed decides deck b's shuffle, and your deck a
  is substituted the same way every time. `run` asserts it from the
  positions themselves rather than trusting the argument. Their turn-2
  draw is NOT fixed, and should not be: a Maxx "C" resolved during your
  turn 1 draws them cards off the top, so what they draw next depends on
  how you played -- the same as at a table.
* Turn 1 is solved ONCE per (hand, board, enemy hand) and reused by all
  `RUNS` runs, so the runs vary only the turn-2 and turn-3 searches.
"""

import json
import re
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import breaking, engine, scenarios, stats, trainer

ROOTED_BIN = (engine.VENDOR / "build" / "combo-solver-rooted" / "bin"
              / "combosolver")
RUNS = 10            # games per (hand, board, enemy hand): the solver is randomized
ENEMY_HANDS = 3      # fixed enemy hands, shared by every one of your hands
ENEMY_SEED0 = 1000   # enemy hand k is dealt by seed ENEMY_SEED0 + k
WORKERS = 4         # games at once; 2 solver threads each = 8 cores
OTK_MS = 20_000      # the enemy's turn-2 "can they just kill you" search
# Going second, your break (turn 2) searches BREAK_X x the solve budget: the
# budget plateau (STAGE11, 2026-11-18) won 33 / 49 / 58 / 81 / 81% at 1 / 2 /
# 4 / 8 / 16x, 0 flips from 8x to 16x and seeds agreeing 100% from 8x. At
# 1x the games undersold every going-second hand. User chose 8x 2026-11-19.
BREAK_X = 8
# Going first, their turn-2 kill check searches OTK_X x OTK_MS: the plateau
# lost 38 / 54 / 66 / 76 / 93 / 93% at 1 / 2 / 4 / 8 / 16 / 32x, every flip
# toward lost up to 16x, 0 flips from 16x to 32x over 71 runs, seeds agreeing
# 100% from 16x (2026-11-19). The user's opponents are "extremely good": the
# enemy gets the converged budget.
OTK_X = 16
# Going first, your lethal turn 3 searches T3_X x the solve budget: the plateau
# won 87 / 89 / 94% at 1 / 2 / 4x, 4 flips toward won and none back, seeds
# agreeing 100% at 4x (2026-11-19, 62 runs). Our own turn was the one stage
# left at 1x (2026-11-20). A won search stops early; only misses pay in full.
T3_X = 4
# Going first, the enemy's non-kill turn 2 builds its trained board since this
# moment (UTC). Older going-first games in a section aimed it at the one payoff
# target: `run` refuses to add new games beside them until they are relabelled
# (as endgame-t1 / brk1x were going second) -- pooled, the two would be one
# number measuring two enemies.
FIRST_METHOD_SINCE = "2026-11-19T16:05:00"     # + OTK_X
# Games before this (UTC) were played before 2026-11-20's fixes: hand traps
# spent on their own turn (patch 0009, `own_turn_args`), negates chained onto
# their own link (--no-self-negate) and the enemy handing us monsters we can
# use (`fewest_gifts`). Both sides: `run` refuses to add
# games beside older ones -- relabel those first (tools/relabel_games.py,
# label "gift"), so no section pools the two methods.
# The version of how games are PLAYED, stamped on every stored game
# (game_results.method). Bump it whenever a change alters play; `run` refuses
# to add games beside any of another method (or, for rows from before the
# stamp, created before METHOD_SINCE) -- so a process that started on older
# code and is still writing can never pass as the new method.
METHOD = "2026-11-21b"
METHOD_SINCE = {False: "2026-11-20T22:14:00",    # going second: + free sets
                True: "2026-11-20T22:14:00"}     # going first: + T3_X, free sets
NEAR_DUD_RATE = 0.10  # under this share of games won: as bad as a dud in practice
# A --goal-win search stops once this many winning lines exist (patch 0003).
# The question it answers is yes/no; the rest of the budget only picked a
# cheaper winning line. A/B on 36 stored turn-3 positions (2026-11-06,
# tools/experiments/stop_on_win_ab.py): 24/36 wins full budget, 25/36 at 4,
# 26/36 at 1, no win lost by either; won searches 46 s -> 25 s. 4, not 1: a
# written line is still re-verified by replay, and one is a thin margin.
STOP_ON_WIN = 4
# The kernel page tracker (patch 0003, R2V_ARENA_UFFD=1) in place of the
# SIGSEGV + mprotect-per-page one, which spent more CPU in the kernel than in
# the search. Proven the same search: deterministic runs give identical
# counters and byte-identical solutions (tools/experiments/uffd_identity.py).
# At the games' settings it searches 1.88x the states in the same 60 s, 8/12
# -> 9/12 wins, none lost (uffd_throughput.py, 2026-11-06). It falls back to
# the old tracker by itself if userfaultfd is unavailable.
ARENA_ENV = {"R2V_ARENA_UFFD": "1"}
GAMES = Path(__file__).resolve().parent.parent / "results" / "games"
# Patch 0007: cards whose single-target effect our searches may only point at
# an OPPONENT's card. S:P Little Knight's summon banish, with nothing of theirs
# to hit, banished our own Fiendsmith Engraver from the GY in 16 of 240 games'
# turn 1s (h16/e1009 run 8): a resource thrown away for nothing. Its quick
# effect (both banished until the End Phase) is a real dodge and stays legal.
NO_SELF_TARGET = (29301450,)


def self_target_args():
    return (["--no-self-target", "|".join(map(str, NO_SELF_TARGET))]
            if NO_SELF_TARGET else [])


# Patch 0009: hand traps a search may not use on its OWN turn -- the ones
# whose only effect punishes the opponent's plays THAT turn: "each time your
# opponent Special Summons ... draw" (Maxx "C", every Mulcharmy), Nibiru
# (their 5th summon), Droll (their search). A turn search scores its own
# board, so spending one cost it nothing: we fired Mulcharmy Fuwalos on our
# own turn 1 in 94 of 480 going-first games vs Angelechy, and Angelechy its
# Fuwalos / Purulia on its turn 1 in 74 / 74 of 480 (traced 2026-11-20) --
# each one a hand trap gone before the turn it was for. Ghost Ogre, Bystial,
# Ash, Veiler and Imperm stay legal: each can matter on your own turn.
OWN_TURN_DEAD = (23434538, 27204311, 94145021)   # Maxx "C", Nibiru, Droll
_DRAW_ON_THEIR_SUMMON = re.compile(r"each time your opponent [^.]*summons[^.]*draw",
                                   re.I)
_own_turn_dead_cache = {}


def card_text(code):
    """A card's text from the ENGINE's card database (always there; the
    YGOPRODeck mirror is not, e.g. in a test database)."""
    from ygorl.engine.duel import default_cards
    c = default_cards().get(int(code))
    return (getattr(c, "desc", None) or "") if c is not None else ""


def own_turn_dead(deck):
    """`deck`'s main-deck cards in the OWN_TURN_DEAD family (by code, or by a
    card text that draws on the opponent's summons), sorted."""
    from .deck import Deck

    key = str(Path(deck).resolve())
    if key not in _own_turn_dead_cache:
        out = set()
        for code in set(Deck.from_ydk(Path(deck)).main):
            if code in OWN_TURN_DEAD or _DRAW_ON_THEIR_SUMMON.search(card_text(code)):
                out.add(code)
        _own_turn_dead_cache[key] = sorted(out)
    return _own_turn_dead_cache[key]


# Defender pieces that only NEGATE a face-up card's effects (patch 0011): they
# wait while the searcher controls nothing live to negate -- "any": a face-up
# card, "mon": a face-up monster. Traced 2026-11-21 (imported vs Angelechy going
# first, 273 games): Evolzar Lars negated a Normal Summoned Mulcharmy Purulia /
# Fuwalos / Droll / Ash in 5 of its 12 uses on their turn 2 (Purulia has no
# field effect; the Angelechy Problem beside it was the live target), and
# Dragostapelia put its counter on one in 10 of 73, every one a lost game.
NEGATE_ONLY = {35103106: "any",        # Evolzar Lars: a face-up card
               69946549: "mon"}        # Predaplant Dragostapelia: a face-up monster
# None: emit the 0011 flags when ROOTED_BIN has them; an A/B arm on another
# build sets True / False.
NEGATE_LIVE = None
_bin_help = {}


def _negate_live():
    if NEGATE_LIVE is not None:
        return NEGATE_LIVE
    key = str(ROOTED_BIN)
    if key not in _bin_help:
        _bin_help[key] = subprocess.run([key, "--help"], capture_output=True,
                                        text=True).stdout if ROOTED_BIN.exists() else ""
    return "--opp-negate" in _bin_help[key]


def field_dead(deck):
    """`deck`'s monsters with no field effect -- the known hand traps and the
    own-turn-dead family (Mulcharmies, Maxx "C", Nibiru, Droll): on the field
    they are bodies, nothing a negate can stop (patch 0011 --opp-dead)."""
    from ygorl.engine.duel import default_cards

    from .deck import Deck
    cards = default_cards()
    main = set(Deck.from_ydk(Path(deck)).main)
    known = set(scenarios.HANDTRAPS) | set(own_turn_dead(deck))
    return sorted(c for c in main & known if cards.get(c) and cards.get(c).type & 0x1)   # TYPE_MONSTER


def own_turn_args(deck):
    """Flags for the side SEARCHING its own turn with `deck`: its
    `own_turn_dead` cards are never chained (patch 0009, the searcher only:
    the defender's copies of the same code still fire) nor activated from
    the hand in an open state; and none of its negates goes on its own chain
    link (--no-self-negate). [] without a deck."""
    if not deck:
        return []
    # --no-self-negate (upstream discipline, never passed before 2026-11-20): no
    # NEGATION effect of the searcher's deck on a chain link of its own. Going
    # second our break chained Ash onto our OWN link in 15 of 480 games.
    args = ["--no-self-negate"]
    if _negate_live():
        dead = field_dead(deck)
        if dead:
            args += ["--opp-dead", "|".join(map(str, dead))]
    codes = own_turn_dead(deck)
    if not codes:
        return args
    args += ["--no-own-chain", "|".join(map(str, codes))]
    for c in codes:
        args += ["--no-activate", f"{c}@hand"]
    return args


def require():
    if not ROOTED_BIN.exists():
        raise RuntimeError(
            f"the rooted solver is not built ({ROOTED_BIN}); run "
            "tools/build_rooted_solver.sh")
    helptext = subprocess.run([str(ROOTED_BIN), "--help"], capture_output=True,
                              text=True).stdout
    if DEFENDER == "smart" and "--opp-smart" not in helptext:
        raise RuntimeError(
            f"the rooted solver at {ROOTED_BIN} predates patch 0004 (no "
            "--opp-smart); rebuild it with tools/build_rooted_solver.sh")
    if NO_SELF_TARGET and "--no-self-target" not in helptext:
        raise RuntimeError(
            f"the rooted solver at {ROOTED_BIN} predates patch 0007 (no "
            "--no-self-target); rebuild it with tools/build_rooted_solver.sh")
    if OWN_TURN_DEAD and "--no-own-chain" not in helptext:
        raise RuntimeError(
            f"the rooted solver at {ROOTED_BIN} predates patch 0009 (no "
            "--no-own-chain); rebuild it with tools/build_rooted_solver.sh")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- running the rooted solver -----------------------------------------------

# A solver that dies by SIGSEGV is re-run once (exit -11 here, 139 through a
# shell, 255 from ssh when the remote command was killed and the device is up).
# Measured 2026-11-19 over 19,155 stored logs: 8 of 631 NRPA-level-3 searches
# (budgets past 180 s: the 8x break) crashed, 6 of 18,523 at level 2 -- and
# the crashing command ran clean under gdb: timing, not the position.
CRASH_RETRIES = 1
_CRASH_RCS = (-11, 139, 255)


def _hard_limit(ms):
    """Seconds before a solver given `ms` counts as hung: its budget twice
    over plus start-up, so a slow but working search is never cut off."""
    return 2 * ms // 1000 + 120


def _solve(ref, args, scratch, label, ms, seed, threads=2, binary=None,
           env_extra=None):
    """One rooted-solver run. Returns {"solution", "approach", "log"} paths.
    `binary`: another build of it (an A/B arm); default ROOTED_BIN.
    `env_extra`: environment overrides for this run only."""
    from ygorl.solver.combo_solver import Workdir

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    wd = Workdir.create(scratch / f"wd-{label}")
    out = scratch / f"out-{label}"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [str(binary or ROOTED_BIN), str(ref), "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    cmd += [str(a) for a in args]
    cmd += ["--solve-ms", str(ms), "--threads", str(threads),
            "--seed", str(seed), "--outdir", str(out)]
    from . import remote
    dev = remote.current()
    log = scratch / f"{label}.log"
    earlier = ""
    for attempt in range(1 + CRASH_RETRIES):
        try:
            if dev.remote:
                r = _solve_remote(dev, cmd, {**ARENA_ENV, **(env_extra or {})},
                                  _hard_limit(ms), wd.path, out)
            else:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   env={**engine.env(), **ARENA_ENV, **(env_extra or {})},
                                   timeout=_hard_limit(ms))
        except subprocess.TimeoutExpired as e:
            # Measured 2026-11-03: a 60 s turn-2 solve still running after 10.5 h,
            # ignoring SIGTERM, held one of the run's workers the whole time.
            # subprocess.run SIGKILLs it; the game is not recorded, so the next
            # run retries it -- never scored off a half-searched turn.
            raise RuntimeError(f"{label}: solver hung past {_hard_limit(ms)} s, killed") from e
        text = ((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or "")
                + f"\n--- exit {r.returncode} ---\n")
        if r.returncode not in _CRASH_RCS or attempt == CRASH_RETRIES:
            break
        # A segfault, not a verdict: re-run the same search once (same seed --
        # the crash is timing-dependent, the run that follows is a full one).
        earlier += text + f"=== crashed (exit {r.returncode}): re-run {attempt + 1} ===\n"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
    log.write_text(earlier + text, encoding="utf-8")
    if r.returncode != 0:
        # Every stored game's solve exits 0, won or not (12325 logs, 2026-11-18):
        # anything else is a crash, and reading "no solution" off it would score
        # a search that never finished -- as a timeout, the game is not recorded.
        raise RuntimeError(f"{label}: solver exited {r.returncode} (log {log})")
    sols = sorted(out.glob("solution_*.yrp"))
    appr = sorted(out.glob("best_approach*.yrp"))
    return {"solution": sols[0] if sols else None, "solutions": sols,
            "approach": appr[0] if appr else None,
            "log": log, "rc": r.returncode}


def solve_stats(log):
    """What a solve's log says about how hard it worked, beyond won or not
    (Stage 15: a richer label per kill check). {first_win_s, wins, states,
    seconds, lines}: first_win_s is the process clock at the rollouts' first
    win (None: none there -- a solution then came from the finisher, or
    there is none); states sums the probe and every rollout phase;
    rollout_s is the rollout phases' time (the finisher's comes after)."""
    import re

    text = Path(log).read_text(encoding="utf-8", errors="replace")
    out = {"first_win_s": None, "wins": 0, "states": 0, "rollout_s": None, "lines": 0}
    m = re.search(r"first win at ([\d.]+) s .*?(\d+) win\(s\) recorded", text)
    if m:
        out["first_win_s"], out["wins"] = float(m.group(1)), int(m.group(2))
    out["states"] = sum(int(x) for x in re.findall(r"rollouts\s+(\d+) states", text))
    probe = re.search(r"^\s*(\d+) states, [\d.]+ s: at best", text, re.M)
    if probe:
        out["states"] += int(probe.group(1))
    m = re.search(r"^\s*total: ([\d.]+) s", text, re.M)
    if m:
        out["rollout_s"] = float(m.group(1))
    m = re.search(r"(\d+) line\(s\) reaching the board", text)
    if m:
        out["lines"] = int(m.group(1))
    return out


def _solve_remote(dev, cmd, env, limit_s, workdir, outdir):
    """One solve on another machine (skills/remote.py). A lost connection
    wakes the device and retries ONCE; a second loss ends the job rather
    than marking every remaining game failed one by one."""
    from . import remote
    env = {"YGORL_THIRD_PARTY": str(engine.THIRD_PARTY), **env}
    try:
        return remote.run(dev, cmd, env, limit_s, push_dirs=[workdir], outdir=outdir)
    except remote.DeviceDown:
        remote.ensure_up(dev)
        return remote.run(dev, cmd, env, limit_s, push_dirs=[workdir], outdir=outdir)


def _respond(codes):
    codes = sorted({int(c) for c in codes if c})
    return ["--opp-respond", "|".join(map(str, codes))] if codes else []


# --- the defender (whoever is not searching) ----------------------------------

# "smart" = patch 0004: the defender answers with the piece ranked first in
# its list, holds board pieces until the attacker has a monster, and picks
# follow-up targets by value. "greedy" = patch 0002's defender: the first
# eligible option, first legal follow-up -- which, measured on the 2400
# going-first games of 2026-11-09, stopped nothing in 79% of the enemy's
# turns and in one traced game banished its own Lars with its own S:P Little
# Knight at the first window of the turn.
DEFENDER = "smart"
# The FAMILY of defences a turn tries (patch 0004 `--opp-hold-until`): board
# pieces held until the attacker controls this many monsters. A fixed script
# is visible to the attacker's rollouts (one bait activation, then
# effect-less summons into lethal -- traced), so the defending side gets to
# pick, per turn, the script that serves it best: on their turn the one
# that stops the kill (then the least damage); on your lethal turn you must
# win against EVERY one. Costs one kill search per entry, one lethal search
# per entry for won turns. (1,) = a single script.
DEFENCES = (1, 2)
# Named scripts a DEFENCES entry may also be (an int n = SCRIPTS-free
# "hold board pieces until n monsters", as above). Candidates measured by
# tools/experiments/defender_family.py: answer order (`respond_order`
# tiers), what is held, and until when. hold_until 0 = nothing held.
SCRIPTS = {
    "neg1": {"order": "negates", "hold_until": 1},   # negates before hand traps
    "rev1": {"order": "reverse", "hold_until": 1},   # same tiers, last-listed first
    "all1": {"hold_traps": True, "hold_until": 1},   # hand traps wait too
    "all2": {"hold_traps": True, "hold_until": 2},
}


def _interrupts(code, cards):
    from ygorl.solver.blocking import card_interruptions
    try:
        return card_interruptions(code, cards)
    except Exception:  # noqa: BLE001 -- a card the scripts cannot read: no interruption
        return ()


def _is_negate(code, cards):
    return any(i.negate for i in _interrupts(code, cards))


def is_hand_trap(code, cards):
    """A card the defender plays from its HAND on the attacker's turn: the
    known list, or a script-read interruption usable from the hand (the
    Mulcharmies, Nibiru...)."""
    return code in scenarios.HANDTRAPS or any(i.where == "hand"
                                              for i in _interrupts(code, cards))


def respond_codes(deck, cards=None, extra=()):
    """Everything in `deck`'s main deck the defender may answer with: the
    known hand traps plus every card whose script interrupts the opponent's
    turn from anywhere (hand, set, field, grave), and `extra`. The core only
    offers what is legal where the card sits, so a trap still in hand is
    never fired; listing it costs nothing. Measured 2026-11-13 on the 237
    stored turn 1s vs Angelechy-Meta: the old list (`scenarios.HANDTRAPS`
    cut to the deck) was Ash, Droll and Ghost Ogre, so Mulcharmy Fuwalos
    and Purulia, three copies each in that deck, never fired once."""
    from .deck import Deck
    if cards is None:
        from ygorl.engine.duel import default_cards
        cards = default_cards()
    main = set(Deck.from_ydk(Path(deck)).main)
    return sorted({c for c in main if c in scenarios.HANDTRAPS or _interrupts(c, cards)}
                  | {int(c) for c in extra})


def respond_order(codes, cards, order="traps"):
    """The defender's pieces, earliest answers first. "traps": hand traps,
    then negates, then the rest, numeric within a tier; "negates": negates
    first, then hand traps; "reverse": the "traps" tiers, last code first."""
    codes = sorted({int(c) for c in codes if c})
    trap = {c: is_hand_trap(c, cards) for c in codes}
    neg = {c: _is_negate(c, cards) for c in codes}
    if order == "negates":
        key = {c: (0 if neg[c] else 1 if trap[c] else 2, c) for c in codes}
    elif order == "reverse":
        key = {c: (0 if trap[c] else 1 if neg[c] else 2, -c) for c in codes}
    elif order == "traps":
        key = {c: (0 if trap[c] else 1 if neg[c] else 2, c) for c in codes}
    else:
        raise ValueError(f"unknown respond order {order!r}")
    return sorted(codes, key=key.get)


def defender_args(respond, cards=None, smart=None, hold_until=1, order="traps",
                  hold_traps=False):
    """Solver flags for the non-searching side. `respond`: the codes it may
    use (ordered by `respond_order(order)`); with the smart defender, every
    piece that is not a hand trap (every piece, with `hold_traps`) is held
    until the attacker controls `hold_until` monsters (0: nothing held)."""
    smart = (DEFENDER == "smart") if smart is None else smart
    if cards is None:
        from ygorl.engine.duel import default_cards
        cards = default_cards()
    codes = respond_order(respond, cards, order)
    args = ["--opp-greedy"]
    if codes:
        args += ["--opp-respond", "|".join(map(str, codes))]
    if smart:
        args.append("--opp-smart")
        hold = [c for c in codes if hold_traps or not is_hand_trap(c, cards)]
        if _negate_live():
            for kind, flag in (("any", "--opp-negate"), ("mon", "--opp-negate-mon")):
                neg = [c for c in codes if NEGATE_ONLY.get(c) == kind]
                if neg:
                    args += [flag, "|".join(map(str, neg))]
        if hold and hold_until:
            args += ["--opp-hold", "|".join(map(str, hold))]
            if hold_until != 1:
                args += ["--opp-hold-until", str(int(hold_until))]
    return args


def defence_args(respond, defence, cards=None):
    """`defender_args` for one DEFENCES entry: an int n (hold until n) or a
    SCRIPTS name."""
    if isinstance(defence, str):
        return defender_args(respond, cards, **SCRIPTS[defence])
    return defender_args(respond, cards, hold_until=defence)


def _damage(prefix, approach, player):
    """LP the opponent of `player` lost along `approach`, from the position
    `prefix` reaches (0 without an approach)."""
    if not approach:
        return 0
    before = replay(prefix).lp[1 - player]
    pos = replay(approach)
    low = (pos.min_lp[1 - player] if pos.min_lp[1 - player] is not None
           else pos.lp[1 - player])
    return max(0, before - low)


# --- replaying a position ----------------------------------------------------

@dataclass
class Position:
    responses: list               # from the duel's start, raw bytes
    board: dict                   # targets.board_summary at the end
    turn: int
    lp: tuple
    winner: int = None            # set when the duel ended
    min_lp: list = field(default_factory=lambda: [None, None])


def replay(source, responses=None, until_turn=None, free_set=None):
    """Replay `responses` (default: the file's own) from `source`'s start.

    `until_turn`: then play PASSIVELY (end phase, pass, no) until that turn
    begins -- what closes a turn whose line stopped mid-way, for either
    player (ygorl's convert_line only closes turn 1). Returns a Position.

    `free_set`: (player, codes) -- while closing, that player SETS each of
    these cards still in its hand during its own turn (`free_sets`).
    """
    from ygorl.engine.branch import actions_for_response
    from ygorl.engine.duel import DuelSession
    from ygorl.engine.replay import Replay
    from ygorl.solver.demo import (MAX_CLOSING_STEPS, PASSIVE_KINDS,
                                   canonical_response)
    from ygorl.solver.targets import board_summary

    rep = Replay.from_yrp(Path(source))
    wanted = list(rep.responses if responses is None else responses)
    session = DuelSession(rep.duel())
    tracker = session.tracker
    low = [None, None]

    def watch():
        for p in (0, 1):
            v = tracker.lp[p]
            low[p] = v if low[p] is None else min(low[p], v)

    try:
        for k, raw in enumerate(wanted):
            point = session.point
            if point is None:
                break      # the duel ended inside the line: a win, read below
            plan = actions_for_response(point.state,
                                        canonical_response(point.decision, raw))
            if plan is None:
                raise RuntimeError(f"response {k} ({raw.hex()}) has no action "
                                   f"for {point.decision.name}")
            for idx in plan:
                session.act(idx)
            watch()
            if tracker.result.retries:
                raise RuntimeError(f"response {k}: MSG_RETRY")
        steps = 0
        setter, to_set = free_set or (None, ())
        while until_turn and not session.done and tracker.turn < until_turn:
            point = session.point
            if point is None:
                break
            kinds = [a.kind for a in point.actions]
            put = (next((i for i, a in enumerate(point.actions)
                         if a.kind == "sset" and a.card and a.card.code in to_set), None)
                   if point.player == setter and point.turn_player == setter else None)
            session.act(put if put is not None else
                        next((kinds.index(k) for k in PASSIVE_KINDS if k in kinds), 0))
            watch()
            steps += 1
            if steps > MAX_CLOSING_STEPS:
                raise RuntimeError("closing the turn did not end")
        res = tracker.result
        # DuelResult.winner is in deck order (a, b); replays seat a as
        # engine player 0 (first=0), so it is the engine player too.
        winner = res.winner if session.done and res.reason == "win" else None
        board = board_summary(session.core, tracker.turn,
                              (tracker.lp[0], tracker.lp[1]))
        return Position(list(res.responses), board, tracker.turn,
                        (tracker.lp[0], tracker.lp[1]), winner, low)
    finally:
        session.close()


def _winner(path):
    """Who won the duel this replay plays (None if it did not end)."""
    import ygorl.engine.messages as M
    from ygorl.engine.replay import Replay

    rep = Replay.from_yrp(Path(path))
    won = {}

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                if type(m).__name__ == "Win":
                    won.setdefault("p", m.player)

    rep.duel().replay(rep.responses, observer=_Obs())
    return won.get("p")


def _pieces(board, player, cards):
    """That player's interruption pieces on `board`, less the ones that cannot
    fire there (`drill.dead_on_field`: a bare Desirae, Trivikarma without Visas,
    an Xyz short of materials; `drill.dead_in_hand`: a Mulcharmy held with a board)."""
    from ygorl.solver.blocking import board_interruptions

    from .drill import dead_in_hand, dead_on_field
    dead = dead_on_field(board, cards, player)
    dead_h = dead_in_hand(board, cards, player)
    return [c for c, w, _n in board_interruptions(board, cards, player).pieces
            if not (w in ("field", "set") and cards.canonical(c) in dead)
            and not (w == "hand" and cards.canonical(c) in dead_h)]


def _hand(board, player):
    return list(board["players"][player].get("hand") or [])


# --- the deck under the hand ---------------------------------------------------

def ordered_deck(deck_path, order, hand, out_path, hand_first=True):
    """Write `deck_path` with its main deck in `order` -- the shuffle the
    trainer solved this hand on, hand at the tail (`trainer.hand_order`).

    The solver forces a hand by removing the FIRST copy of each hand card
    from the list and appending the hand (BuildSyntheticStart, under
    DUEL_PSEUDO_SHUFFLE), so for `--deck` the file is written hand-first:
    what remains is exactly the trainer's rest, in the trainer's order.
    Seated on side b (going second, no `--hand`), the order is written as
    is: that side draws off the end. Returns (path, the hand in the order's
    own tail order).

    Why it matters, measured 2026-11-13: the games used to force the hand
    onto the .ydk FILE order, the same for every hand, and 23 of the 24
    trainer best lines of `imported` break on it within their first
    decisions (a Tearlaments deck: the mills differ). The goal the game
    aimed turn 1 at was reached with cards the game never got to mill.
    """
    from .deck import Deck

    d = Deck.from_ydk(Path(deck_path))
    order = [int(c) for c in order]
    hand = [int(c) for c in hand]
    if sorted(order) != sorted(d.main):
        raise ValueError(f"{Path(deck_path).stem}: the order is not this deck's main deck")
    tail = order[-len(hand):]
    if sorted(tail) != sorted(hand):
        raise ValueError(f"{Path(deck_path).stem}: the order does not end with the hand")
    main = tail + order[:-len(hand)] if hand_first else list(order)
    Deck(name=d.name, main=main, extra=list(d.extra), side=list(d.side)).write_ydk(Path(out_path))
    return Path(out_path), tail


# --- the three turns -----------------------------------------------------------

# On OUR turn 1 the enemy has only hand traps; which of these scripts it plays
# (fire at once / wait until we control 1 / 2 monsters, game.SCRIPTS). More than
# one: turn 1 is solved against each and the enemy keeps the one leaving us the
# fewest live pieces. ("first",) = today's single script. Measured 2026-11-20
# (tools/experiments/t1_enemy_script.py, 28 positions, converged kill check):
# killed 71% (first) / 86% (all1) / 79% (all2); "all1 always" newly killed 4 and
# saved 0 (p = 0.125); the fewest-pieces PICK only 75% -- pieces do not predict
# kills. The switch is therefore ("all1",), one solve, once that test is
# conclusive (6 / 0 would be p = 0.03).
T1_ENEMY_SCRIPTS = ("first",)


def enemy_script_args(respond, script, cards=None):
    """defender_args for a T1_ENEMY_SCRIPTS entry."""
    if script == "first":
        return defender_args(respond, cards)
    return defender_args(respond, cards, **SCRIPTS[script])


def turn1_vs_scripts(our_deck, enemy_deck, hand, goal, enemy_seed, scratch, ms, seed,
                     enemy_respond, start=None, sets=None, scripts=None, cards=None):
    """`turn1` against each enemy script; the board the ENEMY prefers (fewest
    live pieces of ours, ties to the earlier script), with `script` set."""
    scripts = tuple(scripts or T1_ENEMY_SCRIPTS)
    if scripts == ("first",):
        t = turn1(our_deck, enemy_deck, hand, goal, enemy_seed, scratch, ms, seed,
                  enemy_respond, start=start, sets=sets)
        return {**t, "script": "first"} if t else None
    if cards is None:
        from ygorl.engine.duel import default_cards
        cards = default_cards()
    best = None
    for k, sc in enumerate(scripts):
        t = turn1(our_deck, enemy_deck, hand, goal, enemy_seed, Path(scratch) / f"s-{sc}", ms,
                  seed, [], start=start, sets=sets,
                  extra=enemy_script_args(enemy_respond, sc, cards))
        if t is None:
            continue
        n = len(_pieces(t["board"], 0, cards))
        if best is None or n < best[0]:
            best = (n, {**t, "script": sc})
    return best[1] if best else None


def turn1(our_deck, enemy_deck, hand, goal, enemy_seed, scratch, ms, seed,
          enemy_respond, start=None, extra=(), sets=None):
    """Your turn 1 against the real enemy deck, closed at turn 2.

    `start`: the trainer's duel for the hand (`trainer.hand_start`: the
    main deck's order and the core seed), so the search plays the hand the
    trainer solved -- same mills, same shuffles -- with the real enemy in
    the other seat; default the .ydk file's order and the enemy seed's
    randomness. `extra`: more solver flags. Returns {start, prefix, board,
    reached} or None when not even a partial line came back.
    """
    from ygorl.engine.replay import Replay

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    start = start or {}
    tpl = breaking.template_vs(scratch / "tpl.yrpX", our_deck, enemy_deck,
                               seed=enemy_seed, shuffle=True,
                               core_seed=start.get("core_seed"))
    deck = Path(our_deck).resolve()
    if start.get("order"):
        deck, hand = ordered_deck(our_deck, start["order"], hand, scratch / "ordered.ydk")
    args = ["--no-ref", "--deck", deck,
            "--hand", "|".join(map(str, hand))]
    for t in goal:
        args += ["--target", t]
    args += (defender_args(enemy_respond) + self_target_args() + own_turn_args(our_deck)
             + list(extra))
    r = _solve(tpl, args, scratch, "t1", ms, seed)
    src = r["solution"] or r["approach"]
    if src is None:
        return None
    pos = replay(src, until_turn=2,
                 free_set=(0, set(free_sets(our_deck) if sets is None else sets)))
    start, prefix = breaking.split_position(Replay.from_yrp(src), pos.responses,
                                            scratch / "t1")
    return {"start": start, "prefix": prefix, "board": pos.board,
            "reached": r["solution"] is not None, "log": str(r["log"])}


def turn2(start, prefix, enemy_target, our_respond, scratch, ms, seed,
          otk_ms=OTK_MS, defences=None, targets=None, our_deck=None, enemy_deck=None):
    """The enemy's turn into your board. Returns {lost, start, prefix, board,
    reached, defence}; `lost` means they found lethal against EVERY defence
    in `defences` (default `DEFENCES`); otherwise the defence that kept the
    most LP plays the rest of their turn. If the duel ended on their turn
    the result is only {lost, won, line}.

    `targets`: the board the enemy builds when it cannot kill (its trained
    goal for this hand, `trainer.enemy_goal`); default `enemy_target`. The
    kill checks are --goal-win and ignore it. `our_deck`: your list, so that
    of the lines reaching that board the one giving you the fewest monsters
    is played (`fewest_gifts`). `enemy_deck`: theirs, for `own_turn_args`."""
    from ygorl.engine.replay import Replay

    scratch = Path(scratch)
    defences = tuple(defences or DEFENCES) or (1,)
    base = ["--start", start, "--root", prefix, "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2"] \
        + own_turn_args(enemy_deck)
    chosen, first_kill = defences[0], None
    if otk_ms:
        best = None
        for v in defences:
            label = "t2-otk" if v == defences[0] else f"t2-otk-h{v}"
            k = _solve(prefix, base + defence_args(our_respond, v)
                       + ["--goal-win", "--stop-on-win", str(STOP_ON_WIN)],
                       scratch, label, otk_ms, seed)
            if k["solution"] and _winner(k["solution"]) == 1:
                first_kill = first_kill or str(k["solution"])
                continue
            taken = _damage(prefix, k["approach"], 1)
            if best is None or taken < best[0]:
                best = (taken, v)
        if best is None:
            return {"lost": True, "line": first_kill, "defence": None}
        chosen = best[1]
    build = base
    if targets:
        at = base.index("--target")
        build = base[:at] + base[at + 2:] + [a for t in targets for a in ("--target", t)]
    r = _solve(prefix, build + defence_args(our_respond, chosen),
               scratch, "t2", ms, seed)
    # No line at all: the enemy passes its turn from the root.
    src = (fewest_gifts(r.get("solutions") or [], our_deck, player=0, until_turn=3)
           if our_deck else r["solution"]) or r["approach"] or prefix
    pos = replay(src, until_turn=3, free_set=(1, set(free_sets(enemy_deck))))
    if pos.winner is not None:   # e.g. their closing turn ended the duel
        return {"lost": pos.winner == 1, "won": pos.winner == 0,
                "line": str(src)}
    start2, prefix2 = breaking.split_position(Replay.from_yrp(start),
                                              pos.responses, scratch / "t2")
    return {"lost": False, "start": start2, "prefix": prefix2,
            "board": pos.board, "reached": r["solution"] is not None,
            "log": str(r["log"]), "defence": chosen}


def steer(goal, fallback):
    """The target a lethal search (our turn 3, or our break going second)
    is steered by: a BOARD piece of the hand's turn-1 goal. A goal that
    opens with a hand trap kept in hand ("42141493@hand") posts an empty
    target, and every line the search reached was then rejected: going
    first, the 11 of 24 hands whose goal opens that way won 6 of the 564
    turn 3s they reached, the rest 623 of 728 (found by Stage 13's blank
    gate, 2026-11-18)."""
    return next((t for t in goal if not str(t).endswith("@hand")), fallback)


def turn3(start, prefix, our_target, enemy_respond, scratch, ms, seed,
          player=0, turn=3, extra=(), binary=None, env_extra=None,
          defences=None, our_deck=None):
    """Your lethal turn: turn 3 as player 0 going first, turn 2 as player 1
    going second. A win must beat EVERY defence in `defences` (default
    `DEFENCES`): the enemy picks the one that stops you. Returns {won,
    damage, line, defence} -- the line and damage of the first defence that
    held, or the winning line against the last one. `extra` / `binary`: more
    solver flags / another build (A/B arms). `our_deck`: yours, for
    `own_turn_args`."""
    scratch = Path(scratch)
    defences = tuple(defences or DEFENCES) or (1,)
    out = None
    for v in defences:
        label = f"t{turn}" if v == defences[0] else f"t{turn}-h{v}"
        args = ["--start", start, "--root", prefix, "--no-ref",
                "--target", our_target, "--player", str(player), "--turns", str(turn),
                "--goal-win", "--stop-on-win", str(STOP_ON_WIN)] \
            + defence_args(enemy_respond, v) + self_target_args() + own_turn_args(our_deck) \
            + list(extra)
        r = _solve(prefix, args, scratch, label, ms, seed, binary=binary,
                   env_extra=env_extra)
        if r["solution"] and _winner(r["solution"]) == player:
            out = {"won": True, "damage": None, "line": str(r["solution"]),
                   "defence": v}
            continue
        return {"won": False, "damage": _damage(prefix, r["approach"], player),
                "line": str(r["approach"]) if r["approach"] else None,
                "defence": v}
    return out


# --- the searched defender (Stage 14, patch 0006) ------------------------------
#
# A fixed script is readable: the attacker's rollouts see what it will do and
# play around it (tools/experiments/defender_family.py: 8 scripts stopped no
# more converged kills than 2). The searched defender answers on the attacker's
# OWN line instead: at each of its chain windows on the winning line it tries
# the replies it did not make (`_branches`), and the attacker is searched again
# from just after each. A reply the attacker cannot beat (twice: a search can
# miss a kill) becomes a RULE, and the attacker re-plans from the root against
# it -- a rooted re-search alone would freeze the attacker's moves before the
# window, which flatters the defender.

ROUNDS = 3           # rule rounds before "the defender always had a reply"
MAX_BRANCHES = 8     # replies tried per attacker line, in line order
CONFIRM_SEED = 7919  # the second seed a saving reply must hold on


def _branches(line, args, scratch, label):
    """The defender's other replies on `line` (patch 0006 --opp-branches),
    `args` the search's own flags (--start/--root/--player/defender). Runs
    here: it is a replay, a second or two. [{k, at, trigger, mons, chosen,
    alt, file}] in line order."""
    from ygorl.solver.combo_solver import Workdir

    scratch = Path(scratch)
    wd = Workdir.create(scratch / f"wd-{label}")
    out = scratch / f"br-{label}"
    shutil.rmtree(out, ignore_errors=True)
    cmd = [str(ROOTED_BIN), str(line), "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    cmd += [str(a) for a in args] + ["--opp-branches", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, env=engine.env(), timeout=300)
    f = out / "branches.jsonl"
    if r.returncode != 0 or not f.exists():
        raise RuntimeError(f"{label}: --opp-branches failed (exit {r.returncode}): "
                           f"{(r.stdout or '')[-300:]}")
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def rule_args(rules):
    """Solver flags for learnt rules: ("fire"|"skip", piece, trigger, mons)."""
    out = []
    for kind, piece, trigger, mons in rules:
        out += ["--opp-rule" if kind == "fire" else "--opp-skip",
                f"{int(piece)}:{int(trigger)}@{int(mons)}"]
    return out


def _rule_of(b):
    """The rule that makes the defender give branch `b`'s reply again."""
    if b["alt"]:
        return ("fire", b["alt"], b["trigger"], b["mons"])
    return ("skip", b["chosen"], b["trigger"], b["mons"])


def searched_defence(start, prefix, target, player, turns, respond, scratch, ms, seed,
                     defence=None, rounds=ROUNDS, max_branches=MAX_BRANCHES):
    """Can `player` win (lethal by turn `turns`) against a defender that
    searches its replies? `respond`: the defender's pieces, `defence` the
    script it starts from (default DEFENCES[0]). Returns {won, why, rules,
    searches, line}: why = "policy" (no win against the script + rules),
    "every reply" (the attacker beat every reply on its line), "rounds"
    (the defender still had a reply after `rounds` rule rounds)."""
    scratch = Path(scratch)
    defence = DEFENCES[0] if defence is None else defence
    base = ["--start", start, "--no-ref", "--target", target,
            "--player", str(player), "--turns", str(turns)]
    goal = ["--goal-win", "--stop-on-win", str(STOP_ON_WIN)]
    rules, searches, line = [], 0, None

    def wins(root, label, s):
        nonlocal searches
        searches += 1
        args = base + ["--root", root] + defence_args(respond, defence) \
            + rule_args(rules) + goal
        r = _solve(root, args, scratch, label, ms, s)
        ok = bool(r["solution"]) and _winner(r["solution"]) == player
        return r["solution"] if ok else None

    for rnd in range(rounds):
        line = wins(prefix, f"sd{rnd}", seed + rnd)
        if not line:
            return {"won": False, "why": "policy", "rules": rules,
                    "searches": searches, "line": None}
        flags = base + ["--root", prefix] + defence_args(respond, defence) + rule_args(rules)
        saved = None
        for b in _branches(line, flags, scratch, f"sd{rnd}")[:max_branches]:
            label = f"sd{rnd}-b{b['k']}"
            if wins(b["file"], label, seed + rnd):
                continue
            if wins(b["file"], label + "-c", seed + rnd + CONFIRM_SEED):
                continue
            saved = b
            break
        if saved is None:
            return {"won": True, "why": "every reply", "rules": rules,
                    "searches": searches, "line": str(line)}
        new = _rule_of(saved)
        # the same window keyed both ways: the new reply replaces the old one
        rules = [r for r in rules if r[1:] != new[1:]] + [new]
    return {"won": False, "why": "rounds", "rules": rules, "searches": searches,
            "line": str(line)}


def enemy_hand(our_deck, enemy_deck, enemy_seed, scratch):
    """The enemy's opening hand for `enemy_seed`, sorted -- exactly the one
    it holds going first (checked against stored games 2026-11-06), so both
    sides of a matchup face the same enemy hands."""
    tpl = breaking.template_vs(Path(scratch) / "deal.yrpX", our_deck, enemy_deck,
                               seed=enemy_seed, shuffle=True)
    return sorted(_hand(replay(tpl).board, 1))


def enemy_turn1(our_deck, enemy_deck, hand, enemy_seed, enemy_target, scratch,
                ms, seed, our_respond, start=None, trained=None):
    """Going second: the enemy's turn 1 into your hand, closed at turn 2.

    `start`: the trainer's duel for your hand (`trainer.hand_start`): your
    deck in its order, the core on its seed; without it your deck is
    shuffled by the enemy seed. Returns {start, prefix,
    board, reached, enemy_hand}, or None when not even a partial line came
    back -- the bare template would deal them the bottom of their
    unshuffled list, not their hand, so there is no honest position to play
    from (recorded as no_t1, as going first).

    `trained`: (hand, goal) of this enemy hand from the trainer's enemy pool
    (`trainer.enemy_goal`); its board targets replace `enemy_target`. The
    hand must be the one dealt here, or the goal belongs to another duel."""
    import random

    from ygorl.engine.replay import Replay

    from .deck import Deck

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    ehand = enemy_hand(our_deck, enemy_deck, enemy_seed, scratch)
    targets = [enemy_target]
    if trained:
        if sorted(trained[0]) != ehand:
            raise RuntimeError(f"enemy hand {enemy_seed}: trained on {sorted(trained[0])}, "
                               f"dealt {ehand}")
        targets = trainer.split_goal(trained[1])[0] or targets
    start = start or {}
    if start.get("order"):
        ours, _tail = ordered_deck(our_deck, start["order"], hand, scratch / "ours.ydk",
                                   hand_first=False)
    else:
        # Your deck, shuffled by the enemy seed, with the hand at the bottom
        # where the unshuffled side b draws it (breaking.deck_with_hand).
        d = Deck.from_ydk(Path(our_deck))
        shuffled = scratch / "ours-shuffled.ydk"
        main = list(d.main)
        random.Random(enemy_seed).shuffle(main)
        Deck(name=d.name, main=main, extra=list(d.extra), side=list(d.side)).write_ydk(shuffled)
        ours = breaking.deck_with_hand(shuffled, hand, scratch / "ours.ydk")
    tpl = breaking.template_vs(scratch / "tpl.yrpX", enemy_deck, ours,
                               seed=enemy_seed, shuffle=False,
                               core_seed=start.get("core_seed"))
    args = ["--no-ref", "--deck", Path(enemy_deck).resolve(),
            "--hand", "|".join(map(str, ehand))] \
        + [a for t in targets for a in ("--target", t)] + defender_args(our_respond) \
        + own_turn_args(enemy_deck)
    r = _solve(tpl, args, scratch, "t1", ms, seed)
    src = fewest_gifts(r.get("solutions") or [], our_deck) or r["approach"]
    if src is not None and usable_gifts(src, 1, our_deck, 2):
        src = _regift(tpl, args, scratch, ms, seed, our_deck, r) or src
    if src is None:
        return None
    dealt = replay(src, []).board
    for player, want in ((1, hand), (0, ehand)):
        if sorted(_hand(dealt, player)) != sorted(want):
            raise RuntimeError(f"player {player}'s hand was not seated: wanted "
                               f"{sorted(want)}, the duel dealt "
                               f"{sorted(_hand(dealt, player))}")
    pos = replay(src, until_turn=2, free_set=(0, set(free_sets(enemy_deck))))
    start, prefix = breaking.split_position(Replay.from_yrp(src), pos.responses,
                                            scratch / "t1")
    return {"start": start, "prefix": prefix, "board": pos.board,
            "reached": r["solution"] is not None, "enemy_hand": ehand,
            "log": str(r["log"])}


def other_method_games(conn, dh, enemy, first, device):
    """Games of that section played by another method than METHOD (rows from
    before the stamp: created before METHOD_SINCE)."""
    return conn.execute(
        "SELECT COUNT(*) FROM game_results WHERE deck_hash=? AND enemy=? AND first=? "
        "AND device=? AND ((method IS NULL AND created < ?) OR method != ?)",
        (dh, enemy, int(bool(first)), device, METHOD_SINCE[bool(first)], METHOD)).fetchone()[0]


def live_goal(goal, deck):
    """The board targets of a trained goal (`trainer.split_goal`), less the
    cards `deck` can never make work (`drill.never_live`: Trivikarma without
    Visas). Hands 0, 1, 5, 6 of imported kept "7436169@szone:fd" in their
    stored goals after the 2026-11-18 re-score, so our turn 1 still set a dead
    Trivikarma (40 of 226 turn 1s) and -- worse -- it was the FIRST board
    piece, so `steer` aimed their lethal turn 3 and going-second break at
    setting it (traced 2026-11-20)."""
    from .deck import Deck
    from .drill import never_live

    d = Deck.from_ydk(Path(deck))
    dead = {str(c) for c in never_live([*d.main, *d.extra])}
    return [t for t in trainer.split_goal(goal)[0] if str(t).split("@")[0] not in dead]


def free_sets(deck, cards=None, extra=()):
    """`deck`'s Traps whose scripts read as an interruption (Tearlaments
    Sulliek): a turn's line that ends with one in hand sets it before
    passing (`replay(free_set=...)`). The search places only what its goal
    asks for, so our turn 1 kept Sulliek in hand in 8 of 240 going-first
    games with free zones (2026-11-20); in hand a trap answers nothing.
    `extra`: cards the player counts as interruptions (trainer prefs) that
    are Traps or Quick-Play Spells -- a Quick-Play works on the opponent's
    turn only if set (Super Polymerization)."""
    from .deck import Deck
    if not deck:
        return ()
    if cards is None:
        from ygorl.engine.duel import default_cards
        cards = default_cards()
    from .drill import never_live
    main = set(Deck.from_ydk(Path(deck)).main)
    dead = never_live(main)               # Trivikarma without Visas: nothing to set up
    out = []
    for c in sorted(main - dead):
        card = cards.get(c)
        if card and card.type & 0x4 and _interrupts(c, cards):     # TYPE_TRAP
            out.append(c)
    for c in extra:
        card = cards.get(int(c))
        if (int(c) in main and int(c) not in out and card
                and (card.type & 0x4 or card.type & 0x10000)):       # TRAP | QUICKPLAY
            out.append(int(c))
    return tuple(sorted(out))


def gifts(board, player, deck_path):
    """Monsters on `player`'s field that their deck does not run: given to
    them by the other side (Angelechy Opposition summons one to YOUR zone)."""
    from .impact import deck_codes

    mine = deck_codes(deck_path)
    mz = board["players"][player].get("mzone") or []
    codes = (m.get("code") if isinstance(m, dict) else m for m in mz if m)
    return [c for c in codes if c and c not in mine]


def usable_gifts(source, player, receiver_deck, until_turn):
    """Of the `gifts` on `player`'s field when `until_turn` begins, the
    activations they offer that player at its first main-phase prompt (the
    engine's own list: monster-zone `activate` actions of gifted codes).
    Angelechy Seneschal's effect is a summon trigger already spent; Bastion,
    Castellan, Destrier, Shatranga, Enlisted each hand the receiver a banish."""
    from ygorl.engine.branch import actions_for_response
    from ygorl.engine.duel import DuelSession
    from ygorl.engine.replay import Replay
    from ygorl.solver.demo import MAX_CLOSING_STEPS, PASSIVE_KINDS, canonical_response

    pos = replay(source, until_turn=until_turn)
    given = set(gifts(pos.board, player, receiver_deck))
    if not given or pos.winner is not None:
        return 0
    rep = Replay.from_yrp(Path(source))
    session = DuelSession(rep.duel())
    try:
        for raw in pos.responses:
            point = session.point
            for idx in actions_for_response(point.state,
                                            canonical_response(point.decision, raw)) or ():
                session.act(idx)
        for _ in range(MAX_CLOSING_STEPS):
            point = session.point
            if point is None or session.done:
                return 0
            kinds = [a.kind for a in point.actions]
            if point.player == player and point.decision.name == "MSG_SELECT_IDLECMD":
                return sum(1 for a in point.actions if a.kind == "activate" and a.card
                           and a.card.code in given and a.card.loc.location & 0x4)
            session.act(next((kinds.index(k) for k in PASSIVE_KINDS if k in kinds), 0))
        return 0
    finally:
        session.close()


GIFT_RETRIES = 2     # extra enemy turn-1 solves when its line hands us a usable monster


def _approach_progress(path):
    """X of a solver 'best_approach_XofY' file (targets placed), else -1."""
    m = re.search(r"best_approach_(\d+)of\d+", Path(path).name)
    return int(m.group(1)) if m else -1


def _regift(tpl, args, scratch, ms, seed, our_deck, first):
    """The enemy's turn 1 handed us a monster we can USE: re-solve with other
    seeds and keep, over every line found, a solution before any approach,
    approaches only at their best goal progress, then the fewest usable gifts,
    the fewest gifts. The gift rule saw only full SOLUTIONS, and the enemy's
    goal is often unreached -- its one best approach gifted freely: 5 of the
    first 13 games of the 2026-11-21 smoke batch gave us a usable Bastion /
    Castellan / Destrier / Shatranga."""
    runs = [first]
    for k in range(1, GIFT_RETRIES + 1):
        runs.append(_solve(tpl, args, Path(scratch) / f"regift{k}", "t1", ms, seed + 7919 * k))
    sols = [s for r in runs for s in (r.get("solutions") or [])]
    if sols:
        pool = sols
    else:
        apps = [r["approach"] for r in runs if r.get("approach")]
        if not apps:
            return None
        best = max(_approach_progress(a) for a in apps)
        pool = [a for a in apps if _approach_progress(a) == best]
    score = [(usable_gifts(p, 1, our_deck, 2), i) for i, p in enumerate(pool)]
    return pool[min(score)[1]]


def fewest_gifts(solutions, receiver_deck, player=1, until_turn=2):
    """The solution line whose gifts the RECEIVER can use least (`usable_gifts`),
    ties in the solver's order (a deck that never gives monsters away plays
    exactly its first line; a gift the receiver cannot use, Seneschal, is not
    avoided -- it is Angelechy's strategy). None if no lines.

    Every line reaches the same goal, but the search only scores its own
    board, so it gave the opponent whatever it liked: in 37 of 138 stored
    Angelechy turn 1s it handed us a monster (Bastion, Castellan: they banish
    ITS cards) when a line giving nothing was there (traced 2026-11-20; with
    a monster given, we won 75% of games going second, without one 56%).
    Where every line gives one (Opposition's cost), Bastion was still the
    most given (8 of 19): a banish for us, where Seneschal gives nothing."""
    if len(solutions) < 2:
        return solutions[0] if solutions else None
    n = [len(gifts(replay(s, until_turn=until_turn).board, player, receiver_deck))
         for s in solutions]
    use = [usable_gifts(s, player, receiver_deck, until_turn) if n[i] else 0
           for i, s in enumerate(solutions)]
    # a gift the receiver cannot use is NOT avoided: Angelechy GIVES Seneschal
    # (the black king) on purpose -- attacking it swaps it back (user,
    # 2026-11-21: "angelechy does have a strat that gives you a monster")
    return solutions[min(range(len(solutions)), key=lambda i: (use[i], i))]


# --- one matchup run -----------------------------------------------------------

def enemy_respond_codes(enemy_deck, cards=None):
    """What the enemy may answer your turn with (`respond_codes`)."""
    return respond_codes(enemy_deck, cards)


def games_dir(name, enemy_name, first=True, device="server"):
    """Where a matchup's game files live (going second: a `second/` beside
    the going-first hand folders; another device: `<enemy>@<device>`, so two
    machines on one matchup never share a scratch folder)."""
    root = GAMES / name / (enemy_name if device == "server" else f"{enemy_name}@{device}")
    return root if first else root / "second"


def run(conn, deck_path, name, enemy_name, hands=None, enemy_hands=ENEMY_HANDS,
        runs=RUNS, solve_ms=None, workers=None, from_end=False, first=True,
        device=None, say=print):
    """Play every (hand, enemy hand, run) game, resumable, and return the
    summary. `hands` = trainer hand indices (default: every hand the trainer
    has a best goal for). `from_end` plays the list backwards: a second
    runner started beside a running one meets it in the middle instead of
    replaying its games (each game is skipped once stored). `first=False`
    plays going second (module docstring). `device`: where the searches
    run (skills/remote.py; default this process's, i.e. the server), which
    also keys every stored row: games from machines of different speed are
    different measurements."""
    from concurrent.futures import ThreadPoolExecutor

    from ygorl.engine.duel import default_cards

    from . import db, trainer

    from . import remote

    require()
    if device:
        os.environ[remote.ENV_VAR] = device     # _solve reads it per solve
    dev = remote.current()
    device = dev.name
    workers = workers or dev.workers
    remote.prepare(dev, log=say)
    ms = solve_ms or engine.SOLVE_MS
    enemy_deck = engine.DECKS / f"{enemy_name}.ydk"
    enemy_target = scenarios.target_for(enemy_name)
    if not enemy_target:
        raise RuntimeError(f"no payoff target for {enemy_name}: run "
                           f"scenarios.ensure_target first")
    dh = trainer.deck_hash(deck_path)
    check_enemy_list(conn, dh, enemy_name, enemy_deck)
    rows = conn.execute(
        "SELECT hand_index, hand, best_goal, best_record FROM trainer_hands "
        "WHERE deck_hash=? AND best_goal IS NOT NULL ORDER BY hand_index",
        (dh,)).fetchall()
    if hands is not None:
        rows = [r for r in rows if r["hand_index"] in set(hands)]
    if not rows:
        raise RuntimeError(f"no trained hands for {name}: train it first")
    cards = default_cards()
    ours_extra = trainer.prefs(conn, name)["interrupters"]
    theirs = enemy_respond_codes(enemy_deck, cards)
    # Your answers on their turn: your deck's hand traps and interrupters
    # plus the "counts as an interruption" cards you named for the trainer.
    ours_hand = respond_codes(deck_path, cards, extra=ours_extra)
    seeds = [ENEMY_SEED0 + k for k in range(enemy_hands)]
    first = int(bool(first))
    # Each enemy hand plays toward its own trained goal where the trainer has
    # one (`train <enemy> --vs <deck>`), else enemy_target: its turn 1 going
    # second, the board it builds on turn 2 when it cannot kill going first
    # (the same 5 cards: check_shared_enemy_hands).
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        trained = {e: g for e in seeds if (g := trainer.enemy_goal(
            conn, enemy_deck, deck_path, e, with_hand=True,
            hand=enemy_hand(Path(deck_path), enemy_deck, e, tmp)))}
    if first and trained:
        old = conn.execute(
            "SELECT COUNT(*) FROM game_results WHERE deck_hash=? AND enemy=? AND first=1 "
            "AND device=? AND created < ?", (dh, enemy_name, device, FIRST_METHOD_SINCE)).fetchone()[0]
        if old:
            raise RuntimeError(
                f"{old} going-first game(s) vs {enemy_name} on {device} predate the trained "
                f"enemy turn 2 (FIRST_METHOD_SINCE): relabel them first (STAGE15)")
    old = other_method_games(conn, dh, enemy_name, first, device)
    if old:
        raise RuntimeError(
            f"{old} going-{'first' if first else 'second'} game(s) vs {enemy_name} on {device} "
            f"were played by another method than {METHOD} (game.METHOD): relabel them first "
            f"(tools/relabel_games.py ... gift)")
    say(f"enemy turn {2 if first else 1}: {len(trained)}/{len(seeds)} hand(s) on trained goals"
        + ("" if len(trained) == len(seeds) else f", the rest on {enemy_target}"))
    root = games_dir(name, enemy_name, first, device)
    say(f"{name} vs {enemy_name} going {'first' if first else 'second'}: "
        f"{len(rows)} hand(s) x {enemy_hands} enemy hand(s) x {runs} run(s), "
        f"{ms // 1000} s per search"
        f"{f' ({OTK_MS * OTK_X // 1000} s a kill check, {ms * T3_X // 1000} s your turn 3)' if first else f' ({ms * BREAK_X // 1000} s for the break)'}, "
        f"enemy target {f'trained per hand ({len(trained)}/{len(seeds)})' if trained else enemy_target}, "
        f"on {device} "
        f"({workers} at once)")

    def t1_for(row, eseed):
        conn = db.connect()   # one per thread: sqlite3 binds a connection to it
        hit = conn.execute(
            "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND "
            "hand_index=? AND board=0 AND enemy_seed=? AND first=? AND device=?",
            (dh, enemy_name, row["hand_index"], eseed, first, device)).fetchone()
        if hit:
            return dict(hit)
        scratch = root / f"h{row['hand_index']}" / f"e{eseed}" / "t1"
        scratch.mkdir(parents=True, exist_ok=True)
        # The hand is played in the duel the trainer solved it in: its deck
        # order and core seed (trainer.hand_start). A hand the trainer never
        # recorded keeps the file's order and the enemy seed's randomness.
        start = trainer.hand_start(row.get("best_record"))
        if first:
            t1 = turn1_vs_scripts(Path(deck_path), enemy_deck, json.loads(row["hand"]),
                                  live_goal(json.loads(row["best_goal"]), deck_path),
                                  eseed, scratch, ms, eseed, theirs, start=start,
                                  sets=free_sets(deck_path, cards, ours_extra), cards=cards)
        else:
            t1 = enemy_turn1(Path(deck_path), enemy_deck, json.loads(row["hand"]),
                             eseed, enemy_target, scratch, ms, eseed,
                             ours_hand, start=start, trained=trained.get(eseed))
        rec = {"deck_hash": dh, "enemy": enemy_name,
               "hand_index": row["hand_index"], "board": 0,
               "enemy_seed": eseed, "ok": int(t1 is not None)}
        if t1:
            # The OPENING hand: by turn 2 it legitimately differs (Maxx "C"
            # draws scale with your summons, used hand traps are gone).
            ehand = (_hand(replay(t1["start"]).board, 1) if first
                     else t1["enemy_hand"])
            rec.update(start=str(t1["start"]), prefix=str(t1["prefix"]),
                       reached=int(t1["reached"]),
                       pieces=json.dumps(_pieces(t1["board"], 0, cards)),  # turn 1's player
                       enemy_hand=json.dumps(sorted(ehand)))
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO game_turn1 (deck_hash, enemy, "
                "hand_index, board, enemy_seed, first, device, ok, start, prefix, reached, "
                "pieces, enemy_hand, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dh, enemy_name, rec["hand_index"], 0, eseed, first, device, rec["ok"],
                 rec.get("start"), rec.get("prefix"), rec.get("reached"),
                 rec.get("pieces"), rec.get("enemy_hand"), _now()))
        return rec

    failed = []
    down = []       # the device went away: every queued game returns at once

    def guarded(fn):
        # One game's crash (a replay MSG_RETRY, a solver that died) must not
        # end an hours-long run. Nothing is recorded for it, so the next
        # run retries it; the count is reported at the end. A device that
        # stays unreachable after a wake is different: it ends the run.
        def call(*a):
            if down:
                return None
            try:
                return fn(*a)
            except remote.DeviceDown as e:
                down.append(e)
                say(f"  !! {device} is unreachable, stopping: {e}")
                return None
            except Exception as e:  # noqa: BLE001 -- reported, then retried
                failed.append(e)
                say(f"  !! {fn.__name__} failed: {type(e).__name__}: {e}")
                return None
        return call

    def one(job):
        row, eseed, k = job
        conn = db.connect()
        done = conn.execute(
            "SELECT 1 FROM game_results WHERE deck_hash=? AND enemy=? AND "
            "hand_index=? AND board=0 AND enemy_seed=? AND first=? AND device=? AND run=?",
            (dh, enemy_name, row["hand_index"], eseed, first, device, k)).fetchone()
        if done:
            return None
        t1 = t1_for(row, eseed)
        if t1 is None:      # its turn 1 crashed: retried on the next run
            return None
        t0 = time.time()
        out = {"outcome": "no_t1"}
        if t1.get("ok") and not first:
            scratch = root / f"h{row['hand_index']}" / f"e{eseed}" / f"run{k}"
            scratch.mkdir(parents=True, exist_ok=True)
            goal = live_goal(json.loads(row["best_goal"]), deck_path)
            t2 = turn3(t1["start"], t1["prefix"], steer(goal, enemy_target),
                       set(theirs) | set(json.loads(t1["pieces"])),
                       scratch, ms * BREAK_X, eseed * 100 + k, player=1, turn=2,
                       our_deck=deck_path)
            out = {"outcome": "win" if t2["won"] else "no_win",
                   "damage": t2["damage"], "line": t2["line"],
                   "enemy_reached": int(t1["reached"])}
        elif t1.get("ok"):
            scratch = root / f"h{row['hand_index']}" / f"e{eseed}" / f"run{k}"
            scratch.mkdir(parents=True, exist_ok=True)
            seed = eseed * 100 + k
            ours = set(json.loads(t1["pieces"])) | set(ours_hand)
            tg = trained.get(eseed)
            t2 = turn2(t1["start"], t1["prefix"], enemy_target, ours,
                       scratch, ms, seed, otk_ms=OTK_MS * OTK_X,
                       targets=trainer.split_goal(tg[1])[0] if tg else None,
                       our_deck=deck_path, enemy_deck=enemy_deck)
            if t2["lost"]:
                out = {"outcome": "lost_t2", "line": t2.get("line")}
            elif t2.get("won"):  # they lost on their own turn (costs, burn)
                out = {"outcome": "win", "damage": None, "line": t2["line"],
                       "enemy_reached": 0}
            else:
                goal = live_goal(json.loads(row["best_goal"]), deck_path)
                enemy_pieces = _pieces(t2["board"], 1, cards)
                t3 = turn3(t2["start"], t2["prefix"], steer(goal, enemy_target),
                           set(theirs) | set(enemy_pieces),
                           scratch, ms * T3_X, seed, our_deck=deck_path)
                out = {"outcome": "win" if t3["won"] else "no_win",
                       "damage": t3["damage"], "line": t3["line"],
                       "enemy_reached": int(t2["reached"])}
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO game_results (deck_hash, enemy, "
                "hand_index, board, enemy_seed, first, device, run, outcome, damage, "
                "enemy_reached, line, seconds, created, method) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dh, enemy_name, row["hand_index"], 0, eseed, first, device, k,
                 out["outcome"], out.get("damage"), out.get("enemy_reached"),
                 out.get("line"), round(time.time() - t0, 1), _now(), METHOD))
        if out["outcome"] != "no_t1":
            from . import impact, stops
            g = conn.execute(
                "SELECT * FROM game_results WHERE deck_hash=? AND enemy=? AND "
                "hand_index=? AND board=0 AND enemy_seed=? AND first=? AND device=? AND run=?",
                (dh, enemy_name, row["hand_index"], eseed, first, device, k)).fetchone()
            for mod in (impact, stops):
                try:    # the replay caches; a miss is read again by fill()
                    mod.record(conn, dh, name, enemy_name, g)
                except Exception as exc:
                    say(f"  ({mod.__name__.rsplit('.', 1)[1]}: could not read the replay: {exc})")
        say(f"  hand {row['hand_index'] + 1} vs enemy hand {eseed - 999} "
            f"run {k + 1}: {out['outcome']}"
            + (f" ({out['damage']} damage)" if out.get("damage") else ""))
        return out

    # Turn 1 first, one per (hand, enemy hand), so every run reuses it and
    # the shared-enemy-hand check runs before any game is played.
    t1_safe = guarded(t1_for)
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda j: t1_safe(*j),
                    [(dict(r), e) for r in rows for e in seeds]))
    if down:
        raise down[0]
    check_shared_enemy_hands(conn, dh, enemy_name, first)
    t1_for = t1_safe          # `one` reaches turn 1 through this name too
    jobs = [(dict(r), e, k) for k in range(runs) for r in rows for e in seeds]
    if from_end:
        jobs.reverse()
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(guarded(one), jobs))
    if down:
        raise down[0]
    if failed:
        say(f"{len(failed)} step(s) failed and were not recorded; run the "
            f"same command again to retry them")
    return summary(conn, dh, enemy_name, runs * enemy_hands, first, device)


def check_enemy_list(conn, dh, enemy_name, enemy_deck):
    """Stored games are keyed by the enemy's NAME, and `export` rewrites
    that file in place. Refuse to resume (or mix in) games played against a
    different list under the same name."""
    from . import db, trainer
    key = f"game_enemy:{dh}:{enemy_name}"
    now = trainer.deck_hash(enemy_deck)
    was = db.get_meta(conn, key)
    if was is None:     # first run (or games from before this check)
        with conn:
            db.set_meta(conn, key, now)
        return
    if was != now:
        raise RuntimeError(
            f"{enemy_name}.ydk has changed since the stored games against it "
            f"were played (list {was} -> {now}); delete them first: "
            f"DELETE FROM game_turn1/game_results/game_used/game_stops WHERE deck_hash='{dh}' AND "
            f"enemy='{enemy_name}'")


def check_shared_enemy_hands(conn, dh, enemy_name, first=None):
    """Every one of your hands must face the SAME enemy hand per seed --
    going first and second alike (`first=None` checks across both)."""
    by_seed = {}
    for r in conn.execute(
            "SELECT enemy_seed, enemy_hand FROM game_turn1 WHERE deck_hash=? "
            "AND enemy=? AND ok=1", (dh, enemy_name)):
        by_seed.setdefault(r["enemy_seed"], set()).add(r["enemy_hand"])
    bad = {s: v for s, v in by_seed.items() if len(v) > 1}
    if bad:
        raise RuntimeError(f"enemy hands differ across your hands for seed(s) "
                           f"{sorted(bad)} -- the comparison would not be fair")
    return by_seed


def summary(conn, dh, enemy_name, games_per_hand=None, first=True, device="server"):
    """Per hand: games, wins, win rate, tier (dud / near-dud / -).

    `games_per_hand` default: what the stored games imply (enemy hands
    played x runs played). A tier is only given once a hand's games are
    complete -- 0 wins out of the first 2 is not yet a dud."""
    if games_per_hand is None:
        r = conn.execute(
            "SELECT COUNT(DISTINCT enemy_seed) s, MAX(run) + 1 k FROM "
            "game_results WHERE deck_hash=? AND enemy=? AND first=? AND device=?",
            (dh, enemy_name, int(bool(first)), device)).fetchone()
        games_per_hand = (r["s"] or 0) * (r["k"] or 0)
    cells = {}
    for g in conn.execute(
            "SELECT hand_index, enemy_seed, outcome FROM game_results WHERE deck_hash=? "
            "AND enemy=? AND first=? AND device=?",
            (dh, enemy_name, int(bool(first)), device)):
        cells.setdefault(g["hand_index"], {}).setdefault(
            (g["hand_index"], g["enemy_seed"]), []).append(g["outcome"] == "win")
    out = []
    for r in conn.execute(
            "SELECT hand_index, COUNT(*) n, SUM(outcome='win') w, "
            "SUM(outcome='lost_t2') l, MAX(damage) best_dmg "
            "FROM game_results WHERE deck_hash=? AND enemy=? AND first=? AND device=? "
            "GROUP BY hand_index ORDER BY hand_index",
            (dh, enemy_name, int(bool(first)), device)):
        n, w = r["n"], r["w"] or 0
        rate = w / n if n else 0.0
        complete = n >= games_per_hand
        tier = "" if not complete else (
            "dud" if w == 0 else ("near-dud" if rate < NEAR_DUD_RATE else ""))
        out.append({"hand_index": r["hand_index"], "games": n, "wins": w,
                    "lost_t2": r["l"] or 0, "win_rate": rate,
                    # 95%, enemy hands resampled: a hand's games against one
                    # enemy hand are not independent of each other.
                    "ci": stats.crossed_bootstrap(cells.get(r["hand_index"], {}))[1:],
                    # Pairings (enemy hands) whose replays disagree: the line is
                    # there and the search only sometimes finds it -- for a real
                    # match, a coin flip on who sees it, not a known result.
                    "pairings": len(cells.get(r["hand_index"], {})),
                    "borderline": sum(1 for v in cells.get(r["hand_index"], {}).values()
                                      if 0 < sum(v) < len(v)),
                    "best_damage": r["best_dmg"], "tier": tier,
                    "complete": complete})
    out.sort(key=lambda x: -x["win_rate"])
    return out


def overall(conn, dh, enemy_name, first=True, device="server"):
    """The matchup in one line: win rate and (going first) killed-on-turn-2
    rate over every game, each with a 95% interval that resamples BOTH your
    hands and their hands (stats.crossed_bootstrap) -- the honest one; a
    Wilson interval over the pooled games is several times too narrow."""
    win, lost, no_t1 = {}, {}, 0
    for g in conn.execute(
            "SELECT hand_index, enemy_seed, outcome FROM game_results WHERE deck_hash=? "
            "AND enemy=? AND first=? AND device=?",
            (dh, enemy_name, int(bool(first)), device)):
        k = (g["hand_index"], g["enemy_seed"])
        win.setdefault(k, []).append(g["outcome"] == "win")
        lost.setdefault(k, []).append(g["outcome"] == "lost_t2")
        no_t1 += g["outcome"] == "no_t1"
    n = sum(map(len, win.values()))
    return {"games": n, "pairings": len(win),
            "borderline": sum(1 for v in win.values() if 0 < sum(v) < len(v)), "hands": len({h for h, _e in win}),
            "enemy_hands": len({e for _h, e in win}), "no_t1": no_t1,
            "win": stats.crossed_bootstrap(win), "lost_t2": stats.crossed_bootstrap(lost)}
