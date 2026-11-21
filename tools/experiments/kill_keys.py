"""Which of the enemy's cards does its killing turn hinge on? (the defender's targets)

Read from the stored going-first games it killed on turn 2: for each kill, the
cards sent from its field as material just before each of its Special Summons
of that turn, kept for the LAST Synchro / Xyz / Link Summon before the kill.
A card that is material in at least KEY_SHARE of the kills is a kill key:
`game.kill_keys` hands them to the defender (--opp-key, patch 0012), which
then takes a key over any other target.

First read, 2026-11-21 (399 Angelechy-Meta kills): Assault Blackwing - Kusanagi
in 340, its Tuner Angelechy Enlisted in 332 -- while the defender took the
highest-ATK target and S:P banished Enlisted in 48 of its 125 uses.

    venv/bin/python tools/experiments/kill_keys.py [enemy] [device]   -> writes data/kill_keys.json
    venv/bin/python tools/experiments/kill_keys.py --ours [enemy] [device]
        OUR deck's keys, from its going-second wins vs that enemy (our turn 2 is
        engine player 1's turn 2 there too): the enemy's defender takes them first
        on our break -- the strong enemy the user asked for.
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import card_db, db, engine, game, trainer  # noqa: E402

KEY_SHARE = 0.5
OUT = ROOT / "data" / "kill_keys.json"
EXTRA_SUMMON = 0x2000 | 0x800000 | 0x4000000      # TYPE_SYNCHRO | TYPE_XYZ | TYPE_LINK


def main(enemy="Angelechy-Meta", device=None, ours=False):
    os.environ.update(engine.env())
    import ygorl.engine.messages as M
    from ygorl.engine.duel import default_cards
    from ygorl.engine.replay import Replay

    cards = default_cards()
    c = db.connect()
    nm = lambda x: (card_db.resolve(c, x) or {"name": str(x)})["name"]  # noqa: E731
    dh = trainer.deck_hash(engine.DECKS / "imported.ydk")
    # the killing turn is engine player 1's turn 2 both ways: going first the
    # enemy's (it killed us: lost_t2), going second ours (we won on turn 2)
    first, outcome, who = (0, "win", "imported") if ours else (1, "lost_t2", enemy)
    device = device or ("server" if ours else "duelpc")
    games = c.execute("SELECT line FROM game_results WHERE deck_hash=? AND enemy=? AND first=? "
                      "AND device=? AND outcome=? AND line IS NOT NULL",
                      (dh, enemy, first, device, outcome)).fetchall()
    mats, n = Counter(), 0
    for g in games:
        if not Path(g["line"]).exists():
            continue
        st = {"t": 0, "sent": [], "last": None}

        class O:
            def on_start(self, core):
                pass

            def on_buffer(self, core, buf):
                for raw in M.split_messages(buf):
                    m = M.decode_message(raw)
                    k = type(m).__name__
                    if k == "NewTurn":
                        st["t"] += 1
                    elif st["t"] != 2:
                        continue
                    elif k == "Move" and m.previous.controller == 1 and m.previous.location & 0x4:
                        st["sent"].append(m.code)
                    elif k == "SpSummoning" and m.loc.controller == 1:
                        card = cards.get(m.code)
                        if card is not None and card.type & EXTRA_SUMMON and st["sent"]:
                            st["last"] = set(st["sent"])
                        st["sent"] = []

        rep = Replay.from_yrp(Path(g["line"]))
        try:
            rep.duel().replay(rep.responses, observer=O())
        except Exception:  # noqa: BLE001 -- an unreadable line is skipped
            continue
        if st["last"]:
            n += 1
            mats.update(st["last"])
    # most important first: --opp-key's order is the defender's priority
    keys = [code for code, k in mats.most_common() if n and k / n >= KEY_SHARE]
    print(f"{who} (vs {enemy}) [{device}]: {n} kills read; material of the last big summon:")
    for code, k in mats.most_common(8):
        print(f"   {nm(code)[:40]:40} {k:4} ({k / max(1, n):.0%}){'   <-- KEY' if code in keys else ''}")
    data = json.loads(OUT.read_text()) if OUT.exists() else {}
    data[who] = {"keys": keys, "kills": n, "device": device, "share": KEY_SHARE,
                 **({"vs": enemy} if ours else {})}
    OUT.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {OUT}: {who} keys {[nm(x) for x in keys]}")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if x != "--ours"]
    main(*a, ours="--ours" in sys.argv)
