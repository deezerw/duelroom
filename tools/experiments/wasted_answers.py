"""Which answers get thrown away, by answer and what it was thrown at (user, 2026-11-26: "does
the bot keep track of shitty moves? ... dont use imperm on requiem unless i cant use its
ability to dodge it"). Read-only: it changes nothing the AI does.

Every stored game's replay is read twice (each side as the answering one) with stops.Reader:
each time that side activates one of its hand traps / negates on the OTHER side's turn, the
target is the opponent's link it was chained onto (or "open window" with nothing to chain
onto -- the old defender's habit, fixed for Veiler / Imperm by patch 0017), and it counts as
STOPPED if stops.Reader credits it with a stop before that chain ends (a link negated, a card
removed). Caveat: an Imperm left on a face-up monster that stops a LATER activation is not a
stop here, so "no stop seen" is close to "wasted", not the same.

    venv/bin/python tools/experiments/wasted_answers.py [devices, default m21b,m21c,m25c] [min uses]
"""
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, impact, stops  # noqa: E402


class Answers(stops.Reader):
    """stops.Reader + each answer of `me`'s on the opponent's turn: [turn, code, target, stopped]."""

    def __init__(self, me, answers):
        super().__init__(me)
        self.answers_set, self.uses, self.turn_player = set(answers), [], None
        self.open = []                       # this chain's answers of ours not yet settled

    def feed(self, m):
        k = type(m).__name__
        if k == "NewTurn":
            self.turn_player = m.player
        before = len(self.stops)
        if (k == "Chaining" and m.loc.controller == self.me and m.code in self.answers_set
                and self.turn_player == self.them):
            below = self.chain[m.chain_count - 2] if m.chain_count >= 2 else None
            target = below[0] if below and below[1] == self.them else "open window"
            self.open.append([self.turn, m.code, target, False])
        super().feed(m)
        for st in self.stops[before:]:      # a stop credited to an open answer settles it
            for u in self.open:
                if st[2] == u[1]:
                    u[3] = True
        if k == "ChainEnd":
            self.uses += self.open
            self.open = []


def read(path, me, answers):
    import ygorl.engine.messages as M
    from ygorl.engine.replay import Replay
    rep, rd = Replay.from_yrp(Path(path)), Answers(me, answers)

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                rd.feed(M.decode_message(raw))

    rep.duel().replay(rep.responses, observer=_Obs())
    return rd.uses + rd.open


def main(devices="m21b,m21c,m25c", min_uses=5):
    import os
    os.environ.update(engine.env())
    from ygorl.engine.duel import default_cards
    cards = default_cards()
    conn = db.connect()
    devs = devices.split(",")
    nm = lambda c: c if isinstance(c, str) else (cards.get(c).name if cards.get(c) else str(c))  # noqa: E731
    tally = defaultdict(Counter)            # (answer, target) -> {uses, stopped}
    games = 0
    q = (f"SELECT * FROM game_results WHERE device IN ({','.join('?' * len(devs))}) "
         "AND line IS NOT NULL")
    for g in conn.execute(q, devs):
        g = dict(g)
        src = impact.source("imported", g["enemy"], g)
        if not src:
            continue
        ours = engine.DECKS / "imported.ydk"
        theirs = engine.DECKS / f"{g['enemy']}.ydk"
        # going first we are engine player 0; going second player 1
        us = 0 if g["first"] else 1
        try:
            for me, deck in ((us, ours), (1 - us, theirs)):
                ans = [c for c in game.respond_codes(deck, cards)
                       if game.is_hand_trap(c, cards) or game._is_negate(c, cards)]
                for _t, code, target, stopped in read(src, me, ans):
                    side = "you" if me == us else "enemy"
                    tally[(side, code, target)]["uses"] += 1
                    tally[(side, code, target)]["stopped"] += stopped
        except Exception as exc:  # noqa: BLE001 -- an unreadable replay is skipped
            print(f"skip {src}: {type(exc).__name__}: {exc}", flush=True)
            continue
        games += 1
    print(f"{games} games ({devices}); answers with >= {min_uses} uses, most wasted first:")
    rows = [(k, v) for k, v in tally.items() if v["uses"] >= int(min_uses)]
    rows.sort(key=lambda kv: (kv[1]["stopped"] / kv[1]["uses"], -kv[1]["uses"]))
    for (side, code, target), v in rows:
        print(f"  {side:5} {nm(code):34} -> {nm(target):34} used {v['uses']:4}, "
              f"no stop seen {v['uses'] - v['stopped']:4} ({1 - v['stopped'] / v['uses']:.0%})")


if __name__ == "__main__":
    main(*sys.argv[1:3])
