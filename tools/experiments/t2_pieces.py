"""On their turn 2, did our turn-1 board's pieces activate, and did they stop anything?

Read from the stored going-first games (runs 0..2, server). 2026-11-18: 1.25 pieces a
board; in 44% of the games lost on turn 2 with pieces, none fired; Fiendsmith's Desirae
never fired in 103 of them -- 69 of the 101 Desirae boards have no Link monster equipped
to it (its negate count is the equipped Link Rating), so it is a dead piece there.

    venv/bin/python tools/experiments/t2_pieces.py
"""
import sys, json
from collections import Counter
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import ygorl.engine.messages as M
from ygorl.engine.replay import Replay
from skills import db, trainer, side

c = db.connect()
dh = trainer.deck_hash(side.deck_file("imported"))
name = lambda x: (c.execute("SELECT name FROM cards WHERE id=?", (x,)).fetchone() or [str(x)])[0]


def turn2_chains(path):
    rep = Replay.from_yrp(path)
    out, turn = [], [0]

    class O:
        def on_start(self, core): pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                k = type(m).__name__
                if k == "NewTurn":
                    turn[0] += 1
                elif k == "Chaining" and turn[0] == 2:
                    out.append((m.loc.controller, m.code))
    rep.duel().replay(rep.responses, observer=O())
    return out


t1 = {(r["hand_index"], r["enemy_seed"]): set(json.loads(r["pieces"] or "[]")) for r in c.execute(
    "SELECT hand_index, enemy_seed, pieces FROM game_turn1 WHERE deck_hash=? AND first=1 AND device='server' AND enemy='Angelechy-Meta'", (dh,))}
rows = c.execute("SELECT r.hand_index, r.enemy_seed, r.run, r.outcome, r.line, s.stops FROM game_results r "
                 "LEFT JOIN game_stops s USING(deck_hash, enemy, hand_index, board, enemy_seed, first, device, run) "
                 "WHERE r.deck_hash=? AND r.first=1 AND r.device='server' AND r.enemy='Angelechy-Meta' "
                 "AND r.run < 3 AND r.line IS NOT NULL", (dh,)).fetchall()
stat = {k: Counter() for k in ("lost_t2", "survived")}
unused = Counter()
for r in rows:
    grp = "lost_t2" if r["outcome"] == "lost_t2" else "survived"
    pieces = t1.get((r["hand_index"], r["enemy_seed"]), set())
    try:
        ch = turn2_chains(r["line"])
    except Exception:  # noqa: BLE001 -- counted as unreadable
        stat[grp]["unreadable"] += 1
        continue
    ours = {code for ctl, code in ch if ctl == 0}
    fired = pieces & ours
    st = json.loads(r["stops"] or "{}").get("stops", []) if r["stops"] else []
    t2stops = [s for s in st if s[0] == 2]
    S = stat[grp]
    S["games"] += 1
    S["has pieces"] += bool(pieces)
    S["pieces on board"] += len(pieces)
    S["pieces fired"] += len(fired)
    S["games: no piece fired"] += bool(pieces) and not fired
    S["games: we stopped something"] += bool(t2stops)
    S["our activations"] += len(ours)
    S["their activations"] += sum(1 for ctl, _ in ch if ctl == 1)
    if grp == "lost_t2":
        for p in pieces - ours:
            unused[name(p)] += 1
for g, S in stat.items():
    n = S["games"] or 1
    print(f"\n{g}: {S['games']} games ({S['unreadable']} unreadable)")
    print(f"  pieces on our board: {S['pieces on board'] / n:.2f} a game; fired on their turn: {S['pieces fired'] / max(1, S['pieces on board']):.0%} of them")
    print(f"  games with pieces where NONE fired: {S['games: no piece fired'] / max(1, S['has pieces']):.0%}")
    print(f"  games where we stopped anything on T2: {S['games: we stopped something'] / n:.0%}")
    print(f"  activations on their turn: theirs {S['their activations'] / n:.1f}, ours {S['our activations'] / n:.1f}")
print("\nin games we LOST on turn 2, our pieces that never activated:")
for k, v in unused.most_common(10):
    print(f"  {v:4}  {k}")
