"""STAGE9 Gate A: does the rooted solver (patch 0002, `--root`) search a
resumed position as well as it searches the duel's start?

Per hand seed, three arms on the SAME cards (the attacker's five plus its
turn-2 draw), the same target and the same budget, empty field throughout:

  first    pinned binary, synthetic start: those six cards going first
  approach pinned binary, turn 2 via --start/--approach (ISSUES.md #4's
           shape; expected to find nothing -- the baseline being fixed)
  rooted   rooted binary, turn 2 via --start/--root: every phase roots there

Pass (STAGE9.md): rooted solves >= 80% as often as first.

    venv/bin/python tools/experiments/rooted_gate.py [deck] [seeds] [ms]
"""

import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skills import breaking, engine, scenarios  # noqa: E402

ROOTED_BIN = (engine.VENDOR / "build" / "combo-solver-rooted" / "bin"
              / "combosolver")
DECKS = Path(__file__).resolve().parents[2] / "assets" / "decks"


def root_hand(prefix, player):
    """Codes in `player`'s hand at the position the prefix reaches."""
    from ygorl.engine.replay import Replay
    from ygorl.solver.targets import board_summary

    rep = Replay.from_yrp(Path(prefix))
    last = {}

    class _Obs:
        def on_start(self, core):
            last["b"] = board_summary(core, 0, (8000, 8000))

        def on_buffer(self, core, buf):
            last["b"] = board_summary(core, 0, (8000, 8000))

    rep.duel().replay(rep.responses, observer=_Obs())
    return list(last["b"]["players"][player]["hand"])


def run(cmd, scratch, label):
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, env=engine.env())
    (scratch / f"{label}.log").write_text(r.stdout + "\n--- stderr ---\n"
                                          + r.stderr)
    out = scratch / f"out-{label}"
    return {"solved": bool(list(out.glob("*.yrp"))), "rc": r.returncode,
            "s": round(time.time() - t0, 1)}


def base_cmd(binary, ref, wd):
    cmd = [str(binary), str(ref), "--workdir", str(wd.path)]
    for d in wd.scriptdirs:
        cmd += ["--scriptdir", str(d)]
    return cmd


def one_seed(deck, target, seed, ms, root):
    from ygorl.engine.replay import Replay
    from ygorl.solver.combo_solver import Workdir

    scratch = root / f"seed{seed}"
    scratch.mkdir(parents=True, exist_ok=True)
    tpl = breaking.template_vs(scratch / "tpl.yrpX", deck, deck, seed=seed,
                               shuffle=True)
    bare, _ = breaking.split_position(Replay.from_yrp(tpl), [],
                                      scratch / "bare")
    start, prefix = breaking.split_position(
        Replay.from_yrp(tpl), breaking.pass_turn_responses(bare),
        scratch / "control")
    st = breaking.check_root(prefix, expect_board=False)
    hand = root_hand(prefix, breaking.ATTACKER)
    res = {"seed": seed, "hand": hand, "root": st["players"]}
    budget = ["--solve-ms", str(ms), "--threads", "2", "--seed", str(seed)]

    for label in ("first", "approach", "rooted"):
        wd = Workdir.create(scratch / f"wd-{label}")
        out = scratch / f"out-{label}"
        out.mkdir(exist_ok=True)
        if label == "first":
            cmd = base_cmd(engine.SOLVER_BIN, tpl, wd) + [
                "--no-ref", "--deck", str(deck),
                "--hand", "|".join(map(str, hand)), "--target", target]
        elif label == "approach":
            cmd = base_cmd(engine.SOLVER_BIN, prefix, wd) + \
                breaking.attack_args(prefix, start, prefix, target,
                                     solve_ms=ms)[1:]
        else:
            cmd = base_cmd(ROOTED_BIN, prefix, wd) + [
                "--start", str(start), "--root", str(prefix), "--no-ref",
                "--target", target, "--player", str(breaking.ATTACKER),
                "--turns", "2"]
        if label != "approach":
            cmd += budget
        else:
            cmd += budget[2:]
        res[label] = run(cmd + ["--outdir", str(out)], scratch, label)
        print(f"seed {seed} {label:8s} {res[label]}", flush=True)
    return res


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "kewl-tune"
    seeds = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    ms = int(sys.argv[3]) if len(sys.argv) > 3 else engine.SOLVE_MS
    deck = DECKS / f"{name}.ydk"
    target = scenarios.target_for(name)
    root = Path(tempfile.mkdtemp(prefix=f"duelroom-gateA-{name}-"))
    print(f"deck {name}, target {target}, {seeds} seeds, {ms} ms -> {root}")
    with ThreadPoolExecutor(3) as ex:
        rows = list(ex.map(lambda s: one_seed(deck, target, s, ms, root),
                           range(seeds)))
    (root / "results.json").write_text(json.dumps(rows, indent=1))
    n = {k: sum(r[k]["solved"] for r in rows)
         for k in ("first", "approach", "rooted")}
    print(f"\nsolved: first {n['first']}/{seeds}  approach "
          f"{n['approach']}/{seeds}  rooted {n['rooted']}/{seeds}")
    if n["first"]:
        ratio = n["rooted"] / n["first"]
        print(f"rooted / first = {ratio:.2f} -> Gate A "
              f"{'PASS' if ratio >= 0.8 else 'FAIL'}")
    print(f"results: {root / 'results.json'}")


if __name__ == "__main__":
    main()
