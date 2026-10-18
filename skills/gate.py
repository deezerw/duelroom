"""Validation gate, budget calibration, and result loading.

The solver reports a hand as a "brick" when it fails to find a line inside
its time budget. That conflates two very different things: a hand with no
line, and a hand whose line the search did not reach in time. The second is
a *false brick*, and it silently inflates every downstream number.

Upstream measured false bricks at a 5 s budget (+17 to +19 points on
long-combo decks) and settled on 10 s. Measured here, 10 s is still not
enough for Snake-Eye into Snake-Eyes Flamberge Dragon:

    budget   bricks     note
      10 s   3/12 (25%)  hand 1 wrongly called a brick
      60 s   2/12 (17%)  converged
     120 s   2/12 (17%)  stable

Hand 1 (Droll, Bonfire, Original Sinful Spoils, Dark Ruler No More, Ash)
solves in 9 actions given 28.5 s of search. At 10 s it was reported dead.
Its Ash survival flipped too -- from "brick" to "survives 2/4" -- so the
handtrap matrix is budget-sensitive as well, not just the brick rate.

So: never assume a budget. Run `budget_ladder` per deck and use the budget
where the brick count stops moving. A cheaper run is a wrong run.
"""

import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import card_db, engine
from .stats import wilson

LADDER = (10_000, 30_000, 60_000, 120_000)


def run_funnel(deck, hands=12, solve_ms=60_000, workers=None, out=None,
               targets=None, fire=None, timeout=None):
    """One funnel_eval pass. Returns the parsed per-deck record."""
    engine.require()
    deck = Path(deck).resolve()
    workers = workers or engine.DEFAULT_WORKERS
    out = Path(out) if out else (
        engine.ROOT / "results" / f"funnel_{deck.stem}_{solve_ms//1000}s.jsonl"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()  # funnel_eval skips decks already present

    cmd = [
        "nice", "-n", "19", str(engine.PYTHON), "tools/funnel_eval.py", str(deck),
        "--hands", str(hands), "--solve-ms", str(solve_ms),
        "--workers", str(workers), "--no-filter", "--out", str(out),
    ]
    if targets:
        cmd += ["--targets", str(Path(targets).resolve())]
    for f in fire or ():
        cmd += ["--fire", str(f)]
    subprocess.run(cmd, env=engine.env(), cwd=str(engine.VENDOR),
                   timeout=timeout, check=True)
    return [json.loads(l) for l in out.read_text().splitlines() if l.strip()][0]


def budget_ladder(deck, hands=12, budgets=LADDER, workers=None, targets=None):
    """Run the same hands at increasing budgets until bricks stop moving.

    The converged budget is the cheapest one whose brick count matches the
    most expensive run. Anything below it is measuring the search, not the
    deck.
    """
    rows = []
    for ms in budgets:
        rec = run_funnel(deck, hands=hands, solve_ms=ms, workers=workers,
                         targets=targets)
        s = rec["summary"]
        rows.append({
            "solve_ms": ms,
            "bricks": s["bricks"],
            "solved": s["solved"],
            "hands": s["hands"],
            "brick_rate": s["brick_rate"],
            "survival": s.get("hand_trap_survival"),
            "solver_s": s.get("solver_s"),
            "record": rec,
        })

    final = rows[-1]["bricks"]
    converged = next((r["solve_ms"] for r in rows if r["bricks"] == final), None)
    for r in rows:
        r["converged"] = r["solve_ms"] >= (converged or 10 ** 9)
        r["false_bricks"] = r["bricks"] - final
    return rows, converged


# --- gate ----------------------------------------------------------------

def inspect_bricks(record, conn):
    """Every brick hand, by card name, so a human can judge it.

    The gate is not a number on its own -- a brick hand holding an obvious
    starter means the solver failed, whatever the rate says.
    """
    out = []
    for h in record["hands"]:
        if h["status"] != "brick":
            continue
        out.append({
            "index": h["index"],
            "cards": [
                (card_db.resolve(conn, c) or {"name": f"<{c}>"})["name"]
                for c in h["hand"]
            ],
            "solver_s": h.get("solver_s"),
        })
    return out


def gate_report(record, conn):
    """Summarise one converged run for the validation gate."""
    s = record["summary"]
    solved, hands = s["solved"], s["hands"]
    bricks = inspect_bricks(record, conn)
    p, lo, hi = wilson(solved, hands)
    return {
        "deck": s["deck"],
        "hands": hands,
        "solved": solved,
        "bricks": s["bricks"],
        "solve_rate": p,
        "solve_rate_ci": (lo, hi),
        # Conditional on the hand having a line at all: the gate's real
        # question. Bricks confirmed genuine by inspection do not count
        # against the solver.
        "brick_hands": bricks,
        "survival_given_line": s.get("survival_given_line", {}),
        "combo_actions": s.get("combo_actions", {}),
        "solver_s": s.get("solver_s"),
    }


# --- loading into the database -------------------------------------------

def load_funnel(conn, record, scenario="funnel", seed_base=0):
    """Store a funnel_eval record into `runs` and `trials`.

    Version pins go on the run row: the components move independently and a
    mismatch changes results silently rather than erroring.
    """
    s = record["summary"]
    cfg = record.get("config", {})
    v = engine.versions()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with conn:
        cur = conn.execute(
            """INSERT INTO runs
                 (deck_hash, deck_name, scenario, seed_base, n_trials,
                  going_first, engine_version, scripts_version, cdb_version,
                  started, finished)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.get("deck", {}).get("name", "?"),
                s["deck"],
                f"{scenario}@{cfg.get('solve_ms')}ms",
                cfg.get("seed", seed_base),
                s["hands"],
                1,
                f"ygorl {v['ygorl']} / core {v['ygopro_core']}",
                v["card_scripts"],
                v["babel_cdb"],
                now, now,
            ),
        )
        run_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO trials
                 (run_id, seed, opening_hand, completed, board_score,
                  negates, special_summons, line)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    run_id,
                    str(h.get("hand_seed", 0)),  # see schema: unsigned 64-bit
                    json.dumps(h["hand"]),
                    1 if h["status"] == "solved" else 0,
                    h.get("combo_actions"),
                    None,
                    None,
                    json.dumps({"fire": h.get("fire", {}),
                                "decisions": h.get("combo_decisions")}),
                )
                for h in record["hands"]
            ],
        )
    return run_id


# --- metrics duelroom adds -----------------------------------------------

def card_liveliness(record, conn):
    """How often each card sat in a hand that solved vs one that bricked.

    The garnet detector. A card that appears often and only in bricks is
    dead weight; this feeds Stage 5's candidate pool.
    """
    solved, bricked = Counter(), Counter()
    for h in record["hands"]:
        bucket = solved if h["status"] == "solved" else bricked
        for c in set(h["hand"]):
            bucket[c] += 1
    rows = []
    for c in set(solved) | set(bricked):
        s, b = solved[c], bricked[c]
        rows.append({
            "name": (card_db.resolve(conn, c) or {"name": f"<{c}>"})["name"],
            "in_solved": s,
            "in_brick": b,
            "seen": s + b,
            "solve_rate": s / (s + b),
        })
    return sorted(rows, key=lambda r: (r["solve_rate"], -r["seen"]))


def classify_hands(record):
    """god / strong / normal / brick, by how short the line was.

    More useful than a single brick percentage: a deck that always needs
    11 actions plays nothing like one that opens 6.
    """
    out = Counter()
    for h in record["hands"]:
        if h["status"] != "solved":
            out["brick"] += 1
            continue
        a = h.get("combo_actions") or 99
        out["god" if a <= 6 else "strong" if a <= 9 else "normal"] += 1
    return out
