"""Best choices: for every card of yours that CHOOSES something when used, which
choice went with winning (user, 2026-11-21: "a ranking for cards such as super
poly with a best targets thing, like not just best card to make (garura or
muddragon usually) but also best opponents monsters to use etc, and any other
similar card").

Read once per game from its replay (like `impact` and `stops`) into
`game_choices`: each use of one of your cards and what it chose --

  made      what it Special Summoned while it resolved (Super Polymerization:
            the Fusion -- Garura, Mudragon ...)
  material  a card that went in as material while it resolved, and whose
            (Super Poly fusing THEIR Angelechy Enlisted vs one of your own)
  target    a card it targeted, and whose (S:P, Lars, Dragostapelia,
            Sulliek, Black Chaos, Kaleido-Heart ...)

Ranked per card: a choice against the OTHER choices of the same card, in the
games that used it, compared only within (your hand, their hand) and pooled --
`impact.compare`, the same Mantel-Haenszel difference with an Agresti-Caffo 95%
interval. Observational, as Card impact is: a choice that goes with winning may
only mark the games that were going well anyway; a measured "best" needs the
position re-searched with each choice (as `timing.measured` does for timing).
"""

import json
import time
from collections import defaultdict
from pathlib import Path

from . import impact

VERSION = 1
REASON_MATERIAL = 0x8
KINDS = ("made", "material", "target")


class Reader:
    """Feeds on decoded messages; `uses` = [[turn, card, kind, choice, owner]]
    for the cards of seat `me` (owner: "theirs" / "yours" -- whose card was
    chosen)."""

    def __init__(self, me):
        self.me = me
        self.turn = 0
        self.chain = []          # [(code, controller)]
        self.solving = None      # (code, controller) of the resolving link
        self.field = {}          # (controller, location, sequence) -> code
        self.uses = []

    def _own(self, link):
        return link is not None and link[1] == self.me

    def _who(self, controller):
        return "yours" if controller == self.me else "theirs"

    def feed(self, m):
        k = type(m).__name__
        if k == "NewTurn":
            self.turn += 1
        elif k == "Chaining":
            del self.chain[m.chain_count - 1:]
            self.chain.append((m.code, m.loc.controller))
        elif k == "ChainSolving":
            n = m.chain_count
            self.solving = self.chain[n - 1] if 0 < n <= len(self.chain) else None
        elif k in ("ChainSolved", "ChainEnd"):
            self.solving = None
            if k == "ChainEnd":
                self.chain.clear()
        elif k == "Move":
            a, b = m.previous, m.current
            self.field.pop((a.controller, a.location, a.sequence), None)
            if m.code:
                self.field[(b.controller, b.location, b.sequence)] = m.code
            if (self._own(self.solving) and m.reason & REASON_MATERIAL and m.code
                    and a.location & 0x0E):     # from the hand / field
                self.uses.append([self.turn, self.solving[0], "material", m.code,
                                  self._who(a.controller)])
        elif k in ("Summoning", "SpSummoning"):
            self.field[(m.loc.controller, m.loc.location, m.loc.sequence)] = m.code
            if k == "SpSummoning" and self._own(self.solving) and m.loc.controller == self.me:
                self.uses.append([self.turn, self.solving[0], "made", m.code, "yours"])
        elif k == "BecomeTarget":
            # chosen while resolving (that link), else at activation (the newest link)
            link = self.solving or (self.chain[-1] if self.chain else None)
            if not self._own(link):
                return
            for loc in m.locations:
                code = self.field.get((loc.controller, loc.location, loc.sequence))
                if code:
                    self.uses.append([self.turn, link[0], "target", code, self._who(loc.controller)])


def read(path, me=0):
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
    return {"v": VERSION, "uses": rd.uses}


def record(conn, dh, name, enemy, g):
    src = impact.source(name, enemy, g)
    got = read(src, me=0 if g["first"] else 1) if src else None
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO game_choices (deck_hash, enemy, hand_index, board, enemy_seed, "
            "first, device, run, line, choices) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (dh, enemy, g["hand_index"], g["board"], g["enemy_seed"], g["first"], g["device"],
             g["run"], g["line"] or "", None if got is None else json.dumps(got)))
    return got


def fill(conn, dh, name, enemy, budget_s=None, say=None, first=True, device="server"):
    """Read every game not read yet (or re-recorded since); with `budget_s`, stop
    after that long. Returns how many are still unread."""
    todo = conn.execute(
        "SELECT g.* FROM game_results g LEFT JOIN game_choices s USING "
        "(deck_hash, enemy, hand_index, board, enemy_seed, first, device, run) "
        "WHERE g.deck_hash=? AND g.enemy=? AND g.first=? AND g.device=? AND g.outcome != 'no_t1' "
        "AND (s.deck_hash IS NULL OR s.line != COALESCE(g.line, '') "
        "OR (s.choices IS NOT NULL AND json_extract(s.choices, '$.v') IS NOT ?))",
        (dh, enemy, int(bool(first)), device, VERSION)).fetchall()
    t0 = time.monotonic()
    for i, g in enumerate(todo):
        if budget_s is not None and time.monotonic() - t0 > budget_s:
            return len(todo) - i
        try:
            record(conn, dh, name, enemy, g)
        except Exception as exc:  # noqa: BLE001 -- one unreadable replay never hides the rest
            if say:
                say(f"  !! hand {g['hand_index'] + 1} game {g['run'] + 1}: {exc}")
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO game_choices (deck_hash, enemy, hand_index, board, "
                    "enemy_seed, first, device, run, line, choices) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (dh, enemy, g["hand_index"], g["board"], g["enemy_seed"], g["first"],
                     g["device"], g["run"], g["line"] or ""))
    return 0


def scores(conn, dh, enemy, own, min_side=impact.MIN_SIDE, first=True, device="server"):
    """[{card, uses (games), choices: [{kind, choice, owner, n, compare}]}] for
    each of your cards (`own`) that made at least two different choices, its
    choices ranked by `impact.compare` against the other choices of the same
    card, best first."""
    games = []
    for r in conn.execute(
            "SELECT g.hand_index, g.enemy_seed, g.outcome, s.choices FROM game_results g "
            "JOIN game_choices s USING (deck_hash, enemy, hand_index, board, enemy_seed, first, device, run) "
            "WHERE g.deck_hash=? AND g.enemy=? AND g.first=? AND g.device=? AND g.outcome != 'no_t1' "
            "AND s.choices IS NOT NULL AND s.line = COALESCE(g.line, '')",
            (dh, enemy, int(bool(first)), device)):
        per = defaultdict(set)
        for _t, card, kind, choice, owner in json.loads(r["choices"])["uses"]:
            if card in own and not (kind == "made" and choice == card):   # summoning itself: no choice
                per[card].add((kind, choice, owner))
        games.append(((r["hand_index"], r["enemy_seed"]), r["outcome"] == "win", per))
    out = []
    for card in sorted({c for _s, _w, per in games for c in per}):
        used = [(s, w, per[card]) for s, w, per in games if card in per]
        opts = sorted({o for _s, _w, cs in used for o in cs})
        if len(opts) < 2:
            continue
        rows = []
        for o in opts:
            strata = defaultdict(lambda: ([], []))
            for s, w, cs in used:
                strata[s][0 if o in cs else 1].append(w)
            cmp = impact.compare(list(strata.values()), min_side=min_side)
            rows.append({"kind": o[0], "choice": o[1], "owner": o[2],
                         "n": sum(o in cs for _s, _w, cs in used), "compare": cmp})
        rows.sort(key=lambda r: (r["compare"]["impact"] is None, -(r["compare"]["impact"] or 0), -r["n"]))
        out.append({"card": card, "uses": len(used), "choices": rows})
    out.sort(key=lambda c: -c["uses"])
    return out
