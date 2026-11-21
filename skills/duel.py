"""Duel the AI (user, 2026-11-20: "build it during downtime ... it can't see my
hand in that mode. it's okay if it knows all the cards in my deck"; "it should
try to play around all the hand traps i have in my deck").

A duel is a template replay (both decks shuffled by the duel's seed) plus the
list of action indices played so far -- replayed on every request, so nothing
lives in memory and a restart loses nothing.

Who decides what:
- YOU: every one of your decisions, from the page (`user_act`).
- The AI on YOUR turn: the scripted defender, as the games use it
  (`game.respond_order`: hand traps first, board pieces held until you
  control a monster, never onto its own chain link). It reads its own cards
  and the chain, never your hand.
- The AI on ITS turn: a plan from the rooted solver (`plan`, run in its own
  process: it takes the think time), searched on a copy of the position in
  which you hold ONE OF EVERY HAND TRAP IN YOUR DECKLIST (`--opp-hand-root`, patch 0010) and
  answer with them (the smart defender) -- so it plays around all of them,
  as a player who knows your list but not your hand. Followed while the duel
  matches it; anything else (you did not fire where it assumed) -> re-plan.

Not hidden: the engine's position holds your REAL hand too, so the planning
copy has your real cards plus the added hand traps. The plan can only use
your real hand through the defender's hand traps -- so if you hold two copies
of one, it may plan against two. Effects that touch your hand at random
(discard / reveal) use the real cards; the page says so when one resolves.
"""

import json
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUELS = ROOT / "results" / "duels"
THINK_MS = 30_000            # the AI's search per plan (a lethal check, then its turn)
# Play around your decklist's hand traps: patch 0010's --opp-hand-root puts one of
# each in your hand once the moves so far have replayed (--opp-hand, which adds them
# at the DUEL'S start, made the core reject those moves: MSG_RETRY), and the smart
# defender answers with them. Off: the AI plans as if you never respond.
PLAY_AROUND = True


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def schema(conn):
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS duels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_deck TEXT NOT NULL, ai_deck TEXT NOT NULL,
            user_seat INTEGER NOT NULL,          -- engine player you sit at (0 goes first)
            seed INTEGER NOT NULL, template TEXT NOT NULL,
            actions TEXT NOT NULL DEFAULT '[]',  -- DuelSession action indices so far
            plan TEXT,                           -- the AI's planned responses (hex), see plan()
            status TEXT NOT NULL,                -- your move / thinking / over
            note TEXT, think_ms INTEGER NOT NULL DEFAULT 30000,
            created TEXT NOT NULL, updated TEXT NOT NULL)""")


# --- the duel ------------------------------------------------------------------

def create(conn, user_deck, ai_deck, user_first=True, seed=None, think_ms=THINK_MS, spawn=True):
    """A new duel: both main decks shuffled by `seed`, you seated first or second."""
    from . import breaking, engine
    from .deck import Deck

    schema(conn)
    seed = random.randrange(1, 2**31) if seed is None else int(seed)
    with conn:
        cur = conn.execute(
            "INSERT INTO duels (user_deck, ai_deck, user_seat, seed, template, status, think_ms, "
            "created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_deck, ai_deck, 0 if user_first else 1, seed, "", "your move", int(think_ms),
             _now(), _now()))
    did = cur.lastrowid
    root = DUELS / str(did)
    root.mkdir(parents=True, exist_ok=True)

    def shuffled(name, salt):
        d = Deck.from_ydk(engine.DECKS / f"{name}.ydk")
        main = list(d.main)
        random.Random(seed * 7 + salt).shuffle(main)
        out = root / f"{'you' if salt == 1 else 'ai'}.ydk"
        Deck(name=d.name, main=main, extra=list(d.extra), side=list(d.side)).write_ydk(out)
        return out
    you, ai = shuffled(user_deck, 1), shuffled(ai_deck, 2)
    a, b = (you, ai) if user_first else (ai, you)      # side a is engine player 0, who goes first
    tpl = breaking.template_vs(root / "duel.yrpX", a, b, seed=seed, shuffle=False)
    with conn:
        conn.execute("UPDATE duels SET template=? WHERE id=?", (str(tpl), did))
    advance(conn, did, spawn=spawn)
    return did


def get(conn, did):
    schema(conn)
    r = conn.execute("SELECT * FROM duels WHERE id=?", (int(did),)).fetchone()
    if r is None:
        raise KeyError(did)
    return dict(r)


class Live:
    """The duel replayed to now: `session`, `chain` [(code, controller)], and
    `responses` (raw, from the duel's start). Close it."""

    def __init__(self, row):
        from ygorl.engine.duel import DuelSession, default_cards
        from ygorl.engine.replay import Replay

        from . import engine
        import os
        os.environ.update(engine.env())
        self.row, self.cards = row, default_cards()
        self.session = DuelSession(Replay.from_yrp(Path(row["template"])).duel(None, cards=self.cards))
        self.chain = []
        self.log = []             # [(turn, player, text)], what happened, for the page
        self._seen = None
        self._track()
        for idx in json.loads(row["actions"]):
            self.act(idx)

    def _track(self):
        p = self.session.point
        if p is None or p is self._seen:
            return
        self._seen = p
        t = self.session.tracker.turn
        nm = lambda c: (self.cards.get(c).name if c and self.cards.get(c) is not None else "a card")  # noqa: E731
        for e in p.events or ():
            k = type(e).__name__
            if k == "Chaining":
                del self.chain[e.chain_count - 1:]
                self.chain.append((e.code, e.loc.controller))
                self.log.append((t, e.loc.controller, f"activates {nm(e.code)}"))
            elif k == "ChainEnd":
                self.chain.clear()
            elif k in ("Summoning", "SpSummoning", "FlipSummoning"):
                self.log.append((t, e.loc.controller, f"summons {nm(e.code)}"))
            elif k in ("ChainNegated", "ChainDisabled"):
                self.log.append((t, None, f"chain link {e.chain_count} is negated"))
            elif k == "Attack":
                self.log.append((t, None, "attacks"))
            elif k == "Damage":
                self.log.append((t, e.player, f"takes {e.amount} damage"))
            elif k == "NewTurn":
                self.log.append((t, e.player, "--- new turn ---"))

    def act(self, idx):
        self.session.act(int(idx))
        self._track()

    @property
    def point(self):
        return self.session.point

    @property
    def responses(self):
        return list(self.session.tracker.result.responses)

    def board(self):
        from ygorl.solver.targets import board_summary
        t = self.session.tracker
        return board_summary(self.session.core, t.turn, (t.lp[0], t.lp[1]))

    def close(self):
        self.session.close()


def _save(conn, did, **cols):
    cols["updated"] = _now()
    with conn:
        conn.execute(f"UPDATE duels SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                     (*cols.values(), int(did)))


# --- the AI's decisions ------------------------------------------------------------

def _monsters(board, player):
    return len(board["players"][player].get("mzone") or [])


def defend(live, ai, respond, cards, wasted=()):
    """The AI's answer on YOUR turn (the games' smart defender in Python):
    the eligible piece ranked first in `respond` (hand traps, then negates,
    then the rest), board pieces held until you control a monster, never
    onto its own chain link; else pass. Your hand is never read."""
    from . import game

    p = live.point
    kinds = [a.kind for a in p.actions]
    passing = next((i for i, a in enumerate(p.actions) if a.kind == "pass"), len(p.actions) - 1)
    if p.decision.name != "MSG_SELECT_CHAIN":
        return _follow_up(p, ai)
    if live.chain and live.chain[-1][1] == ai:
        return passing
    # your own-turn-dead card on your own turn (Mulcharmy Fuwalos on your turn
    # draws only off the AI's summons): answering it spends a hand trap on
    # nothing -- the AI Ashed one in its first test duel (2026-11-20)
    if live.chain and live.chain[-1][0] in wasted:
        return passing
    held = _monsters(live.board(), 1 - ai) < 1
    rank = {c: i for i, c in enumerate(respond)}
    best, best_r = passing, len(rank) + 1
    for i, a in enumerate(p.actions):
        if a.kind != "chain" or not a.card or a.card.code not in rank:
            continue
        if held and not game.is_hand_trap(a.card.code, cards):
            continue
        if rank[a.card.code] < best_r:
            best, best_r = i, rank[a.card.code]
    return best if "chain" in kinds else passing


def _follow_up(p, ai):
    """Targets / options of the AI's own effect: an opponent's card (highest
    ATK) where it can choose one, else the first legal choice."""
    best, best_v = 0, None
    for i, a in enumerate(p.actions):
        c = a.card
        if c is None or c.loc is None:
            continue
        theirs = c.loc.controller != ai
        v = (1 if theirs else 0, getattr(c, "attack", 0) or 0)
        if best_v is None or v > best_v:
            best, best_v = i, v
    return best


def _plan_step(live, plan):
    """The action indices that play the plan's next response here, or None."""
    from ygorl.engine.branch import actions_for_response
    from ygorl.solver.demo import canonical_response

    p = live.point
    # The plan was searched with your deck's hand traps in your hand, so it holds
    # YOUR answers at windows you only get with them -- passes, at prompts the real
    # duel never asks. A planned pass that does not fit here is stepped over.
    while plan:
        raw = bytes.fromhex(plan[0])
        try:
            steps = actions_for_response(p.state, canonical_response(p.decision, raw))
        except Exception:  # noqa: BLE001 -- a response this prompt cannot take
            steps = None
        if steps:
            return list(steps)
        if raw == _PASS:
            del plan[0]
            continue
        if p.decision.name == "MSG_SELECT_CHAIN":
            # an answer to a window of the planning copy's own: adding your assumed
            # hand traps opens AI chain options the real duel does not have (seen
            # 2026-11-20 at its first draw-phase window). One window, one answer:
            # pass here and keep the rest of the plan in step.
            del plan[0]
            return [next((i for i, a in enumerate(p.actions) if a.kind == "pass"),
                         len(p.actions) - 1)]
        return None
    return None


_PASS = b"\xff\xff\xff\xff"      # a declined chain window


def advance(conn, did, spawn=True):
    """Play every AI decision until it is your move, the AI must think, or the
    duel is over. Returns the new status."""
    from . import engine, game
    from ygorl.solver.demo import PASSIVE_KINDS

    row = get(conn, did)
    live = Live(row)
    try:
        ai = 1 - row["user_seat"]
        cards = live.cards
        respond = game.respond_order(game.respond_codes(engine.DECKS / f"{row['ai_deck']}.ydk", cards),
                                     cards)
        wasted = set(game.own_turn_dead(engine.DECKS / f"{row['user_deck']}.ydk"))
        plan = json.loads(row["plan"]) if row["plan"] else None
        acts = json.loads(row["actions"])
        for _ in range(2000):
            p = live.point
            if p is None or live.session.done:
                res = live.session.tracker.result
                _save(conn, did, actions=json.dumps(acts), status="over",
                      note=f"winner: {'you' if res.winner is not None and res.winner == row['user_seat'] else 'the AI'} ({res.reason})"
                      if res.winner is not None else f"over ({res.reason})")
                return "over"
            n_resp = len(live.responses)
            if p.player != ai:
                # your decision: a plan that expected another answer is stale
                _save(conn, did, actions=json.dumps(acts), status="your move",
                      plan=json.dumps(plan) if plan else None)
                return "your move"
            if live.session.tracker.turn_player != ai:
                idxs = [defend(live, ai, respond, cards, wasted)]
            else:
                idxs = _plan_step(live, plan)
                if idxs is None:
                    if plan is None:
                        # no plan for this turn yet: think
                        _save(conn, did, actions=json.dumps(acts), status="thinking", plan=None)
                        if spawn:
                            _spawn_plan(did)
                        return "thinking"
                    # the plan is used up or no longer fits: close the turn passively
                    # if it ran out, re-plan if the position moved off it
                    if plan == []:
                        kinds = [a.kind for a in p.actions]
                        idxs = [next((kinds.index(k) for k in PASSIVE_KINDS if k in kinds), 0)]
                    else:
                        _save(conn, did, actions=json.dumps(acts), status="thinking", plan=None)
                        if spawn:
                            _spawn_plan(did)
                        return "thinking"
            for i in idxs:
                live.act(i)
                acts.append(int(i))
            if plan and len(live.responses) > n_resp:
                plan = plan[len(live.responses) - n_resp:]
            if live.session.tracker.turn_player != ai and plan is not None:
                plan = None           # its turn ended: the next turn plans afresh
        raise RuntimeError("advance did not settle")
    finally:
        live.close()


def user_act(conn, did, idx):
    """Play your choice `idx` (an index into the current point's actions)."""
    row = get(conn, did)
    if row["status"] != "your move":
        raise ValueError(f"not your move ({row['status']})")
    live = Live(row)
    try:
        p = live.point
        if p is None or p.player != row["user_seat"]:
            raise ValueError("not your decision")
        if not 0 <= int(idx) < len(p.actions):
            raise ValueError("no such action")
        n = len(live.responses)
        live.act(int(idx))
        acts = json.loads(row["actions"]) + [int(idx)]
        plan = json.loads(row["plan"]) if row["plan"] else None
        if plan:
            # the plan assumed an answer of yours: the same one moves it on, any
            # other makes it stale (None: the AI re-plans at its next decision)
            new = [r.hex() for r in live.responses[n:]]
            plan = plan[len(new):] if new and plan[:len(new)] == new else (plan if not new else None)
        _save(conn, did, actions=json.dumps(acts), plan=json.dumps(plan) if plan is not None else None)
    finally:
        live.close()
    return advance(conn, did)


# --- the AI's turn: the solver plans it --------------------------------------------

def opp_hand(user_deck, cards):
    """What the AI assumes you hold: one copy of every hand trap in your decklist."""
    from . import engine, game
    from .deck import Deck
    main = set(Deck.from_ydk(engine.DECKS / f"{user_deck}.ydk").main)
    return sorted(c for c in main if game.is_hand_trap(c, cards))


def plan_args(row, cards, turn):
    """The solver flags for the AI's turn: its own deck's own-turn rules, you
    answering with every hand trap of your list (added to your hand), the
    smart defender. No card of your real hand is named."""
    from . import engine, game

    ai = 1 - row["user_seat"]
    args = ["--player", str(ai), "--turns", str(turn), "--no-ref"]
    if PLAY_AROUND:
        traps = opp_hand(row["user_deck"], cards)
        if traps:
            args += ["--opp-hand-root", "|".join(map(str, traps))]
            args += game.defender_args(traps, cards)
    args += game.own_turn_args(engine.DECKS / f"{row['ai_deck']}.ydk")
    return args


def deck_target(conn, deck):
    """What the AI builds toward with `deck` (a target list): its payoff target, else the board
    piece its trained hands reach most often (our own deck has no payoff target:
    the planner asked for card "1", 2026-11-20)."""
    from collections import Counter

    from . import engine, game, scenarios, trainer

    t = scenarios.target_for(deck)
    if t:
        return [t]
    path = engine.DECKS / f"{deck}.ydk"
    dh = trainer.deck_hash(path)
    seen = Counter()
    for r in conn.execute("SELECT best_goal FROM trainer_hands WHERE deck_hash=? AND best_goal "
                          "IS NOT NULL", (dh,)):
        g = game.live_goal(json.loads(r["best_goal"]), path)
        first = game.steer(g, None)
        if first:
            seen[first] += 1
    if not seen:
        raise RuntimeError(f"no target for {deck}: no payoff target and no trained hands")
    # through split_goal: a piece that needs an equip gets it (Desirae -> Sequence)
    return trainer.split_goal([seen.most_common(1)[0][0]])[0]


def _spawn_plan(did):
    subprocess.Popen([sys.executable, "-m", "skills.duel", "plan", str(did)], cwd=str(ROOT),
                     stdout=open(DUELS / str(did) / "plan.log", "a"), stderr=subprocess.STDOUT,
                     start_new_session=True)


def plan(conn, did):
    """Search the AI's turn from the current position and store the plan:
    turn 2 on, the --goal-win search (lethal, else its line that hurts you
    most); turn 1 (no attacks) or no line at all, its deck's target board.
    Then play on."""
    from ygorl.engine.replay import Replay

    from . import breaking, game

    row = get(conn, did)
    live = Live(row)
    try:
        turn = live.session.tracker.turn
        root = DUELS / str(did) / f"plan-{len(json.loads(row['actions']))}"
        start, prefix = breaking.split_position(Replay.from_yrp(Path(row["template"])),
                                                live.responses, root)
        cards = live.cards
        base = ["--start", str(start), "--root", str(prefix)] + plan_args(row, cards, turn)
        targets = [a for t in deck_target(conn, row["ai_deck"]) for a in ("--target", t)]
        t0 = time.time()
        r = None
        if turn > 1:
            r = game._solve(prefix, base + targets + ["--goal-win",
                                            "--stop-on-win", str(game.STOP_ON_WIN)],
                            root, "lethal", row["think_ms"] // 2, int(t0))
            # under --goal-win a solution IS a win the solver verified -- on its copy
            # with the assumed hand, which replaying the file here would lack. No
            # win: its best approach is the line that took you LOWEST (it builds and
            # attacks); a build line has no Battle Phase, so the AI never attacked
            # unless it had lethal (2026-11-20). Only with no approach: build.
            if not (r["solution"] or r["approach"]):
                r = None
        if r is None:
            r = game._solve(prefix, base + targets, root, "build",
                            row["think_ms"], int(t0))
        src = r["solution"] or r["approach"]
        n = len(live.responses)
        line = [x.hex() for x in Replay.from_yrp(src).responses[n:]] if src else []
    finally:
        live.close()
    _save(conn, did, plan=json.dumps(line), status="your move" if not line else "thinking",
          note=f"planned {len(line)} step(s) in {time.time() - t0:.0f} s")
    return advance(conn, did, spawn=False if not line else True)


# --- what the page shows -----------------------------------------------------------

_VERB = {"summon": "Normal Summon", "spsummon": "Special Summon", "reposition": "Change position of",
         "mset": "Set (monster)", "sset": "Set", "activate": "Activate", "chain": "Chain",
         "attack": "Attack with", "battle_phase": "Go to the Battle Phase", "main2": "Go to Main Phase 2",
         "end_phase": "End the turn", "shuffle": "Shuffle your hand", "pass": "Pass (do not respond)",
         "yes": "Yes", "no": "No", "finish": "Done", "cancel": "Cancel", "select": "Choose",
         "unselect": "Unchoose", "option": "Option", "position": "Position", "place": "Zone",
         "declare": "Declare", "number": "Number", "sort": "Order", "counter": "Counter", "rps": "Rock-paper-scissors"}
_WHERE = {0x1: "deck", 0x2: "hand", 0x4: "monster zone", 0x8: "spell/trap zone", 0x10: "GY",
          0x20: "banished", 0x40: "Extra Deck", 0x80: "material"}
_POS = {0x1: "face-up attack", 0x2: "face-down attack", 0x4: "face-up defense", 0x8: "face-down defense"}


def label(action, cards, you):
    """A readable button for one of your actions: verb, card name, where it is
    (and whose, when it is the AI's card), else what ygorl describes."""
    from ygorl.commands.branch import describe_action

    verb = _VERB.get(action.kind)
    c = action.card
    if verb is None:
        return describe_action(action, cards)
    if action.kind == "position":
        return f"{_POS.get(action.value, action.value)}"
    if action.kind == "option" and c is None:
        return f"Option {action.index + 1}"
    if c is not None and c.code:
        card = cards.get(c.code)
        nm = card.name if card is not None and card.name else str(c.code)
        where = _WHERE.get(c.loc.location if c.loc else 0, "")
        whose = "" if c.loc is None or c.loc.controller == you else "AI's "
        return f"{verb} {nm}" + (f" ({whose}{where})" if where else "")
    if action.kind in ("select", "unselect", "sort", "counter"):
        return f"{verb} #{action.index + 1}"
    return verb

def view(conn, did):
    """Your side of the table: the board with the AI's hand as a count, your
    legal actions (labels), the chain, the status."""
    row = get(conn, did)
    live = Live(row)
    try:
        b = live.board()
        you, ai = row["user_seat"], 1 - row["user_seat"]
        p = live.point
        mine = (p is not None and p.player == you and row["status"] == "your move")
        b["players"][ai]["hand"] = [None] * len(b["players"][ai].get("hand") or [])
        # its SET cards too (POS_FACEDOWN_ATTACK 0x2 / _DEFENSE 0x8): you see a back
        for z in ("mzone", "szone"):
            for m in b["players"][ai].get(z) or []:
                if m and m.get("position", 0) & 0xA:
                    m["code"] = None
                    m["overlay"] = [None] * len(m.get("overlay") or [])
        return {"id": row["id"], "status": row["status"], "note": row["note"],
                "you": you, "ai": ai, "user_deck": row["user_deck"], "ai_deck": row["ai_deck"],
                "turn": live.session.tracker.turn, "turn_player": live.session.tracker.turn_player,
                "board": b, "chain": list(live.chain),
                # what happened lately -- the AI's face-down cards stay unnamed (its
                # activations reveal them, as at a real table)
                "log": [{"turn": t, "who": "you" if pl == you else ("AI" if pl == ai else ""),
                         "text": txt} for t, pl, txt in live.log[-14:]],
                "prompt": p.decision.name if mine else None,
                "actions": [{"i": i, "label": label(a, live.cards, you)}
                            for i, a in enumerate(p.actions)] if mine else []}
    finally:
        live.close()


if __name__ == "__main__":
    from . import db
    if sys.argv[1:2] == ["plan"]:
        plan(db.connect(), int(sys.argv[2]))
