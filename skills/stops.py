"""Which of the enemy's cards you STOPPED, with what, and which stops won games.

Read from each game's replay (the same file `impact.source` picks):

- **negated**: a chain link of theirs is negated or has its effect negated
  (MSG_CHAIN_NEGATED / MSG_CHAIN_DISABLED). The engine says so when the
  negator resolves (Ash) or only when the victim itself resolves (Evolzar
  Lars onto The Fallen & The Virtuous, traced 2026-11-05) -- both happen.
  In the first case the resolving link IS the stopper; in the second it is
  your nearest link ABOVE the victim in the same chain. Links above it
  that are all theirs = they negated their own card (the turn-2 search
  does play Dominus Impulse onto its own Dominus Spark): not a stop. No
  link above it at all = a continuous effect on the field (Dragostapelia's
  counters, an earlier Imperm), recorded as stopper 0 = "field".
- **removed**: a card of theirs leaves the field (or their hand) to the GY,
  banishment, hand, deck or Extra Deck BY EFFECT -- not as a cost, as
  material, or by release -- while a link of YOURS is resolving. A card
  that moves itself (S:P Little Knight banishing itself to dodge) is theirs
  resolving, so it is never counted.

Not counted: battle destruction, and things that stop a card without a
message naming it (a floodgate that makes an activation illegal never lets
the card be activated, so the enemy simply plays something else).

Scores reuse `impact.compare` -- the same within-(your hand, their hand)
comparison the card impact score uses:

- per ENEMY card: among the games where they played it (activated or
  summoned it), how much more often you won when it was stopped than when
  it went through. "Stopping Angelechy Problem is worth +40 pts."
- per YOUR card: the games where it stopped something against the games
  where it did not -- and what it stopped.
"""

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import impact

FIELD = 0               # the stopper of a negation no link of yours explains
_OFF_FIELD_TO = 0x1 | 0x2 | 0x10 | 0x20 | 0x40     # deck, hand, GY, banished, extra


class Reader:
    """Feed it a game's decoded messages in order (`feed`); `played` and
    `stops` are what `read` returns. Split from the replay so the
    attribution rules can be tested on hand-made chains."""

    def __init__(self, me=0):
        import ygorl.engine.constants as C

        self.C, self.me, self.them = C, me, 1 - me
        self.played, self.stops = {}, []
        self.chain, self.solving, self.turn = [], None, 0
        self.not_a_stop = (C.REASON_COST | C.REASON_MATERIAL | C.REASON_RELEASE
                           | C.REASON_SUMMON)

    def feed(self, m):
        C, me, them, chain = self.C, self.me, self.them, self.chain
        k = type(m).__name__
        if k == "NewTurn":
            self.turn += 1
        elif k == "Chaining":
            del chain[m.chain_count - 1:]
            chain.append((m.code, m.loc.controller))
            if m.loc.controller == them and m.code:
                self.played.setdefault(m.code, self.turn)
        elif k in ("Summoning", "SpSummoning", "FlipSummoning"):
            if m.loc.controller == them and m.code:
                self.played.setdefault(m.code, self.turn)
        elif k == "ChainSolving":
            self.solving = m.chain_count
        elif k == "ChainSolved":
            self.solving = None
        elif k == "ChainEnd":
            chain.clear()
            self.solving = None
        elif k in ("ChainNegated", "ChainDisabled"):
            n, s = m.chain_count, self.solving
            if n > len(chain) or chain[n - 1][1] != them:
                return
            if s and n < s <= len(chain):
                neg = [chain[s - 1]]      # said while the negator resolves
            else:
                neg = chain[n:]           # said when the victim resolves
            by = next((code for code, ctl in neg if ctl == me),
                      None if neg else FIELD)
            if by is not None:            # None: they negated their own
                self.stops.append([self.turn, "negated", by, chain[n - 1][0]])
        elif k == "Move":
            s = self.solving
            if (not s or s > len(chain) or chain[s - 1][1] != me
                    or m.previous.controller != them
                    or not m.previous.location & (C.LOCATION_ONFIELD | C.LOCATION_HAND)
                    or m.previous.location & C.LOCATION_OVERLAY
                    or not m.reason & C.REASON_EFFECT or m.reason & self.not_a_stop):
                return
            if (m.current.location & _OFF_FIELD_TO
                    or m.current.controller == me):    # taken over
                self.stops.append([self.turn, "removed", chain[s - 1][0], m.code])


def read(path, me=0):
    """{played: {code: first turn}, stops: [[turn, kind, by, code], ...]}
    for the game replay at `path`, `me` being your seat (module docstring)."""
    import ygorl.engine.messages as M
    from ygorl.engine.replay import Replay

    rep, rd = Replay.from_yrp(Path(path)), Reader(me)

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                rd.feed(M.decode_message(raw))

    rep.duel().replay(rep.responses, observer=_Obs())
    return {"played": rd.played, "stops": rd.stops}


def record(conn, dh, name, enemy, g):
    """Read one game and cache what was stopped. Returns the dict or None."""
    src = impact.source(name, enemy, g)
    got = read(src) if src else None
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO game_stops (deck_hash, enemy, hand_index, "
            "board, enemy_seed, run, line, stops) VALUES (?,?,?,?,?,?,?,?)",
            (dh, enemy, g["hand_index"], g["board"], g["enemy_seed"], g["run"],
             g["line"] or "", None if got is None else json.dumps(got)))
    return got


def fill(conn, dh, name, enemy, budget_s=None, say=None):
    """Cache every game not read yet (or re-recorded since). With
    `budget_s`, stop after that long. Returns how many are still unread."""
    todo = conn.execute(
        "SELECT g.* FROM game_results g LEFT JOIN game_stops s USING "
        "(deck_hash, enemy, hand_index, board, enemy_seed, run) "
        "WHERE g.deck_hash=? AND g.enemy=? AND g.outcome != 'no_t1' AND "
        "(s.deck_hash IS NULL OR s.line != COALESCE(g.line, ''))",
        (dh, enemy)).fetchall()
    t0 = time.monotonic()
    for i, g in enumerate(todo):
        if budget_s is not None and time.monotonic() - t0 > budget_s:
            return len(todo) - i
        try:
            record(conn, dh, name, enemy, g)
        except Exception as exc:    # one unreadable replay never hides the rest
            if say:
                say(f"  !! hand {g['hand_index'] + 1} game {g['run'] + 1}: {exc}")
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO game_stops (deck_hash, enemy, hand_index, "
                    "board, enemy_seed, run, line, stops) VALUES (?,?,?,?,?,?,?,NULL)",
                    (dh, enemy, g["hand_index"], g["board"], g["enemy_seed"],
                     g["run"], g["line"] or ""))
    return 0


def _games(conn, dh, enemy, own):
    """[(stratum, won, played, stops)] for every read game. Stops BY a card
    not in your list (theirs, while you controlled it) are dropped; stops OF
    one of your own cards they had taken are dropped too."""
    out = []
    for r in conn.execute(
            "SELECT g.hand_index, g.enemy_seed, g.outcome, s.stops FROM game_results g "
            "JOIN game_stops s USING (deck_hash, enemy, hand_index, board, enemy_seed, run) "
            "WHERE g.deck_hash=? AND g.enemy=? AND g.outcome != 'no_t1' AND "
            "s.stops IS NOT NULL AND s.line = COALESCE(g.line, '')", (dh, enemy)):
        d = json.loads(r["stops"])
        played = {int(k): v for k, v in d["played"].items()}
        stops = [(t, kind, by, code) for t, kind, by, code in d["stops"]
                 if (own is None or by == FIELD or by in own)
                 and (own is None or code not in own or code in played)]
        for t, _kind, _by, code in stops:
            played.setdefault(code, t)   # a set card removed before it was ever used
        out.append(((r["hand_index"], r["enemy_seed"]), r["outcome"] == "win",
                    played, stops))
    return out


def scores(conn, dh, enemy, min_side=impact.MIN_SIDE, own=None):
    """{"games": n, "theirs": [...], "yours": [...]}.

    theirs -- one row per enemy card you stopped at least once:
      {code, played, stopped, impact, ci, compared, stopped_win, through_win,
       turn, by: [(your code, times), ...], status}
      `impact` = win rate when it was stopped minus when it went through,
      among the games they played it, inside each (your hand, their hand).
    yours -- one row per card of yours (or FIELD) that stopped something:
      {code, stops, games_with, impact, ci, compared, with_win, without_win,
       turn, hit: [(their code, times), ...], status}
      `impact` = games where it stopped something vs games where it did not.

    `own`: your deck's card codes (`impact.deck_codes`)."""
    games = _games(conn, dh, enemy, own)
    strata = defaultdict(list)
    for s, won, played, stops in games:
        strata[s].append((won, played, stops))

    theirs = []
    for c in {code for _s, _w, _p, st in games for _t, _k, _b, code in st}:
        by, turns = Counter(), Counter()
        for _s, _w, _p, st in games:
            for t, _k, b, code in st:
                if code == c:
                    by[b] += 1
                    turns[t] += 1
        rows = [[(won, any(code == c for *_x, code in st))
                 for won, played, st in g if c in played] for g in strata.values()]
        r = impact.compare([([w for w, hit in g if hit], [w for w, hit in g if not hit])
                            for g in rows], min_side)
        theirs.append({"code": c, "played": sum(len(g) for g in rows),
                       "stopped": r.pop("n_yes"), "stopped_win": r.pop("yes_win"),
                       "through_win": r.pop("no_win"), **r,
                       "turn": turns.most_common(1)[0][0],
                       "by": by.most_common()})

    yours = []
    for c in {b for _s, _w, _p, st in games for _t, _k, b, _c in st}:
        hit, turns, n = Counter(), Counter(), 0
        for _s, _w, _p, st in games:
            for t, _k, b, code in st:
                if b == c:
                    hit[code] += 1
                    turns[t] += 1
                    n += 1
        r = impact.compare([([w for w, _p, st in g if any(b == c for _t, _k, b, _c in st)],
                             [w for w, _p, st in g if not any(b == c for _t, _k, b, _c in st)])
                            for g in strata.values()], min_side)
        yours.append({"code": c, "stops": n, "games_with": r.pop("n_yes"),
                      "with_win": r.pop("yes_win"), "without_win": r.pop("no_win"),
                      **r, "turn": turns.most_common(1)[0][0],
                      "hit": hit.most_common()})

    order = {"scored": 0, "few games": 1, "every game": 2}
    for rows, n in ((theirs, "stopped"), (yours, "stops")):
        rows.sort(key=lambda r: (order[r["status"]], -(r["impact"] or 0), -r[n]))
    return {"games": len(games), "theirs": theirs, "yours": yours}
