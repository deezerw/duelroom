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


# --- end-board library ---------------------------------------------------

def load_boards(conn, summary_path, fmt="Tournament Meta Decks", source_run=None):
    """Store solve_blocking end boards into `boards`.

    This is what replaces bot-vs-bot duelling. Each meta deck is goldfished
    going first and the boards it actually assembles are recorded with their
    frequency. Stage 4 then treats each as a static puzzle: deal yourself a
    going-second hand and search for a break. No opponent AI anywhere.

    Identical boards are collapsed and counted, so `frequency` is the share
    of that deck's solved hands ending on that board -- which is what Stage
    4 must weight break rates by. A board they assemble one time in ten is
    not worth optimizing against.
    """
    summary = json.loads(Path(summary_path).read_text())
    stored = 0
    with conn:
        for deck, d in (summary.get("decks") or {}).items():
            boards = d.get("boards") or []
            if not boards:
                continue
            # Collapse identical boards: the interruption set is the board's
            # identity for our purposes.
            groups = Counter()
            negates = {}
            for b in boards:
                key = tuple(sorted(b.get("interruptions") or []))
                groups[key] += 1
                negates[key] = len(key)
            total = sum(groups.values())
            conn.execute(
                "DELETE FROM boards WHERE archetype = ? AND format = ?",
                (deck, fmt),
            )
            for key, n in groups.most_common():
                conn.execute(
                    """INSERT INTO boards
                         (archetype, format, board_state, frequency, negates,
                          source_run)
                       VALUES (?,?,?,?,?,?)""",
                    (deck, fmt, json.dumps(list(key)), n / total,
                     negates[key], source_run),
                )
                stored += 1
    return stored


def board_library(conn, fmt="Tournament Meta Decks"):
    """The stored library, most common board first per archetype."""
    rows = conn.execute(
        """SELECT archetype, board_state, frequency, negates
             FROM boards WHERE format = ?
         ORDER BY archetype, frequency DESC""",
        (fmt,),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["archetype"], []).append({
            "interruptions": json.loads(r["board_state"]),
            "frequency": r["frequency"],
            "negates": r["negates"],
        })
    return out


def split_zones(pieces):
    """Separate interruptions you can see from ones you cannot.

    ygorl labels each end-board interruption with its zone: "(field,
    negate)", "(set, negate)", "(hand, negate)". Counting them together
    overstates how hard a board is to break, and understates how much of
    the difficulty is hidden information. Going second you can play around
    a visible negate; you cannot play around a handtrap you have not seen.

    Branded is the clear case: its single most common "interruption" is
    Whisker Blitzclique in HAND at 64%, which is not a board at all.
    """
    visible, hidden = [], []
    for p in pieces:
        (hidden if "(hand" in p else visible).append(p)
    return visible, hidden


def threat_profile(conn, fmt="Tournament Meta Decks"):
    """Per-archetype distribution over interruption COUNT, not exact board.

    The exact interruption set is too fine to weight against: 12 hands per
    deck produced 69 distinct boards, so almost every board is a sample of
    one and its "frequency" is noise. What Stage 4 actually asks is "can I
    break a 3-interruption board", and the count distribution answers that
    at a sample size we can afford.

    Keep `board_library` for reading what the boards contain; use this for
    weighting.
    """
    rows = conn.execute(
        """SELECT archetype, negates, SUM(frequency) AS share
             FROM boards WHERE format = ?
         GROUP BY archetype, negates ORDER BY archetype, negates""",
        (fmt,),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["archetype"], {})[r["negates"]] = r["share"]
    for arch, dist in out.items():
        total = sum(dist.values()) or 1
        out[arch] = {k: v / total for k, v in sorted(dist.items())}
    return out


def common_pieces(conn, fmt="Tournament Meta Decks", top=5):
    """Which interruptions actually show up, per archetype.

    Robust where exact boards are not: a card appearing in 60% of an
    archetype's end boards is a real fact at 12 hands, even though the
    board it sits in is a sample of one. This is the list your side deck
    has to answer.
    """
    out = {}
    for r in conn.execute(
        "SELECT archetype, board_state, frequency FROM boards WHERE format = ?",
        (fmt,),
    ):
        bucket = out.setdefault(r["archetype"], Counter())
        for piece in json.loads(r["board_state"]):
            bucket[piece] += r["frequency"]
    return {a: c.most_common(top) for a, c in out.items()}


def board_vs_hand(conn, fmt="Tournament Meta Decks"):
    """Mean visible-board interruptions vs mean hand-held ones, per deck.

    The first number is what a board breaker has to get through. The second
    is what it has to survive. Optimising against their sum is optimising
    against a board that does not exist.
    """
    out = {}
    for r in conn.execute(
        "SELECT archetype, board_state, frequency FROM boards WHERE format = ?",
        (fmt,),
    ):
        vis, hid = split_zones(json.loads(r["board_state"]))
        acc = out.setdefault(r["archetype"], {"field": 0.0, "hand": 0.0, "w": 0.0})
        acc["field"] += len(vis) * r["frequency"]
        acc["hand"] += len(hid) * r["frequency"]
        acc["w"] += r["frequency"]
    return {
        a: {"field": v["field"] / (v["w"] or 1), "hand": v["hand"] / (v["w"] or 1)}
        for a, v in out.items()
    }


def load_blocking(conn, jsonl_path, scenario="blocking"):
    """Store solve_blocking per-hand records into `runs` / `trials`.

    funnel_eval and solve_blocking emit different shapes -- the former one
    record per deck with a `hands` list, the latter one record per hand.
    Both end up in the same two tables so Stage 5 has a single place to
    read per-card liveliness from.
    """
    records = [json.loads(l) for l in Path(jsonl_path).read_text().splitlines() if l.strip()]
    by_deck = {}
    for r in records:
        # `deck` is a dict {name, main, extra} here, unlike funnel_eval's
        # per-deck record where the name sits in `summary.deck`.
        d = r.get("deck")
        name = d.get("name") if isinstance(d, dict) else str(d)
        by_deck.setdefault(name, []).append(r)

    v = engine.versions()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_ids = {}
    with conn:
        for deck, hands in by_deck.items():
            cur = conn.execute(
                """INSERT INTO runs
                     (deck_hash, deck_name, scenario, seed_base, n_trials,
                      going_first, engine_version, scripts_version,
                      cdb_version, started, finished)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (deck, deck, scenario, 0, len(hands), 1,
                 f"ygorl {v['ygorl']} / core {v['ygopro_core']}",
                 v["card_scripts"], v["babel_cdb"], now, now),
            )
            rid = cur.lastrowid
            run_ids[deck] = rid
            rows = []
            for h in hands:
                lines = h.get("lines") or []
                pieces = []
                if lines:
                    best = max(lines, key=lambda l: len(l.get("interruptions") or []))
                    pieces = best.get("interruptions") or []
                rows.append((
                    rid,
                    str(h.get("hand_seed", 0)),
                    json.dumps(h.get("hand") or []),
                    1 if h.get("status") == "solved" else 0,
                    len(pieces),
                    len(pieces),
                    None,
                    json.dumps({"status": h.get("status"),
                                "interruptions": pieces}),
                ))
            conn.executemany(
                """INSERT INTO trials
                     (run_id, seed, opening_hand, completed, board_score,
                      negates, special_summons, line)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
    return run_ids


def liveliness_from_db(conn, deck_name, conn_cards=None):
    """Per-card solve rate for a deck, read back out of `trials`."""
    rows = conn.execute(
        """SELECT t.opening_hand, t.completed FROM trials t
             JOIN runs r ON r.id = t.run_id
            WHERE r.deck_name = ?""",
        (deck_name,),
    ).fetchall()
    solved, total = Counter(), Counter()
    for r in rows:
        for c in set(json.loads(r["opening_hand"])):
            total[c] += 1
            if r["completed"]:
                solved[c] += 1
    out = []
    for c, n in total.items():
        out.append({
            "name": (card_db.resolve(conn, c) or {"name": f"<{c}>"})["name"],
            "seen": n,
            "solved": solved[c],
            "solve_rate": solved[c] / n,
        })
    return sorted(out, key=lambda r: (r["solve_rate"], -r["seen"]))
