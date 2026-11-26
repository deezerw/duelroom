"""Stage 16 step 4: positions for a value net, labelled by how hard the kill was.

"Lost or not" says almost nothing on its own -- at a converged kill check
about 95% of our turn-1 boards die (STAGE15, 2026-11-19). The rich kill labels
(Stage 15: `game.solve_stats`) say HOW they die, and that ranks boards:

  hardness = 1 - first_win_s / budget_s   if the kill check killed
             (a kill found at once -> ~1, one found at the last second -> ~0)
           = -(8000 - damage) / 8000      if it did not (a board that held
             with 0 damage taken -> -1, one at 1 LP -> ~0)

i.e. from the ENEMY's side: +1 = trivially killed, -1 = untouched. Over the
defences tried, the defender keeps the best (lowest). A label is only
comparable at one kill budget: `otk_ms` is kept and the trainer stays within one.

Sources, each a position at the start of their turn 2 (start + prefix files):
  board_data  results/ml/boards/boards.jsonl with `kill` (16x re-check wins over 4x)
  games       results/ml/game_kills.jsonl + game_turn1's start/prefix

    venv/bin/python tools/ml/value_data.py      -> results/ml/value/positions.jsonl
"""

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "results" / "ml" / "value"
MAX_LOSS = 8000


def hardness(kills, budget_s):
    """The defender's best over the defences tried (lowest = hardest to kill)."""
    vals = []
    for k in kills:
        if k.get("lost"):
            t = k.get("first_win_s")
            vals.append(1.0 - min(1.0, t / budget_s) if t is not None else 0.0)
        else:
            vals.append(-(MAX_LOSS - min(MAX_LOSS, k.get("damage") or 0)) / MAX_LOSS)
    return min(vals) if vals else None


def from_boards():
    f = ROOT / "results" / "ml" / "boards" / "boards.jsonl"
    rc = ROOT / "results" / "ml" / "boards" / "rechecks.jsonl"
    recheck = ({(c["hand"], c["eseed"], c["sig"]): c for c in map(json.loads, rc.read_text().splitlines())}
               if rc.exists() else {})
    for d in map(json.loads, f.read_text().splitlines()):
        if not (d.get("ok") and not d.get("dup") and d.get("kill") and d.get("prefix")):
            continue
        c = recheck.get((d["hand"], d["eseed"], d["sig"]))
        kills, ms = (c["kill"], c["otk_ms"]) if c and c.get("kill") else (d["kill"], d["otk_ms"])
        yield {"source": "board_data", "start": d["start"], "prefix": d["prefix"],
               "group": f"imported:{d['hand']}", "eseed": d["eseed"], "otk_ms": ms,
               "lost": int(all(k["lost"] for k in kills)), "hardness": hardness(kills, ms / 1000),
               "variant": d["variant"]}


def from_games():
    f = ROOT / "results" / "ml" / "game_kills.jsonl"
    if not f.exists():
        return
    conn = sqlite3.connect(ROOT / "data" / "duelroom.db")
    t1 = {(r[0], r[1], r[2], r[3]): (r[4], r[5]) for r in conn.execute(
        "SELECT deck_hash, hand_index, enemy_seed, device, start, prefix FROM game_turn1 "
        "WHERE first=1 AND ok=1")}
    from skills import game
    for d in map(json.loads, f.read_text().splitlines()):
        pos = t1.get((d["deck_hash"], d["hand_index"], d["enemy_seed"], d["device"]))
        if not pos or not d.get("kill"):
            continue
        # the games' kill budget: 1x before OTK_X, OTK_X after (FIRST_METHOD_SINCE)
        ms = game.OTK_MS * (game.OTK_X if d.get("created", "") >= game.FIRST_METHOD_SINCE else 1)
        lost, hard = game_label(d, ms)
        yield {"source": "games", "start": pos[0], "prefix": pos[1],
               "group": f"{d['deck_hash']}:{d['hand_index']}", "eseed": d["enemy_seed"],
               "otk_ms": ms, "lost": lost, "hardness": hard, "device": d["device"]}


def game_label(d, ms):
    """(lost, hardness) of a stored game's kill check. `lost` is the GAME's verdict
    (outcome lost_t2): since 2026-11-25b a kill needs a MAJORITY of futures
    (game.majority), and the kill entries describe the real future only. Where the
    two disagree the board was killed in some futures and not others: hardness 0,
    the line between "killed" and "untouched"."""
    real = int(all(k["lost"] for k in d["kill"]))
    lost = int(d["outcome"] == "lost_t2") if d.get("outcome") else real
    return lost, (hardness(d["kill"], ms / 1000) if lost == real else 0.0)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = [r for r in (*from_boards(), *from_games()) if r["hardness"] is not None
            and Path(r["prefix"]).exists()]
    (OUT / "positions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    by = Counter((r["source"], r["otk_ms"]) for r in rows)
    print(f"{len(rows)} positions -> {OUT / 'positions.jsonl'}")
    for (src, ms), n in sorted(by.items()):
        sub = [r for r in rows if r["source"] == src and r["otk_ms"] == ms]
        hs = sorted(r["hardness"] for r in sub)
        print(f"  {src:10} kill at {ms // 1000:4} s: {n:5} positions, lost {sum(r['lost'] for r in sub) / n:.0%}, "
              f"hardness median {hs[len(hs) // 2]:+.2f} (10% {hs[len(hs) // 10]:+.2f}, 90% {hs[9 * len(hs) // 10]:+.2f})")


if __name__ == "__main__":
    main()
