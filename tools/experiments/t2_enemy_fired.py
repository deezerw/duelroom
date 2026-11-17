"""Which ENEMY cards fired during your lethal turn, split by win / loss
(going second: their cards activated on turn 2, read from each game's line).

Measured 2026-11-17 on imported vs Angelechy-Meta going second (2390 games,
server): the drop from the 2026-11-09 run's 63% to 45% is the enemy now
answering with its whole deck (8463177: script-read interruptions, not only
the HANDTRAPS list) -- 9 Dominus traps activated FROM THE HAND that the old
model never fired. Dominus Impulse in 41% of losses vs 20% of wins, Purge 24%
vs 11%, Spark 24% vs 15%; the set Angelechy traps fire about evenly. Same
enemy opening hands as the old run; their boards hold no more pieces. The
old 63% was inflated (and excluded 19% no_t1 games), not a regression.

    venv/bin/python tools/experiments/t2_enemy_fired.py [enemy] [device]
"""
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from skills import db, impact, card_db
c = db.connect()
ENEMY = sys.argv[1] if len(sys.argv) > 1 else "Angelechy-Meta"
DEVICE = sys.argv[2] if len(sys.argv) > 2 else "server"
rows = c.execute("SELECT hand_index, enemy_seed, run, outcome, line FROM game_results "
                 "WHERE enemy=? AND first=0 AND device=?", (ENEMY, DEVICE)).fetchall()
fired = {"win": Counter(), "no_win": Counter()}
n = Counter(); nolines = Counter(); any_fired = Counter()
for r in rows:
    if not r["line"] or not Path(r["line"]).exists():
        nolines[r["outcome"]] += 1
        continue
    n[r["outcome"]] += 1
    used = impact.used_cards(r["line"], player=0)
    t2 = {k for k, t in used.items() if t == 2}
    fired[r["outcome"]].update(t2)
    any_fired[(r["outcome"], bool(t2))] += 1
nm = lambda x: (card_db.resolve(c, x) or {"name": str(x)})["name"]
print("games read", dict(n), "no line", dict(nolines))
print("enemy fired anything on our turn:", dict(any_fired))
allc = fired["win"] + fired["no_win"]
for code, k in allc.most_common(20):
    print(f"  {nm(code):40} in {fired['no_win'][code]:4} of {n['no_win']} losses, {fired['win'][code]:4} of {n['win']} wins")
