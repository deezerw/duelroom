"""Which counted interruption pieces actually fire? (a reading-gap audit)

For every stored game: the pieces `game._pieces` counts on the board at the
start of the opponent's turn 2, and whether each was activated on that turn.
A piece counted often and fired ~never is a reading gap (2026-11-20: a
materialless Lars, Mulcharmies held with a board) -- or a real condition
(Exciton Knight vs a deck that kills with fewer cards), so read before fixing.

    venv/bin/python tools/experiments/piece_audit.py [device-first] [device-second]
"""

import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from skills import db, engine, trainer, card_db, impact, game
import ygorl.engine.messages as M
from ygorl.engine.replay import Replay
from ygorl.engine.duel import default_cards
cards = default_cards(); c = db.connect(); dh = trainer.deck_hash(engine.DECKS / "imported.ydk")
nm = lambda x: (card_db.resolve(c, x) or {"name": str(x)})["name"]
DEV1 = sys.argv[1] if len(sys.argv) > 1 else "duelpc"
DEV2 = sys.argv[2] if len(sys.argv) > 2 else "server"
for first, dev, enemy, label in ((1, DEV1, "Angelechy-Meta", "OUR board on their turn 2"),
                                  (0, DEV2, "Angelechy-Meta", "ANGELECHY's board on our turn 2"),
                                  (0, DEV2, "blue_eyes_oct_2026", "BLUE-EYES's board on our turn 2")):
    owner = 0 if first else 0   # the board's owner is engine player 0 in both layouts
    their_turn = 2
    counted, fired = Counter(), Counter()
    for g in c.execute("SELECT * FROM game_results WHERE deck_hash=? AND enemy=? AND first=? AND device=? AND outcome!='no_t1'", (dh, enemy, first, dev)).fetchall():
        src = impact.source("imported", enemy, g)
        if not src: continue
        b = game.replay(src, until_turn=2).board
        pieces = set(game._pieces(b, owner, cards))
        if not pieces: continue
        rep = Replay.from_yrp(Path(src)); t = [0]; got = set()
        class O:
            def on_start(s, core): pass
            def on_buffer(s, core, buf):
                for raw in M.split_messages(buf):
                    m = M.decode_message(raw); k = type(m).__name__
                    if k == "NewTurn": t[0] += 1
                    elif k == "Chaining" and t[0] == their_turn and m.loc.controller == owner and m.code in pieces:
                        got.add(m.code)
        rep.duel().replay(rep.responses, observer=O())
        counted.update(pieces); fired.update(got)
    print(f"== {label} ({enemy}, {dev}): piece counted on N boards -> fired on the opponent's turn")
    for code, n in counted.most_common(14):
        print(f"   {nm(code)[:36]:36} {n:4} boards, fired {fired[code]:4} ({fired[code]/n:.0%})")
