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
not searching is a GREEDY DEFENDER (`--opp-greedy`). At each chain window
it plays its first eligible interruption (never onto its own link), out
of the cards in `--opp-respond` -- its board's interruption pieces, the
player's own "counts as an interruption" cards and the known hand traps.
It is not a timing expert: it negates the first thing it legally can,
and the searching side's search is free to bait that. A smarter defender
(trying several windows per piece) is the known upgrade; it costs one
search per window tried.

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
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import breaking, engine, scenarios, trainer

ROOTED_BIN = (engine.VENDOR / "build" / "combo-solver-rooted" / "bin"
              / "combosolver")
RUNS = 10            # games per (hand, board, enemy hand): the solver is randomized
ENEMY_HANDS = 3      # fixed enemy hands, shared by every one of your hands
WORKERS = 4         # games at once; 2 solver threads each = 8 cores
OTK_MS = 20_000      # the enemy's turn-2 "can they just kill you" search
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


def require():
    if not ROOTED_BIN.exists():
        raise RuntimeError(
            f"the rooted solver is not built ({ROOTED_BIN}); run "
            "tools/build_rooted_solver.sh")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- running the rooted solver -----------------------------------------------

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
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env={**engine.env(), **ARENA_ENV, **(env_extra or {})},
                           timeout=_hard_limit(ms))
    except subprocess.TimeoutExpired as e:
        # Measured 2026-11-03: a 60 s turn-2 solve still running after 10.5 h,
        # ignoring SIGTERM, held one of the run's workers the whole time.
        # subprocess.run SIGKILLs it; the game is not recorded, so the next
        # run retries it -- never scored off a half-searched turn.
        raise RuntimeError(f"{label}: solver hung past {_hard_limit(ms)} s, killed") from e
    log = scratch / f"{label}.log"
    log.write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""),
                   encoding="utf-8")
    sols = sorted(out.glob("solution_*.yrp"))
    appr = sorted(out.glob("best_approach*.yrp"))
    return {"solution": sols[0] if sols else None,
            "approach": appr[0] if appr else None,
            "log": log, "rc": r.returncode}


def _respond(codes):
    codes = sorted({int(c) for c in codes if c})
    return ["--opp-respond", "|".join(map(str, codes))] if codes else []


# --- replaying a position ----------------------------------------------------

@dataclass
class Position:
    responses: list               # from the duel's start, raw bytes
    board: dict                   # targets.board_summary at the end
    turn: int
    lp: tuple
    winner: int = None            # set when the duel ended
    min_lp: list = field(default_factory=lambda: [None, None])


def replay(source, responses=None, until_turn=None):
    """Replay `responses` (default: the file's own) from `source`'s start.

    `until_turn`: then play PASSIVELY (end phase, pass, no) until that turn
    begins -- what closes a turn whose line stopped mid-way, for either
    player (ygorl's convert_line only closes turn 1). Returns a Position.
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
        while until_turn and not session.done and tracker.turn < until_turn:
            point = session.point
            if point is None:
                break
            kinds = [a.kind for a in point.actions]
            session.act(next((kinds.index(k) for k in PASSIVE_KINDS
                              if k in kinds), 0))
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
    from ygorl.solver.blocking import board_interruptions
    return [c for c, _w, _n in board_interruptions(board, cards, player).pieces]


def _hand(board, player):
    return list(board["players"][player].get("hand") or [])


# --- the three turns -----------------------------------------------------------

def turn1(our_deck, enemy_deck, hand, goal, enemy_seed, scratch, ms, seed,
          enemy_respond):
    """Your turn 1 against the real enemy deck, closed at turn 2.

    Returns {start, prefix, board, reached} or None when not even a
    partial line came back (nothing to play from).
    """
    from ygorl.engine.replay import Replay

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    tpl = breaking.template_vs(scratch / "tpl.yrpX", our_deck, enemy_deck,
                               seed=enemy_seed, shuffle=True)
    args = ["--no-ref", "--deck", Path(our_deck).resolve(),
            "--hand", "|".join(map(str, hand))]
    for t in goal:
        args += ["--target", t]
    args += ["--opp-greedy"] + _respond(enemy_respond)
    r = _solve(tpl, args, scratch, "t1", ms, seed)
    src = r["solution"] or r["approach"]
    if src is None:
        return None
    pos = replay(src, until_turn=2)
    start, prefix = breaking.split_position(Replay.from_yrp(src), pos.responses,
                                            scratch / "t1")
    return {"start": start, "prefix": prefix, "board": pos.board,
            "reached": r["solution"] is not None, "log": str(r["log"])}


def turn2(start, prefix, enemy_target, our_respond, scratch, ms, seed,
          otk_ms=OTK_MS):
    """The enemy's turn into your board. Returns {lost, start, prefix, board,
    reached}; `lost` means they found lethal. If the duel ended on their
    turn the result is only {lost, won, line}."""
    from ygorl.engine.replay import Replay

    scratch = Path(scratch)
    base = ["--start", start, "--root", prefix, "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2",
            "--opp-greedy"] + _respond(our_respond)
    if otk_ms:
        k = _solve(prefix, base + ["--goal-win", "--stop-on-win", str(STOP_ON_WIN)],
                   scratch, "t2-otk", otk_ms, seed)
        if k["solution"] and _winner(k["solution"]) == 1:
            return {"lost": True, "line": str(k["solution"])}
    r = _solve(prefix, base, scratch, "t2", ms, seed)
    # No line at all: the enemy passes its turn from the root.
    src = r["solution"] or r["approach"] or prefix
    pos = replay(src, until_turn=3)
    if pos.winner is not None:   # e.g. their closing turn ended the duel
        return {"lost": pos.winner == 1, "won": pos.winner == 0,
                "line": str(src)}
    start2, prefix2 = breaking.split_position(Replay.from_yrp(start),
                                              pos.responses, scratch / "t2")
    return {"lost": False, "start": start2, "prefix": prefix2,
            "board": pos.board, "reached": r["solution"] is not None,
            "log": str(r["log"])}


def turn3(start, prefix, our_target, enemy_respond, scratch, ms, seed,
          player=0, turn=3, extra=(), binary=None, env_extra=None):
    """Your lethal turn: turn 3 as player 0 going first, turn 2 as player 1
    going second. Returns {won, damage, line}. `extra` / `binary`: more
    solver flags / another build (A/B arms)."""
    scratch = Path(scratch)
    args = ["--start", start, "--root", prefix, "--no-ref",
            "--target", our_target, "--player", str(player), "--turns", str(turn),
            "--goal-win", "--stop-on-win", str(STOP_ON_WIN), "--opp-greedy"] \
        + _respond(enemy_respond) + list(extra)
    r = _solve(prefix, args, scratch, f"t{turn}", ms, seed, binary=binary,
               env_extra=env_extra)
    if r["solution"] and _winner(r["solution"]) == player:
        return {"won": True, "damage": None, "line": str(r["solution"])}
    damage = 0
    if r["approach"]:
        before = replay(prefix).lp[1 - player]
        pos = replay(r["approach"])
        low = (pos.min_lp[1 - player] if pos.min_lp[1 - player] is not None
               else pos.lp[1 - player])
        damage = max(0, before - low)
    return {"won": False, "damage": damage,
            "line": str(r["approach"]) if r["approach"] else None}


def enemy_hand(our_deck, enemy_deck, enemy_seed, scratch):
    """The enemy's opening hand for `enemy_seed`, sorted -- exactly the one
    it holds going first (checked against stored games 2026-11-06), so both
    sides of a matchup face the same enemy hands."""
    tpl = breaking.template_vs(Path(scratch) / "deal.yrpX", our_deck, enemy_deck,
                               seed=enemy_seed, shuffle=True)
    return sorted(_hand(replay(tpl).board, 1))


def enemy_turn1(our_deck, enemy_deck, hand, enemy_seed, enemy_target, scratch,
                ms, seed, our_respond):
    """Going second: the enemy's turn 1 into your hand, closed at turn 2.

    Returns {start, prefix, board, reached, enemy_hand}, or None when not
    even a partial line came back -- the bare template would deal them the
    bottom of their unshuffled list, not their hand, so there is no honest
    position to play from (recorded as no_t1, as going first)."""
    import random

    from ygorl.engine.replay import Replay

    from .deck import Deck

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    ehand = enemy_hand(our_deck, enemy_deck, enemy_seed, scratch)
    # Your deck, shuffled by the enemy seed, with the hand at the bottom
    # where the unshuffled side b draws it (breaking.deck_with_hand).
    d = Deck.from_ydk(Path(our_deck))
    shuffled = scratch / "ours-shuffled.ydk"
    main = list(d.main)
    random.Random(enemy_seed).shuffle(main)
    Deck(name=d.name, main=main, extra=list(d.extra), side=list(d.side)).write_ydk(shuffled)
    ours = breaking.deck_with_hand(shuffled, hand, scratch / "ours.ydk")
    tpl = breaking.template_vs(scratch / "tpl.yrpX", enemy_deck, ours,
                               seed=enemy_seed, shuffle=False)
    args = ["--no-ref", "--deck", Path(enemy_deck).resolve(),
            "--hand", "|".join(map(str, ehand)), "--target", enemy_target,
            "--opp-greedy"] + _respond(our_respond)
    r = _solve(tpl, args, scratch, "t1", ms, seed)
    src = r["solution"] or r["approach"]
    if src is None:
        return None
    dealt = replay(src, []).board
    for player, want in ((1, hand), (0, ehand)):
        if sorted(_hand(dealt, player)) != sorted(want):
            raise RuntimeError(f"player {player}'s hand was not seated: wanted "
                               f"{sorted(want)}, the duel dealt "
                               f"{sorted(_hand(dealt, player))}")
    pos = replay(src, until_turn=2)
    start, prefix = breaking.split_position(Replay.from_yrp(src), pos.responses,
                                            scratch / "t1")
    return {"start": start, "prefix": prefix, "board": pos.board,
            "reached": r["solution"] is not None, "enemy_hand": ehand,
            "log": str(r["log"])}


# --- one matchup run -----------------------------------------------------------

def enemy_respond_codes(enemy_deck):
    """The enemy's hand traps: the known list, as far as its deck runs them."""
    from .deck import Deck
    d = Deck.from_ydk(enemy_deck)
    return [c for c in scenarios.HANDTRAPS if c in set(d.main)]


def games_dir(name, enemy_name, first=True):
    """Where a matchup's game files live (going second: a `second/` beside
    the going-first hand folders)."""
    root = GAMES / name / enemy_name
    return root if first else root / "second"


def run(conn, deck_path, name, enemy_name, hands=None, enemy_hands=ENEMY_HANDS,
        runs=RUNS, solve_ms=None, workers=WORKERS, from_end=False, first=True,
        say=print):
    """Play every (hand, enemy hand, run) game, resumable, and return the
    summary. `hands` = trainer hand indices (default: every hand the trainer
    has a best goal for). `from_end` plays the list backwards: a second
    runner started beside a running one meets it in the middle instead of
    replaying its games (each game is skipped once stored). `first=False`
    plays going second (module docstring)."""
    from concurrent.futures import ThreadPoolExecutor

    from ygorl.engine.duel import default_cards

    from . import db, trainer

    require()
    ms = solve_ms or engine.SOLVE_MS
    enemy_deck = engine.ROOT / "assets" / "decks" / f"{enemy_name}.ydk"
    enemy_target = scenarios.target_for(enemy_name)
    if not enemy_target:
        raise RuntimeError(f"no payoff target for {enemy_name}: run "
                           f"scenarios.ensure_target first")
    dh = trainer.deck_hash(deck_path)
    check_enemy_list(conn, dh, enemy_name, enemy_deck)
    rows = conn.execute(
        "SELECT hand_index, hand, best_goal FROM trainer_hands "
        "WHERE deck_hash=? AND best_goal IS NOT NULL ORDER BY hand_index",
        (dh,)).fetchall()
    if hands is not None:
        rows = [r for r in rows if r["hand_index"] in set(hands)]
    if not rows:
        raise RuntimeError(f"no trained hands for {name}: train it first")
    cards = default_cards()
    ours_extra = trainer.prefs(conn, name)["interrupters"]
    theirs = enemy_respond_codes(enemy_deck)
    seeds = [1000 + k for k in range(enemy_hands)]
    first = int(bool(first))
    root = games_dir(name, enemy_name, first)
    say(f"{name} vs {enemy_name} going {'first' if first else 'second'}: "
        f"{len(rows)} hand(s) x {enemy_hands} enemy hand(s) x {runs} run(s), "
        f"{ms // 1000} s per search, target {enemy_target}")

    def t1_for(row, eseed):
        conn = db.connect()   # one per thread: sqlite3 binds a connection to it
        hit = conn.execute(
            "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND "
            "hand_index=? AND board=0 AND enemy_seed=? AND first=?",
            (dh, enemy_name, row["hand_index"], eseed, first)).fetchone()
        if hit:
            return dict(hit)
        scratch = root / f"h{row['hand_index']}" / f"e{eseed}" / "t1"
        scratch.mkdir(parents=True, exist_ok=True)
        if first:
            t1 = turn1(Path(deck_path), enemy_deck, json.loads(row["hand"]),
                       trainer.split_goal(json.loads(row["best_goal"]))[0],
                       eseed, scratch, ms, eseed,
                       theirs)
        else:
            t1 = enemy_turn1(Path(deck_path), enemy_deck, json.loads(row["hand"]),
                             eseed, enemy_target, scratch, ms, eseed,
                             set(ours_extra) | set(scenarios.HANDTRAPS))
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
                "hand_index, board, enemy_seed, first, ok, start, prefix, reached, "
                "pieces, enemy_hand, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dh, enemy_name, rec["hand_index"], 0, eseed, first, rec["ok"],
                 rec.get("start"), rec.get("prefix"), rec.get("reached"),
                 rec.get("pieces"), rec.get("enemy_hand"), _now()))
        return rec

    failed = []

    def guarded(fn):
        # One game's crash (a replay MSG_RETRY, a solver that died) must not
        # end an hours-long run. Nothing is recorded for it, so the next
        # run retries it; the count is reported at the end.
        def call(*a):
            try:
                return fn(*a)
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
            "hand_index=? AND board=0 AND enemy_seed=? AND first=? AND run=?",
            (dh, enemy_name, row["hand_index"], eseed, first, k)).fetchone()
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
            goal = trainer.split_goal(json.loads(row["best_goal"]))[0]
            # The steer for the lethal search: a BOARD piece of the hand's
            # goal. A goal that opens with hand traps kept in hand
            # ("42141493@hand") posts an empty target, and every line the
            # search reached was then rejected -- no line, no damage read.
            steer = next((t for t in goal if not t.endswith("@hand")),
                         enemy_target)
            t2 = turn3(t1["start"], t1["prefix"], steer,
                       set(theirs) | set(json.loads(t1["pieces"])),
                       scratch, ms, eseed * 100 + k, player=1, turn=2)
            out = {"outcome": "win" if t2["won"] else "no_win",
                   "damage": t2["damage"], "line": t2["line"],
                   "enemy_reached": int(t1["reached"])}
        elif t1.get("ok"):
            scratch = root / f"h{row['hand_index']}" / f"e{eseed}" / f"run{k}"
            scratch.mkdir(parents=True, exist_ok=True)
            seed = eseed * 100 + k
            ours = set(json.loads(t1["pieces"])) | set(ours_extra) \
                | set(scenarios.HANDTRAPS)
            t2 = turn2(t1["start"], t1["prefix"], enemy_target, ours,
                       scratch, ms, seed)
            if t2["lost"]:
                out = {"outcome": "lost_t2", "line": t2.get("line")}
            elif t2.get("won"):  # they lost on their own turn (costs, burn)
                out = {"outcome": "win", "damage": None, "line": t2["line"],
                       "enemy_reached": 0}
            else:
                goal = trainer.split_goal(json.loads(row["best_goal"]))[0]
                enemy_pieces = _pieces(t2["board"], 1, cards)
                t3 = turn3(t2["start"], t2["prefix"], goal[0] if goal else
                           enemy_target, set(theirs) | set(enemy_pieces),
                           scratch, ms, seed)
                out = {"outcome": "win" if t3["won"] else "no_win",
                       "damage": t3["damage"], "line": t3["line"],
                       "enemy_reached": int(t2["reached"])}
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO game_results (deck_hash, enemy, "
                "hand_index, board, enemy_seed, first, run, outcome, damage, "
                "enemy_reached, line, seconds, created) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dh, enemy_name, row["hand_index"], 0, eseed, first, k,
                 out["outcome"], out.get("damage"), out.get("enemy_reached"),
                 out.get("line"), round(time.time() - t0, 1), _now()))
        if out["outcome"] != "no_t1":
            from . import impact, stops
            g = conn.execute(
                "SELECT * FROM game_results WHERE deck_hash=? AND enemy=? AND "
                "hand_index=? AND board=0 AND enemy_seed=? AND first=? AND run=?",
                (dh, enemy_name, row["hand_index"], eseed, first, k)).fetchone()
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
    check_shared_enemy_hands(conn, dh, enemy_name, first)
    t1_for = t1_safe          # `one` reaches turn 1 through this name too
    jobs = [(dict(r), e, k) for k in range(runs) for r in rows for e in seeds]
    if from_end:
        jobs.reverse()
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(guarded(one), jobs))
    if failed:
        say(f"{len(failed)} step(s) failed and were not recorded; run the "
            f"same command again to retry them")
    return summary(conn, dh, enemy_name, runs * enemy_hands, first)


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


def summary(conn, dh, enemy_name, games_per_hand=None, first=True):
    """Per hand: games, wins, win rate, tier (dud / near-dud / -).

    `games_per_hand` default: what the stored games imply (enemy hands
    played x runs played). A tier is only given once a hand's games are
    complete -- 0 wins out of the first 2 is not yet a dud."""
    if games_per_hand is None:
        r = conn.execute(
            "SELECT COUNT(DISTINCT enemy_seed) s, MAX(run) + 1 k FROM "
            "game_results WHERE deck_hash=? AND enemy=? AND first=?",
            (dh, enemy_name, int(bool(first)))).fetchone()
        games_per_hand = (r["s"] or 0) * (r["k"] or 0)
    out = []
    for r in conn.execute(
            "SELECT hand_index, COUNT(*) n, SUM(outcome='win') w, "
            "SUM(outcome='lost_t2') l, MAX(damage) best_dmg "
            "FROM game_results WHERE deck_hash=? AND enemy=? AND first=? "
            "GROUP BY hand_index ORDER BY hand_index",
            (dh, enemy_name, int(bool(first)))):
        n, w = r["n"], r["w"] or 0
        rate = w / n if n else 0.0
        complete = n >= games_per_hand
        tier = "" if not complete else (
            "dud" if w == 0 else ("near-dud" if rate < NEAR_DUD_RATE else ""))
        out.append({"hand_index": r["hand_index"], "games": n, "wins": w,
                    "lost_t2": r["l"] or 0, "win_rate": rate,
                    "best_damage": r["best_dmg"], "tier": tier,
                    "complete": complete})
    out.sort(key=lambda x: -x["win_rate"])
    return out
