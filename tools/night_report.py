"""What happened overnight, in one read (duelpc's day run + the server's jobs).

    venv/bin/python tools/night_report.py
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tail_after(path, marker, n=40):
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()
    idx = max((i for i, l in enumerate(lines) if marker in l), default=0)
    return lines[idx:][-n:]


def main():
    print("== duelpc day log (latest run) ==")
    for l in tail_after(ROOT / "results/ml/duelpc_day.log", "day starts"):
        if l.startswith("==="):
            print(" ", l.strip("= "))

    rep = ROOT / "results/ml/bc/report.json"
    print("\n== imitation training (Stage 16 step 2) ==")
    if rep.exists():
        r = json.loads(rep.read_text())
        print(f"  {r['train_samples']} training / {r['heldout_samples']} held-out decisions, "
              f"{r['train_seconds']} s on the GPU")
        for k, v in r.get("heldout_by_kind", {}).items():
            print(f"  held-out {k:11} accuracy {v['accuracy']:.0%} (uniform {v['uniform_accuracy']:.0%}, "
                  f"{v['samples']} decisions)")
    else:
        print("  not run yet (see results/ml/bc_train.log)")

    for title, cmd in (("turn-1 budget vs the converged kill check",
                        [sys.executable, "tools/experiments/t1_budget.py", "summary"]),):
        print(f"\n== {title} ==")
        try:
            out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300).stdout
            print("  " + "\n  ".join(out.strip().splitlines() or ["no results yet"]))
        except Exception as exc:  # noqa: BLE001
            print(f"  not readable: {exc}")

    conn = sqlite3.connect(ROOT / "data/duelroom.db")
    print("\n== games (main deck), by side, opponent and settings ==")
    for enemy, first, dev, n, w, l2 in conn.execute(
            "SELECT r.enemy, r.first, r.device, COUNT(*), SUM(outcome='win'), SUM(outcome='lost_t2') "
            "FROM game_results r JOIN (SELECT DISTINCT deck_hash, deck FROM trainer_hands) t "
            "USING (deck_hash) WHERE t.deck='imported' AND outcome != 'no_t1' "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2 DESC, 3"):
        side = "first" if first else "second"
        extra = f", killed on turn 2 {l2 / n:.0%}" if first else ""
        print(f"  vs {enemy:20} going {side:6} [{dev:10}] {n:5} games, won {w / n:.0%}{extra}")

    print("\n== server jobs ==")
    for row in conn.execute("SELECT id, kind, state, finished_at FROM jobs ORDER BY id DESC LIMIT 6"):
        print("  ", *row)


if __name__ == "__main__":
    main()
