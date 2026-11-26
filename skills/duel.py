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

import hashlib
import json
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUELS = ROOT / "results" / "duels"
# The AI's search per plan. Was 30 s (lethal 15 s): the games' kill check converges
# at 320 s a defence, and the user "easily beat" it (2026-11-21). Its turn now runs
# its searches IN PARALLEL (plain lethal + ANSWER_PLANS answer plans), each this long.
THINK_MS = 90_000
ANSWER_PLANS = 3             # "lethal, using answer X" searches beside the plain one
# The gaming PC runs the AI's searches when it is ALREADY on and idle (user, 2026-11-25:
# "use the gaming PC when it's on"): 16 threads hold all five 2-thread searches at
# once, so a plan waits ~think_ms instead of the server's CPU-bound ~1.6x. Never woken.
HELPER = "duelpc"
_REMOVE = re.compile(r"\b(banish|destroy|negate|send|return|shuffle|take control|change control)", re.I)
_HITS_MON = re.compile(r"(monsters?|cards?) (your )?opponent controls|opponent's monster|monsters? on the field|"
                       r"cards? on the field|monster in (another|this card's|an adjacent) column|adjacent column", re.I)
_HITS_ST = re.compile(r"Spell & Trap Zone|Spell/Trap|cards? (your )?opponent controls|cards? on the field|Set card", re.I)
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
    # respond mode (user, 2026-11-21: "auto or manual respond mode"): auto = a
    # response window where pass is your only move is passed for you; manual = the
    # duel stops at every response window
    # none / select (user, 2026-11-21: "none or select cards to wait for"): never
    # stop at a response window, or stop only when one of `respond_cards` can respond
    from .db import _add_column
    _add_column(conn, "duels", "respond", "TEXT NOT NULL DEFAULT 'auto'")
    _add_column(conn, "duels", "respond_cards", "TEXT NOT NULL DEFAULT '[]'")


RESPOND_MODES = ("auto", "manual", "none", "select")


def _auto_pass(row, p):
    """The pass action's index if this response window is passed for you under
    the duel's respond mode, else None: auto -- nothing of yours can respond;
    manual -- never; none -- always; select -- none of `respond_cards` can."""
    if p.decision.name != "MSG_SELECT_CHAIN":
        return None
    passing = next((i for i, a in enumerate(p.actions) if a.kind == "pass"), None)
    if passing is None:
        return None
    mode = row.get("respond") or "auto"
    chains = [a for a in p.actions if a.kind == "chain"]
    if mode == "manual":
        return None
    if mode == "none":
        return passing
    if mode == "select":
        want = set(json.loads(row.get("respond_cards") or "[]"))
        return None if any(a.card is not None and a.card.code in want for a in chains) else passing
    return passing if not chains else None


def respond_choices(row, cards):
    """The cards of your deck that can respond at all -- the respond-mode picker:
    Traps, Quick-Play Spells, hand traps / interrupters (game.respond_codes, your
    marked ones too), and cards with a Quick Effect or a "when your opponent
    activates" clause."""
    from . import engine, game, trainer, db as _db
    from .deck import Deck
    path = engine.DECKS / f"{row['user_deck']}.ydk"
    d = Deck.from_ydk(path)
    extra = trainer.prefs(_db.connect(), row["user_deck"])["interrupters"]
    out = set(game.respond_codes(path, cards, extra=extra))
    for code in set(d.main) | set(d.extra):
        card, t = cards.get(code), game.card_text(code)
        if card is None:
            continue
        if card.type & 0x4 or (card.type & 0x2 and card.type & 0x10000) \
                or "(Quick Effect)" in t or re.search(r"When your opponent activates", t, re.I):
            out.add(code)
    return sorted(out, key=lambda c: (cards.get(c).name if cards.get(c) is not None else str(c)))


def set_respond(conn, did, mode, cards=None):
    """Switch a duel's respond mode (and, for select, the cards it waits for);
    a response window the duel is waiting at that the new mode passes is passed."""
    if mode not in RESPOND_MODES:
        raise ValueError(f"respond mode is one of {RESPOND_MODES}")
    get(conn, did)
    cols = {"respond": mode}
    if cards is not None:
        cols["respond_cards"] = json.dumps(sorted({int(c) for c in cards}))
    _save(conn, did, **cols)
    if get(conn, did)["status"] == "your move":
        advance(conn, did)


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
    # number AND seed: a database started afresh never lands in an old folder
    root = DUELS / f"{did}-{seed}"
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
        self.chain_at = []        # each chain link's location when activated (0x4: monster zone)
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
        ai = 1 - self.row["user_seat"]
        add = lambda pl, txt, code=None: self.log.append((t, pl, txt, code))  # noqa: E731
        for e in p.events or ():
            k = type(e).__name__
            if k == "Chaining":
                del self.chain[e.chain_count - 1:]
                del self.chain_at[e.chain_count - 1:]
                self.chain.append((e.code, e.loc.controller))
                self.chain_at.append(e.loc.location)
                add(e.loc.controller, f"activates {nm(e.code)}", e.code)
            elif k == "ChainEnd":
                self.chain.clear()
                self.chain_at.clear()
            elif k in ("Summoning", "SpSummoning", "FlipSummoning"):
                add(e.loc.controller, f"summons {nm(e.code)}", e.code)
            elif k in ("ChainNegated", "ChainDisabled"):
                add(None, f"chain link {e.chain_count} is negated")
            elif k == "Attack":
                add(None, "attacks")
            elif k == "Damage":
                add(e.player, f"takes {e.amount} damage")
            elif k == "NewTurn":
                add(e.player, "--- new turn ---")
            elif k == "Move":
                self._move(e, ai, nm, add)

    def _move(self, e, ai, nm, add):
        """Cards changing place, as a table sees them: what reaches the GY, the
        banished pile or a hand, and Sets. `who` is the card's controller, not
        whoever moved it, so the text is passive and starts with "~": the page
        reads it as "your X ..." / "AI's X ...". The AI's draws and face-down
        cards are never named -- the session sees everything, the page must not."""
        src, dst, who = e.previous.location, e.current.location, e.current.controller
        up = e.current.position & 0x5           # POS_FACEUP
        if dst == 0x10:                          # GY
            # from the field, a summon's material or a resolved Spell: only the notable ones
            if src in (0x2, 0x1, 0x40) or e.reason & 0x4000:
                add(who, "~" + f"{nm(e.code)} is " + ("discarded" if e.reason & 0x4000 else "sent to the GY"), e.code)
            elif src in (0x4, 0x8):              # off the field
                add(who, f"~{nm(e.code)} goes to the GY", e.code)
        elif dst == 0x20:                        # banished
            add(who, f"~{nm(e.code)} is banished" if up else "~card is banished face-down", e.code if up else None)
        elif dst == 0x2 and src != 0x2:          # to a hand
            if e.reason & 0x2000000:             # REASON_DRAW
                if who == ai:
                    add(who, "draws a card")
                else:
                    add(who, f"draws {nm(e.code)}", e.code)
            elif src in (0x1, 0x10) or (src in (0x4, 0x8, 0x20) and e.previous.position & 0x5):
                # a search is revealed at a real table; a face-up card or one from the GY is public
                add(who, f"~{nm(e.code)} is added to the hand", e.code)
            elif who == ai:
                # its face-down card bounced (a Set Trap returned): it stays hidden
                add(who, "~card is returned to the hand")
            else:
                add(who, f"~{nm(e.code)} is returned to the hand", e.code)
        elif dst == 0x8 and not up and src == 0x2:   # a Spell/Trap Set from the hand
            if who == ai:
                add(who, "sets a card")
            else:
                add(who, f"sets {nm(e.code)}", e.code)

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

    def codes(self, player, loc):
        from ygorl.solver.targets import _BOARD_FLAGS, parse_query_location
        core = self.session.core
        return [c["code"] for c in parse_query_location(core.query_location(_BOARD_FLAGS, player, loc))
                if c is not None and c.get("code")]

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


def _negate_live(board, player, kind, dead):
    """Patch 0011 in Python: does `player` control a face-up card (kind "mon":
    a face-up monster) that is not a `dead` body -- something a negate-only
    piece (game.NEGATE_ONLY) could stop?"""
    zones = ("mzone",) if kind == "mon" else ("mzone", "szone")
    return any(c.get("position", 0) & 0x5 and c["code"] not in dead
               for z in zones for c in board["players"][player].get(z) or [])


def defend(live, ai, respond, cards, wasted=(), dead=(), keys=()):
    """The AI's answer on YOUR turn (the games' smart defender in Python):
    the eligible piece ranked first in `respond` (hand traps, then negates,
    then the rest), board pieces held until you control a monster, never
    onto its own chain link; else pass. Your hand is never read."""
    from . import game
    p = live.point
    kinds = [a.kind for a in p.actions]
    passing = next((i for i, a in enumerate(p.actions) if a.kind == "pass"), len(p.actions) - 1)
    if p.decision.name != "MSG_SELECT_CHAIN":
        return _follow_up(live, ai, cards, dead, keys)
    if live.chain and live.chain[-1][1] == ai:
        return passing
    # your own-turn-dead card on your own turn (Mulcharmy Fuwalos on your turn
    # draws only off the AI's summons): answering it spends a hand trap on
    # nothing -- the AI Ashed one in its first test duel (2026-11-20)
    if live.chain and live.chain[-1][0] in wasted:
        return passing
    # board pieces wait until you control a monster; hand traps fire at once.
    # Making the hand traps wait too ("all1") was a wash over 79 positions of
    # t1_enemy_script.py (killed 82% vs 80%, +6 / -4), so the duel plays the games'
    # timing (2026-11-21).
    held = _monsters(live.board(), 1 - ai) < 1
    rank = {c: i for i, c in enumerate(respond)}
    best, best_r = passing, len(rank) + 1
    for i, a in enumerate(p.actions):
        if a.kind != "chain" or not a.card or a.card.code not in rank:
            continue
        if held and not game.is_hand_trap(a.card.code, cards):
            continue
        # a negate-only piece waits while your face-up cards are hand-trap bodies
        # (the games' Lars negated a Normal Summoned Mulcharmy Purulia, 2026-11-21)
        kind = game.NEGATE_ONLY.get(a.card.code)
        if kind and not _negate_live(live.board(), 1 - ai, kind, dead):
            continue
        # Veiler / Impermanence answer your monster's effect activated on the field
        if a.card.code in game.ON_MONSTER_ACTIVATION and not _monster_link(live, ai):
            continue
        if rank[a.card.code] < best_r:
            best, best_r = i, rank[a.card.code]
    return best if "chain" in kinds else passing


def _monster_link(live, ai):
    """The top chain link is YOUR monster's effect activated in the monster zone
    (and that monster is still there, face-up): what Veiler / Impermanence answer."""
    if not live.chain or live.chain[-1][1] == ai or live.chain_at[-1] & 0x4 == 0:
        return False
    code = live.chain[-1][0]
    return any(m["code"] == code and m.get("position", 0) & 0x5
               for m in live.board()["players"][1 - ai].get("mzone") or [])


def _follow_up(live, ai, cards, dead=(), keys=()):
    """Targets / costs of the AI's own effect. Where one of YOUR cards can be
    chosen: your most valuable -- an interruption piece first, then the
    highest ATK. Where only its own cards can: the one it loses least -- not a
    piece, lowest ATK. Else the first legal choice. (It read an `attack` the
    prompt's card reference does not carry, so it took whichever came first,
    and gave up its OWN strongest card to a cost.)"""
    from . import game

    p = live.point
    b = live.board()
    pieces = {0: set(game._pieces(b, 0, cards)), 1: set(game._pieces(b, 1, cards))}

    def worth(c):
        card = cards.get(c.code) if c.code else None
        owner = c.loc.controller
        if owner != ai and c.code in dead:
            return (-1, 0)          # your hand-trap body: the least worth taking
        if owner != ai and c.code in keys:
            # your deck's kill keys first, most important first (patch 0012 in Python)
            return (2 + len(keys) - list(keys).index(c.code), 0)
        return (1 if c.code in pieces[owner] else 0, (card.attack if card is not None else 0) or 0)

    opts = [(i, a.card) for i, a in enumerate(p.actions) if a.card is not None and a.card.loc is not None]
    theirs = [(i, c) for i, c in opts if c.loc.controller != ai]
    # its Veiler / Impermanence just chained onto your monster's effect: target THAT monster
    if (len(live.chain) >= 2 and live.chain[-1][1] == ai
            and live.chain[-1][0] in game.ON_MONSTER_ACTIVATION and live.chain[-2][1] != ai):
        hit = [(i, c) for i, c in theirs if c.code == live.chain[-2][0] and c.loc.location & 0x4]
        if hit:
            return hit[0][0]
    if theirs:
        return max(theirs, key=lambda t: worth(t[1]))[0]
    if opts:
        return min(opts, key=lambda t: worth(t[1]))[0]
    return 0


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
        dead = set(game.field_dead(engine.DECKS / f"{row['user_deck']}.ydk"))
        keys = game.kill_keys(engine.DECKS / f"{row['user_deck']}.ydk")
        plan = json.loads(row["plan"]) if row["plan"] else None
        acts = json.loads(row["actions"])
        for _ in range(2000):
            p = live.point
            if p is None or live.session.done:
                res = live.session.tracker.result
                _save(conn, did, actions=json.dumps(acts), status="over",
                      note=_winner_note(res, live.session.tracker.lp, row["user_seat"]))
                return "over"
            n_resp = len(live.responses)
            ix = _auto_pass(row, p) if p.player != ai else None
            if ix is not None:
                # a response window your respond mode passes (auto: you have nothing for
                # it, as a real client does -- user, 2026-11-21)
                live.act(ix)
                acts.append(ix)
                continue
            if p.player != ai:
                # your decision: a plan that expected another answer is stale
                _save(conn, did, actions=json.dumps(acts), status="your move",
                      plan=json.dumps(plan) if plan else None)
                return "your move"
            if live.session.tracker.turn_player != ai:
                idxs = [defend(live, ai, respond, cards, wasted, dead, keys)]
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


def undo(conn, did):
    """Take back your last move (user, 2026-11-21: "make an undo move button"):
    the duel's moves are cut to just before your last decision -- the windows
    auto mode passed for you are skipped over, so one undo is one real choice
    -- and every AI move after it goes too. The AI's plan is dropped (it re-plans
    when it must). The duel replays deterministically, so the same move gives the
    same result: undo cannot reroll luck. Not while the AI is thinking: its
    planner would write a plan for a position that is gone."""
    row = get(conn, did)
    if row["status"] == "thinking":
        raise ValueError("wait until the AI has moved")
    acts = json.loads(row["actions"])
    you = row["user_seat"]
    live = Live({**row, "actions": "[]"})
    mine = []
    try:
        for k, idx in enumerate(acts):
            p = live.point
            # windows the respond mode passed for you are not your decisions
            if p.player == you and _auto_pass(row, p) is None:
                mine.append(k)
            live.act(idx)
    finally:
        live.close()
    if not mine:
        raise ValueError("nothing to undo")
    _save(conn, did, actions=json.dumps(acts[:mine[-1]]), status="your move", plan=None,
          note="move taken back")


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


def assumed_set_card(user_deck, cards):
    """What the AI assumes each of your face-down Spell/Traps is when it plans
    (user chose, 2026-11-25, from the flaw audit: it planned as if they were
    blanks, and a set Super Polymerization wrecked it): your most dangerous card
    that can be set -- a Trap or Quick-Play Spell of your list, your marked
    interrupters first (Super Polymerization for imported), then the ones the
    scripts read as interruptions (Sulliek). It never reads what you really set."""
    from . import db as _db, engine, game, trainer
    from .deck import Deck
    path = engine.DECKS / f"{user_deck}.ydk"
    main = set(Deck.from_ydk(path).main)
    settable = lambda c: (cards.get(c) is not None and (cards.get(c).type & 0x4 or  # noqa: E731
                          (cards.get(c).type & 0x2 and cards.get(c).type & 0x10000)))
    marked = [int(c) for c in trainer.prefs(_db.connect(), user_deck)["interrupters"]]
    read = [c for c in game.respond_codes(path, cards) if c not in marked]
    return next((c for c in marked + read if c in main and settable(c)), None)


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
        # your face-down Spell/Traps are planned around as your most dangerous one
        setc = assumed_set_card(row["user_deck"], cards) if game._bin_has("--opp-set-root") else None
        if setc:
            args += ["--opp-set-root", str(setc)]
        respond = list(traps) + ([setc] if setc and setc not in traps else [])
        if respond:
            args += game.defender_args(respond, cards)
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


def hand_pieces(conn, row, live):
    """The interruption pieces the AI's current hand could reach, as targets
    (trainer.breadth_goals on its own hand; dead cards and targets already
    asked left out)."""
    from . import engine, trainer
    ai = 1 - row["user_seat"]
    hand = [c for c in live.board()["players"][ai].get("hand") or [] if c]
    try:
        _base, goals = trainer.breadth_goals(engine.DECKS / f"{row['ai_deck']}.ydk", hand,
                                             trainer.prefs(conn, row["ai_deck"]))
    except Exception:  # noqa: BLE001 -- no plan for this hand: the target alone
        return []
    out = []
    for g, label in goals:
        if label.startswith("piece:") and g and g[-1] not in out:
            out.append(g[-1])
    return out


def plan_or_pass(conn, did):
    """`plan`, but a crash never leaves the duel on "thinking" (duel 7,
    2026-11-25: a scoring replay raised and the page waited for good): the AI
    passes the rest of its turn, the note says why, the traceback goes to
    plan.log."""
    try:
        return plan(conn, did)
    except Exception:  # noqa: BLE001 -- any failure: the duel must go on
        import traceback
        traceback.print_exc()
        _save(conn, did, plan="[]", status="thinking",
              note="the AI's planner failed (see plan.log); it passes this turn")
        return advance(conn, did, spawn=False)


def revive(conn, did):
    """A planner killed outright (a reboot, out of memory) leaves the duel on
    "thinking" with nobody thinking. Past twice the solver's hard limit and a
    minute (each plan is at most two rounds of searches) it plans again; the
    conditional UPDATE lets only one of two polling requests spawn it."""
    from . import game
    row = get(conn, did)
    if row["status"] != "thinking":
        return False
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated"])).total_seconds()
    if age < 2 * game._hard_limit(row["think_ms"]) + 60:
        return False
    with conn:
        n = conn.execute("UPDATE duels SET updated=?, note=? WHERE id=? AND status='thinking' "
                         "AND updated=?", (_now(), "the AI's planner had stopped; thinking again",
                                           int(did), row["updated"])).rowcount
    if n:
        _spawn_plan(did)
    return bool(n)


def _spawn_plan(did):
    from . import db
    folder = Path(get(db.connect(), did)["template"]).parent
    subprocess.Popen([sys.executable, "-m", "skills.duel", "plan", str(did)], cwd=str(ROOT),
                     stdout=open(folder / "plan.log", "a"), stderr=subprocess.STDOUT,
                     start_new_session=True)


def answer_cards(row, live, cards, limit=ANSWER_PLANS):
    """The AI's own cards that beat what you have out (user, 2026-11-21: "the
    cards in the deck that will be the best against that card and then tries to
    find a way to get it"): from its decklist, those whose text removes (banish,
    destroy, negate, send, return, shuffle, take control) a card where yours
    are -- your field monsters, your Spell/Trap Zone (set cards included). Cards
    reaching both come first, then those reaching your monsters; at most
    `limit`. The plan then asks for a line that RESOLVES each (--resolve)."""
    from . import engine, game
    from .deck import Deck
    you = row["user_seat"]
    b = live.board()["players"][you]
    mons = bool(b.get("mzone"))
    st = bool(b.get("szone"))
    if not (mons or st):
        return []
    d = Deck.from_ydk(engine.DECKS / f"{row['ai_deck']}.ydk")
    ranked = []
    for code in sorted(set(d.main) | set(d.extra)):
        # the removal and what it reaches in ONE sentence, never your own hand
        # (Mulcharmy Fuwalos "shuffles cards from your hand ... your opponent controls +6")
        if game.is_hand_trap(code, cards):
            continue        # it only REACTS (Ghost Ogre): no plan can use it on purpose
        hm = hs = False
        # sentences end at periods only: card text puts the target BEFORE a ";" and the
        # effect after it ("target 1 monster your opponent controls; banish it")
        for sent in re.split(r"(?<=\.)\s+", game.card_text(code)):
            if not _REMOVE.search(sent) or re.search(r"from your (hand|Deck)", sent, re.I):
                continue
            if re.search(r"you own|you control", sent, re.I) and not re.search(r"opponent", sent, re.I):
                continue    # its own cards (Strategy Score returns its own Angelechy)
            hm = hm or (mons and bool(_HITS_MON.search(sent)))
            hs = hs or (st and bool(_HITS_ST.search(sent)))
        if hm or hs:
            ranked.append((-(hm + hs), -hm, code))
    return [c for *_k, c in sorted(ranked)][:limit]


def _winner_note(res, lp, you):
    """Who won. The result's winner is sometimes unset ("over (win)" told you
    nothing, 2026-11-21): then the side at 0 LP lost."""
    w = res.winner
    if w is None and min(lp) <= 0 and max(lp) > 0:
        w = 0 if lp[1] <= 0 else 1
    if w is None:
        return f"over ({res.reason})"
    return f"winner: {'you' if w == you else 'the AI'} ({res.reason})"


def helper_device(conn):
    """The HELPER device when it is already awake, runs no job of its own and has
    this solver install; else None (search here). Never wakes it: it is loud,
    and never on at night (user)."""
    from . import remote
    dev = remote.DEVICES.get(HELPER)
    if dev is None or not remote.reachable(dev, 2.0):
        return None
    try:
        busy = any(json.loads(r[0] or "{}").get("device") == HELPER
                   for r in conn.execute("SELECT params FROM jobs WHERE state = 'running'"))
    except sqlite3.Error:
        busy = False
    if busy:
        return None
    try:
        if not remote.up(dev):
            return None
        remote.sync(dev, log=lambda m: print(m, flush=True))
    except Exception as e:  # noqa: BLE001 -- a borrowed machine: any trouble, search here
        print(f"{HELPER}: not used ({e})", flush=True)
        return None
    return dev


def _stop_searches(root, dev=None):
    """Kill this plan's solvers still running (their argv names `root`/wd-...):
    here by /proc, on `dev` by pkill (the [x] keeps pkill off its own shell)."""
    import os
    import shlex
    import signal
    mark = f"{root}/wd-".encode()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if mark in cmd and b"combosolver" in cmd:
            try:
                os.kill(int(d.name), signal.SIGKILL)
            except OSError:
                pass
    if dev is not None:
        from . import remote
        pat = f"[{str(root)[0]}]{str(root)[1:]}/wd-"
        try:
            remote.ssh(dev, f"pkill -KILL -f {shlex.quote(pat)}", timeout=20)
        except Exception:  # noqa: BLE001 -- they end within their budget anyway
            pass


class _Reuse(Exception):
    """plan(): this position was planned before."""


def plan(conn, did):
    """Search the AI's turn from the current position and store the plan:
    turn 2 on, the --goal-win search (lethal, else its line that hurts you
    most); turn 1 (no attacks) or no line at all, its deck's target board.
    Then play on."""
    from ygorl.engine.replay import Replay

    from . import breaking, engine, game

    row = get(conn, did)
    live = Live(row)
    try:
        # the same position again (an undo, a move made again): the plan it got
        # before, at once -- as undo cannot reroll luck, it cannot reroll the AI
        cache = Path(row["template"]).parent / "plans.json"
        known = json.loads(cache.read_text()) if cache.exists() else {}
        key = hashlib.sha1(f"{row['think_ms']}|".encode() + b"".join(live.responses)).hexdigest()
        if key in known:
            line, t0 = known[key], time.time()
            raise _Reuse
        turn = live.session.tracker.turn
        root = Path(row["template"]).parent / f"plan-{len(json.loads(row['actions']))}"
        start, prefix = breaking.split_position(Replay.from_yrp(Path(row["template"])),
                                                live.responses, root)
        cards = live.cards
        base = ["--start", str(start), "--root", str(prefix)] + plan_args(row, cards, turn)
        dev, stopped = helper_device(conn), threading.Event()

        def solve(*a):
            if dev is not None:
                try:
                    return game._solve(*a, device=dev)
                except Exception as e:  # noqa: BLE001 -- the PC went away: search here
                    if stopped.is_set():
                        raise
                    print(f"{dev.name}: {e}; searching here instead", flush=True)
            return game._solve(*a)
        targets = [a for t in deck_target(conn, row["ai_deck"]) for a in ("--target", t)]
        t0 = time.time()
        r = building = None
        you = row["user_seat"]
        # the board: its target AND every interruption piece its hand can reach
        # (the trainer's breadth, on the AI's OWN hand -- it knows that). A lone
        # target stopped at the first line placing it: a thin board (Endgame
        # Problem alone). The conjunction is rarely all reachable; the search's
        # best approach keeps the line placing the most.
        build = targets + [a for t in hand_pieces(conn, row, live) for a in ("--target", t)]

        def build_board():
            return solve(prefix, base + build, root, "build", row["think_ms"], int(t0))
        user_deck = engine.DECKS / f"{row['user_deck']}.ydk"
        if turn > 1:
            # Its turn, searched several ways AT ONCE: plain lethal (no build target:
            # with one its best approach ranked target overlap over damage, so it
            # built and never attacked, 2026-11-21), and "lethal using answer X" for
            # the cards of its deck that beat what you have out (answer_cards).
            # Under --goal-win a solution IS a win the solver verified.
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from .deck import Deck
            # --no-ref needs a --target, and the best approach ranks target OVERLAP
            # before win progress (search.cpp). A BOARD target nothing can match -- one
            # of its Spells in the monster zone -- ties every line at 0, so the damage
            # decides, and a board target never vetoes a win (the games' kill check
            # carries one). An off-field one ("@hand") is a DEMAND: it rejected all
            # four wins found on the replay of duel 1's turn 2 (2026-11-21), as the
            # old build target vetoed every kill that did not also build its board.
            main = Deck.from_ydk(engine.DECKS / f"{row['ai_deck']}.ydk").main
            spell = next((x for x in main if cards.get(x) is not None and cards.get(x).type & 0x2), None)
            neutral = ["--target", f"{spell}@mzone"] if spell else targets
            variants = [("lethal", [])] + [(f"answer-{c}", ["--resolve", str(c), "--hint", str(c)])
                                           for c in answer_cards(row, live, cards)]
            def one(kv):
                k, (label, extra) = kv
                return k, label, solve(prefix, base + neutral + extra + ["--goal-win", "--stop-on-win",
                                                                 str(game.STOP_ON_WIN)],
                                       root, label, row["think_ms"], int(t0) + k)
            # the build search runs BESIDE the kill searches, not after them: it is
            # needed whenever no kill turns up (every plan of duel 7), and budgets are
            # CPU work (patch 0015), so running at once searches exactly as much and
            # saves a whole think_ms of waiting (~160 s -> ~100 s at 90 s)
            # the first VERIFIED kill is played at once and the other searches stopped:
            # any lethal wins the duel, so waiting for the rest changes nothing
            ex = ThreadPoolExecutor(len(variants) + 1)
            building = ex.submit(build_board)
            tried = []
            for f in as_completed([ex.submit(one, kv) for kv in enumerate(variants)]):
                tried.append(f.result())
                if tried[-1][2]["solution"]:
                    stopped.set()
                    _stop_searches(root, dev)
                    break
            ex.shutdown(wait=False, cancel_futures=True)
            tried = [(lab, x) for _k, lab, x in sorted(tried, key=lambda t: t[0])]
            won = [(lab, x) for lab, x in tried if x["solution"]]
            if won:
                r = won[0][1]
            else:
                # no win: of the lines that hurt you, the one giving you the fewest usable
                # monsters, then the most damage; none hurts at all -> build
                scored = []
                for lab, x in tried:
                    if x["approach"]:
                        dmg = game._damage(prefix, x["approach"], 1 - you, lenient=True)
                        gift = game.usable_gifts(x["approach"], you, user_deck, turn + 1,
                                                 lenient=(1 - you, len(live.responses)))
                        scored.append((gift, -dmg, lab, x))
                scored.sort(key=lambda g: g[:2])
                r = scored[0][3] if scored and scored[0][1] < 0 else None
        if r is None:
            r = building.result() if building is not None else build_board()
            # of the lines reaching its board, the one handing you the least you can use
            # (game.fewest_gifts: Opposition put Castellan, a banish, on your field)
            r = {**r, "solution": game.fewest_gifts(r.get("solutions") or [], user_deck, player=you,
                                                    until_turn=turn + 1,
                                                    lenient=(1 - you, len(live.responses))) or r["solution"]}
        src = r["solution"] or r["approach"]
        n = len(live.responses)
        line = [x.hex() for x in Replay.from_yrp(src).responses[n:]] if src else []
        cache.write_text(json.dumps({**known, key: line}))
        note = f"planned {len(line)} step(s) in {time.time() - t0:.0f} s"
    except _Reuse:
        note = f"same position as before: its plan of {len(line)} step(s) again"
    finally:
        live.close()
    _save(conn, did, plan=json.dumps(line), status="your move" if not line else "thinking", note=note)
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


def _hidden(action, you, hidden):
    """Is this action's card one you may not see: the AI's SET card (in
    `hidden`: (location bit, sequence) of its face-down field cards), its
    hand or its deck?"""
    c = action.card
    if c is None or c.loc is None or c.loc.controller == you:
        return False
    loc = c.loc.location
    if loc & 0x80:
        # an Xyz material (location MZONE|OVERLAY, position = its index under the
        # monster): materials are public, and the index is not a face-down flag
        return False
    return bool(loc & 0x3 or (loc, c.loc.sequence) in hidden or (loc & 0xC and c.loc.position & 0xA))


def label(action, cards, you, hidden=frozenset()):
    """A readable button for one of your actions: verb, card name, where it is
    (and whose, when it is the AI's card), else what ygorl describes. The AI's
    set cards, hand and deck are never named: a target prompt over its
    face-down cards named them before 2026-11-21 though the board hid them."""
    from ygorl.commands.branch import describe_action

    verb = _VERB.get(action.kind)
    c = action.card
    if verb is None:
        return describe_action(action, cards)
    if action.kind == "position":
        return f"{_POS.get(action.value, action.value)}"
    if action.kind == "option" and c is None:
        return f"Option {action.index + 1}"
    if c is not None and c.code and _hidden(action, you, hidden):
        where = _WHERE.get(c.loc.location if c.loc else 0, "")
        return f"{verb} the AI's face-down card" + (f" ({where})" if where else "")
    if c is not None and c.code:
        card = cards.get(c.code)
        nm = card.name if card is not None and card.name else str(c.code)
        where = _WHERE.get(c.loc.location if c.loc else 0, "")
        whose = "" if c.loc is None or c.loc.controller == you else "AI's "
        return f"{verb} {nm}" + (f" ({whose}{where})" if where else "")
    if action.kind in ("select", "unselect", "sort", "counter"):
        return f"{verb} #{action.index + 1}"
    return verb

def _act(i, a, cards, you, hidden):
    """One legal action for the playmat: what it is (kind, a short verb, the
    full label) and WHERE it points -- the card's (controller, location,
    sequence), a zone for a place prompt, a position -- so the page puts it on
    the card or zone you tap, as a real game does, instead of a list. A hidden
    card of the AI's keeps its place but never its code."""
    c = a.card
    hid = _hidden(a, you, hidden)
    out = {"i": i, "kind": a.kind, "label": label(a, cards, you, hidden),
           "verb": _VERB.get(a.kind) or label(a, cards, you, hidden),
           "code": c.code if c is not None and c.code and not hid else None,
           "at": ({"con": c.loc.controller, "loc": c.loc.location, "seq": c.loc.sequence}
                  if c is not None and c.loc is not None and a.kind != "position" else None)}
    if a.kind == "place":
        out["zone"] = {"con": a.value >> 16 & 0xFF, "loc": a.value >> 8 & 0xFF, "seq": a.value & 0xFF}
    if a.kind == "position":
        out["pos"] = a.value
    if a.kind in ("option", "number", "declare"):
        out["value"] = a.value
    return out


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
        hidden = set()
        for z, bit in (("mzone", 0x4), ("szone", 0x8)):
            for m in b["players"][ai].get(z) or []:
                if m and m.get("position", 0) & 0xA:
                    hidden.add((bit, m.get("sequence")))
                    m["code"] = None
                    m["overlay"] = [None] * len(m.get("overlay") or [])
        return {"id": row["id"], "status": row["status"], "note": row["note"],
                "you": you, "ai": ai, "user_deck": row["user_deck"], "ai_deck": row["ai_deck"],
                "turn": live.session.tracker.turn, "turn_player": live.session.tracker.turn_player,
                "phase": live.session.tracker.phase,
                "respond": row.get("respond") or "auto",
                "respond_cards": json.loads(row.get("respond_cards") or "[]"),
                "respond_choices": [{"code": c, "name": live.cards.get(c).name}
                                    for c in respond_choices(row, live.cards)],
                "can_undo": row["status"] != "thinking" and bool(json.loads(row["actions"] or "[]")),
                "moves": len(json.loads(row["actions"] or "[]")),
                "board": b, "chain": list(live.chain),
                # what happened lately -- the AI's face-down cards stay unnamed (its
                # activations reveal them, as at a real table)
                "log": [{"turn": t, "who": "you" if pl == you else ("AI" if pl == ai else ""),
                         "text": txt.lstrip("~"), "own": txt.startswith("~"), "code": code}
                        for t, pl, txt, code in live.log[-60:]],
                "log_total": len(live.log),        # which entries are new since the page last looked
                # your own Deck (sorted: you know its cards, never its order) and whole
                # Extra Deck; the AI's stay a count and its face-up Extra cards
                "my_deck": sorted(live.codes(you, 0x1)), "my_extra": live.codes(you, 0x40),
                "prompt": p.decision.name if mine else None,
                "actions": [_act(i, a, live.cards, you, hidden)
                            for i, a in enumerate(p.actions)] if mine else []}
    finally:
        live.close()


if __name__ == "__main__":
    from . import db
    if sys.argv[1:2] == ["plan"]:
        plan_or_pass(db.connect(), int(sys.argv[2]))
