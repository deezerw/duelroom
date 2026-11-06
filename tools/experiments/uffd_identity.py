"""Is the userfaultfd page tracker (R2V_ARENA_UFFD=1) EXACTLY the fault
handler, only faster? Deterministic mode (--threads 1, node and rollout
budgets, a wall budget that never binds) must give byte-identical solutions
and identical search counters with either tracker; R2V_ARENA_VERIFY=1 also
memcmp-checks every restore for an unmarked dirty page.

    venv/bin/python tools/experiments/uffd_identity.py <rollouts> <nodes>
"""

import hashlib
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import game  # noqa: E402

BIN = game.ROOTED_BIN.parent / "combosolver-stopwin"
OUT = ROOT / "results" / "experiments" / "uffd_identity"
CASES = [(0, 1009, 0), (0, 1005, 1), (3, 1003, 2)]   # (hand, enemy seed, run)


def main():
    rollouts, nodes = sys.argv[1], sys.argv[2]
    for h, e, r in CASES:
        d = game.GAMES / "imported" / "angelechy" / f"h{h}" / f"e{e}" / f"run{r}" / "t2"
        start, prefix = d / "start.yrpX", d / "prefix.yrpX"
        seen = {}
        for arm in ("fault", "uffd"):
            os.environ["R2V_ARENA_VERIFY"] = "1"
            if arm == "uffd":
                os.environ["R2V_ARENA_UFFD"] = "1"
            else:
                os.environ.pop("R2V_ARENA_UFFD", None)
            scratch = OUT / f"h{h}e{e}r{r}" / arm
            args = ["--start", start, "--root", prefix, "--no-ref", "--target", "35103106",
                    "--player", "0", "--turns", "3", "--goal-win", "--opp-greedy",
                    "--max-rollouts", rollouts, "--max-nodes", nodes]
            t0 = time.time()
            res = game._solve(prefix, args, scratch, "t3", 3_600_000, 7, threads=1,
                              binary=BIN)
            wall = time.time() - t0
            log = Path(res["log"]).read_text(errors="replace")
            outs = sorted((scratch / "out-t3").glob("*.yrp"))
            digest = hashlib.sha256(b"".join(p.read_bytes() for p in outs)).hexdigest()[:16]
            counters = re.findall(r"(\d+) rollouts\s+(\d+) states", log)
            bad = [ln for ln in log.splitlines() if "INCOMPLETE" in ln or "missed" in ln.lower()]
            seen[arm] = (digest, counters, len(outs))
            print(f"h{h} e{e} r{r} {arm:<5} {wall:6.1f}s  files={len(outs)} sha={digest} "
                  f"counters={counters} verify_problems={len(bad)}", flush=True)
            for ln in bad[:3]:
                print("   ", ln)
        print("   IDENTICAL" if seen["fault"] == seen["uffd"] else "   !! DIFFERENT", flush=True)


if __name__ == "__main__":
    main()
