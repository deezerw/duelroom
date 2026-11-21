"""Print one stored game move by move, for reading the AI's choices.

Every activation (with its chain link), summon, set, position change, card
movement between zones, negation, attack and damage, by turn and phase, named,
with whose card it is; each player's board at the end of every turn.

    venv/bin/python tools/experiments/trace_game.py <enemy> first|second <device> <hand> <enemy_seed> [run]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

LOC = {0x1: "deck", 0x2: "hand", 0x4: "monster zone", 0x8: "spell/trap zone", 0x10: "GY",
       0x20: "banished", 0x40: "Extra Deck", 0x80: "material"}
PHASE = {0x1: "Draw", 0x2: "Standby", 0x4: "Main 1", 0x8: "Battle", 0x10: "Battle",
         0x20: "Battle", 0x40: "Battle", 0x80: "Battle", 0x100: "Main 2", 0x200: "End"}


def where(loc):
    for bit, n in LOC.items():
        if loc & bit:
            return n
    return "?"


def main(enemy, side, device, hand, eseed, run=0):
    import os

    from skills import card_db, db, engine, game, impact, trainer
    os.environ.update(engine.env())
    import ygorl.engine.messages as M
    from ygorl.engine.replay import Replay

    conn = db.connect()
    nm = lambda c: (card_db.resolve(conn, c) or {"name": f"#{c}"})["name"] if c else "a face-down card"  # noqa: E731
    first = side == "first"
    dh = trainer.deck_hash(engine.DECKS / "imported.ydk")
    g = conn.execute("SELECT * FROM game_results WHERE deck_hash=? AND enemy=? AND first=? AND device=? "
                     "AND hand_index=? AND enemy_seed=? AND run=?",
                     (dh, enemy, int(first), device, int(hand), int(eseed), int(run))).fetchone()
    if g is None:
        raise SystemExit("no such game")
    me = 0 if first else 1
    who = lambda p: "US  " if p == me else "THEM"  # noqa: E731
    src = impact.source("imported", enemy, g)
    print(f"# {enemy} -- we go {side}, hand {hand}, enemy hand {eseed}, run {run}: {g['outcome']}"
          f"{' (' + str(g['damage']) + ' dmg)' if g['damage'] else ''}")
    rep = Replay.from_yrp(Path(src))
    st = {"turn": 0, "phase": None}
    out = []

    class O:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                k = type(m).__name__
                t = st["turn"]
                if k == "NewTurn":
                    st["turn"] += 1
                    out.append(f"\n=== TURN {st['turn']} ({who(m.player).strip()}) ===")
                elif k == "NewPhase":
                    ph = PHASE.get(m.phase, hex(m.phase))
                    if ph != st["phase"]:
                        st["phase"] = ph
                        out.append(f"  -- {ph} --")
                elif k == "Chaining":
                    out.append(f"  {who(m.loc.controller)} activates {nm(m.code)} "
                               f"(from {where(m.loc.location)}, chain link {m.chain_count})")
                elif k in ("ChainNegated", "ChainDisabled"):
                    out.append(f"         -> chain link {m.chain_count} "
                               f"{'negated' if k == 'ChainNegated' else 'effect negated'}")
                elif k in ("Summoning", "SpSummoning", "FlipSummoning"):
                    kind = {"Summoning": "Normal Summons", "SpSummoning": "Special Summons",
                            "FlipSummoning": "Flip Summons"}[k]
                    out.append(f"  {who(m.loc.controller)} {kind} {nm(m.code)}")
                elif k == "Set":
                    out.append(f"  {who(m.loc.controller)} sets a card ({where(m.loc.location)})")
                elif k == "Move":
                    a, b = m.previous, m.current
                    if a.location == b.location and a.controller == b.controller:
                        continue
                    if b.location & 0x2 and a.location & 0x1:
                        line = f"  {who(b.controller)} adds {nm(m.code)} from deck to hand"
                    elif a.location & 0x40 and b.location & 0x4:
                        continue            # an Extra Deck summon, printed by SpSummoning
                    else:
                        owner = who(a.controller)
                        cross = (f" -> {who(b.controller).strip()}'s {where(b.location)}"
                                 if b.controller != a.controller else f" -> {where(b.location)}")
                        line = f"  {owner} {nm(m.code)}: {where(a.location)}{cross}"
                    out.append(line)
                elif k == "Attack":
                    out.append("  attack declared")
                elif k == "Damage":
                    out.append(f"         -> {who(m.player).strip()} takes {m.amount} damage")
                elif k == "Draw":
                    pass

    rep.duel().replay(rep.responses, observer=O())
    print("\n".join(out))


if __name__ == "__main__":
    main(*sys.argv[1:])
