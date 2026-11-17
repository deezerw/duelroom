"""Server vs duelpc on the SAME turn-1 positions (STAGE10 step 6).

The games' turn 1 is one search per (hand, enemy hand) with a fixed seed,
so a matchup played on both devices gives paired solves for free:
    results/games/<deck>/<enemy>/h*/e*/t1/t1.log           (server)
    results/games/<deck>/<enemy>@<device>/h*/e*/t1/t1.log  (other)
Compares rollout-phase states per second (greedy+novelty + NRPA states over
the rollout phase's own seconds, from the log) and whether turn 1 reached
its goal (game_turn1.reached), each device at its normal concurrency.

    venv/bin/python tools/experiments/device_bench.py imported Angelechy-Meta duelpc
"""

import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skills import db, game, trainer  # noqa: E402

STATES = re.compile(r"^\s*(?:greedy\+novelty|NRPA)\s*:\s*\d+ rollouts\s+(\d+) states", re.M)
TOTAL = re.compile(r"^\s*total: ([\d.]+) s", re.M)


def rate(log):
    text = log.read_text(errors="replace")
    states, total = STATES.findall(text), TOTAL.search(text)
    if len(states) != 2 or not total or float(total.group(1)) <= 0:
        return None
    return sum(map(int, states)) / float(total.group(1))


def main(name, enemy, device):
    conn = db.connect()
    dh = trainer.deck_hash(Path("assets/decks") / f"{name}.ydk")
    reached = {(r["device"], r["hand_index"], r["enemy_seed"]): r["reached"]
               for r in conn.execute(
                   "SELECT device, hand_index, enemy_seed, reached FROM game_turn1 "
                   "WHERE deck_hash=? AND enemy=? AND first=1 AND ok=1", (dh, enemy))}
    a_root, b_root = game.games_dir(name, enemy), game.games_dir(name, enemy, True, device)
    pairs = []
    for log_b in sorted(b_root.glob("h*/e*/t1/t1.log")):
        rel = log_b.relative_to(b_root)
        log_a = a_root / rel
        h, e = int(rel.parts[0][1:]), int(rel.parts[1][1:])
        ra, rb = (rate(log_a), rate(log_b)) if log_a.exists() else (None, None)
        if ra and rb:
            pairs.append((h, e, ra, rb, reached.get(("server", h, e)), reached.get((device, h, e))))
    if not pairs:
        print("no paired turn-1 logs yet")
        return
    ratios = [rb / ra for _h, _e, ra, rb, _x, _y in pairs]
    print(f"{len(pairs)} paired turn-1 solves, {name} vs {enemy}")
    print(f"  states/s  server median {statistics.median(p[2] for p in pairs):,.0f}"
          f"   {device} median {statistics.median(p[3] for p in pairs):,.0f}")
    print(f"  {device}/server per position: median {statistics.median(ratios):.2f}x"
          f"  (min {min(ratios):.2f}, max {max(ratios):.2f})")
    ra = sum(1 for p in pairs if p[4]), sum(1 for p in pairs if p[5])
    both = sum(1 for p in pairs if p[4] and p[5])
    print(f"  turn 1 reached its goal: server {ra[0]}/{len(pairs)}, {device} {ra[1]}/{len(pairs)}"
          f"  ({device} only {ra[1] - both}, server only {ra[0] - both})")


if __name__ == "__main__":
    main(*sys.argv[1:4])
