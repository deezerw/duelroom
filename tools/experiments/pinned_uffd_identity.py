"""Gate for the pinned solver + 0003's tracker (tools/build_pinned_uffd.sh).

Same search, faster? Deterministic mode (1 thread, fixed seed, fixed rollout count)
on trained `imported` hands, three arms: the pinned binary, the uffd build with the
tracker OFF, and ON. Pass = identical solution bytes and counters in all three.
Then a throughput pass at the trainer's settings (wall budget): states per arm.
Usage: pinned_uffd_identity.py [rollouts] [hands] [throughput_ms]
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from skills import engine  # noqa: E402

PINNED = engine.VENDOR / "build" / "combo-solver" / "bin" / "combosolver"
UFFD = engine.VENDOR / "build" / "combo-solver-uffd" / "bin" / "combosolver"
ARMS = [("pinned", PINNED, None), ("uffd-off", UFFD, None), ("uffd-on", UFFD, "1")]


def hands(n):
    conn = sqlite3.connect(ROOT / "data" / "duelroom.db")
    dh = conn.execute("SELECT deck_hash FROM trainer_hands GROUP BY deck_hash "
                      "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    return conn.execute("SELECT hand_index, hand_seed, hand, best_goal FROM trainer_hands "
                        "WHERE deck_hash=? AND best_goal IS NOT NULL AND best_goal != '' "
                        "ORDER BY hand_index LIMIT ?", (dh, n)).fetchall()


def solve(binary, uffd, h, rollouts, solve_ms):
    from ygorl.engine.duel import default_cards
    from ygorl.solver import HandJob, Workdir
    from ygorl.solver.batch import solve_hand
    if uffd:
        os.environ["R2V_ARENA_UFFD"] = uffd
    else:
        os.environ.pop("R2V_ARENA_UFFD", None)
    idx, seed, hand, goal = h
    scratch = Path(tempfile.mkdtemp(prefix="pinned-uffd-"))
    job = HandJob(deck_path=(engine.DECKS / "imported.ydk").resolve(), hand_index=idx,
                  hand_seed=int(seed), targets=tuple(json.loads(goal)),
                  workdir=Workdir.create(scratch / "wd").path, scratch=scratch / "job",
                  solve_ms=solve_ms, threads=1, hand=tuple(json.loads(hand)), lines=4,
                  binary=binary, solver_seed=12345, max_rollouts=rollouts,
                  keep_files=True, timeout_s=solve_ms / 1000 + 600)
    demo = solve_hand(job, cards=default_cards()).to_json()
    outs = sorted((scratch / "job" / "out").glob("*.yrp*"))
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in outs)).hexdigest()[:16]
    out = json.dumps(demo.get("solver", {}))
    states = [int(x) for x in re.findall(r'"states": (\d+)', out)]
    return digest, len(outs), demo.get("status"), states, demo["solver"].get("wall_s")


def main():
    rollouts = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    tp_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 30_000
    engine._export()
    ok = True
    for h in hands(n):
        seen = {}
        for name, binary, uffd in ARMS:
            r = solve(binary, uffd, h, rollouts, 3_600_000)
            seen[name] = r[:3]
            print(f"h{h[0]} {name:<8} sha={r[0]} files={r[1]} {r[2]} states={r[3]} {r[4]}s",
                  flush=True)
        same = len(set(seen.values())) == 1
        ok &= same
        print("   IDENTICAL" if same else "   !! DIFFERENT", flush=True)
    print("IDENTITY PASS" if ok else "IDENTITY FAIL", flush=True)
    if not ok or not tp_ms:
        return
    for name, binary, uffd in (ARMS[0], ARMS[2]):
        tot = 0
        for h in hands(n):
            r = solve(binary, uffd, h, None, tp_ms)
            tot += sum(r[3])
            print(f"throughput h{h[0]} {name:<8} states={r[3]} {r[2]}", flush=True)
        print(f"THROUGHPUT {name}: {tot} states over {n} hands at {tp_ms} ms", flush=True)


if __name__ == "__main__":
    main()
