"""Scenario suite: handtrap matrix and derived solver targets.

`funnel_eval` fires a handtrap into every solved hand and reports whether
the line survives -- that is the handtrap matrix. It wants a per-deck
targets file, though, and ygorl only ships one for its own ten test decks.
Scraped tournament lists have no curated target.

Rather than hand-writing targets for decks nobody here plays, derive them:
`solve_blocking` already searched each deck with automatically planned
targets and recorded which ones it actually reached. The most frequently
reached target IS that deck's canonical payoff, measured instead of
guessed, and it updates itself when the meta moves.
"""

import json
from collections import Counter
from pathlib import Path

from . import card_db, engine

# The handtraps worth testing against. Passcodes verified against the
# card mirror; names kept for reports.
HANDTRAPS = {
    14558127: "Ash Blossom & Joyous Spring",
    10045474: "Infinite Impermanence",
    23434538: 'Maxx "C"',
    27204311: "Nibiru, the Primal Being",
    94145021: "Droll & Lock Bird",
    59438930: "Ghost Ogre & Snow Rabbit",
    97268402: "Effect Veiler",
}

# Common floodgates for the Stage 4 static check. Locals run these far more
# than top tables do.
FLOODGATES = {
    82732705: "Skill Drain",
    83326048: "Dimensional Barrier",
    90846359: "Rivalry of Warlords",
    97642679: "There Can Be Only One",
    23616456: "Summon Limit",
}


def derive_targets(jsonl_path, per_deck=1, min_hits=2):
    """Build a funnel_eval targets file from solve_blocking results.

    Counts the targets each deck actually reached and keeps the most
    common. A target reached once in twelve hands is noise, so `min_hits`
    filters those out unless nothing else qualifies.

    ONE target per deck by default. A targets list is a CONJUNCTION -- the
    final board must contain every entry (batch.py checks it with
    `board_summary_missing`, and `_check_targets` counts all of them) --
    so two targets demands both at once, which is far harder than either
    and reads as a brick when they rarely co-occur. Measured: branded went
    from 11/12 solved with per-hand auto targets to bricking repeatedly on
    a two-target conjunction. ygorl's own curated file uses a single
    target for nine of its ten decks; the exception is labrynth, whose
    real board genuinely is a pair. Raise `per_deck` only when a deck's
    signature board is itself a combination.
    """
    records = [
        json.loads(l)
        for l in Path(jsonl_path).read_text().splitlines()
        if l.strip()
    ]
    hits = {}
    for r in records:
        if r.get("status") != "solved":
            continue
        d = r.get("deck")
        name = d.get("name") if isinstance(d, dict) else str(d)
        hits.setdefault(name, Counter()).update(r.get("targets") or [])

    out = {}
    for deck, counter in hits.items():
        ranked = counter.most_common()
        # Prefer targets that are BUILT over targets that are DEALT. An
        # "@hand" target only asks that a card be in hand, which a draw can
        # satisfy without the deck doing anything -- firing a handtrap at
        # that measures nothing. Branded derived two @hand targets
        # (Whisker Blitzclique, Droll) purely because it ends on almost no
        # visible board.
        built = [(t, n) for t, n in ranked if "@hand" not in t]
        dealt = [(t, n) for t, n in ranked if "@hand" in t]
        ordered = built + dealt

        kept = [t for t, n in ordered if n >= min_hits][:per_deck]
        if not kept and ordered:
            kept = [ordered[0][0]]
        if kept:
            out[deck] = {
                "targets": kept,
                "fire": sorted(HANDTRAPS),
                "_built_targets": sum(1 for t in kept if "@hand" not in t),
            }
    return out


def write_targets(jsonl_path, out_path, per_deck=1, min_hits=2):
    targets = derive_targets(jsonl_path, per_deck, min_hits)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for spec in targets.values():
        spec.pop("_built_targets", None)
    payload = {
        "_comment": (
            "Derived by duelroom from solve_blocking results: the targets "
            "each deck actually reached most often. Regenerate after a "
            "meta refresh."
        ),
        **targets,
    }
    out_path.write_text(json.dumps(payload, indent=1))
    return targets


def describe_targets(conn, targets):
    """Render a derived targets file with card names."""
    rows = []
    for deck, spec in sorted(targets.items()):
        named = []
        for t in spec["targets"]:
            code = int(t.split("@")[0])
            row = card_db.resolve(conn, code)
            zone = f"@{t.split('@')[1]}" if "@" in t else ""
            named.append((row["name"] if row else f"<{code}>") + zone)
        rows.append((deck, named))
    return rows


# --- handtrap matrix ------------------------------------------------------

def handtrap_matrix(records):
    """Per deck per handtrap: survival, computed from the per-hand records.

    NOT `summary.survival_given_line`. That field counts a `no_window`
    outcome -- the handtrap never had a legal moment to be used -- as a
    survival, which conflates two opposite facts and inverts the answer.

    Sky-striker vs Nibiru is the clear case: the summary reports 90%
    survival, but the per-hand records are `no_window` nine times and
    `unsolved` once. Nibiru got exactly one legal window and STOPPED the
    line. Where testable its survival is 0%, not 90%, and the inflated
    number is most of why Nibiru ranked last among handtraps to main.

    So report both, because both matter and they are different questions:
      survival  -- of the windows where it COULD be used, how often did
                   the line still complete (did they play through it)
      windows   -- how often it had any legal window at all (is it even
                   live in this matchup)
    A handtrap with no window is useless here, but for a different reason
    than one that gets played through, and the fixes differ.
    """
    matrix = {}
    for rec in records:
        s = rec.get("summary") or {}
        deck = s.get("deck")
        if not deck:
            continue
        tested, survived, offered = Counter(), Counter(), Counter()
        for h in rec.get("hands") or []:
            if h.get("status") != "solved":
                continue
            for code, f in (h.get("fire") or {}).items():
                code = int(code)
                offered[code] += 1
                st = f.get("status")
                if st == "no_window":
                    continue
                tested[code] += 1
                if st == "solved":      # line still completed
                    survived[code] += 1
        row = {}
        for code in offered:
            n = tested[code]
            row[code] = {
                "survival": (survived[code] / n) if n else None,
                "tested": n,
                "offered": offered[code],
                "window_rate": n / offered[code] if offered[code] else 0.0,
            }
        matrix[deck] = {
            "cells": row,
            "solved": s.get("solved"),
            "hands": s.get("hands"),
            "brick_rate": s.get("brick_rate"),
        }
    return matrix


def weighted_handtrap_ranking(matrix, weights):
    """Which handtraps to main, weighted by who you actually face.

    `weights` is the locals.toml share per archetype. A handtrap that
    blanks a deck nobody in your room plays is worth nothing, which is
    the entire reason that file exists.
    """
    scores, coverage = Counter(), Counter()
    total_w = 0.0
    for deck, data in matrix.items():
        w = weights.get(deck, 0.0)
        if not w:
            continue
        total_w += w
        for code, cell in data["cells"].items():
            # Expected value of drawing it: it must have a window AND the
            # line must fail through it. Scoring stop-rate alone would
            # reward a handtrap that is never live, which is the trap the
            # summary field falls into.
            surv = cell["survival"]
            stop_given_window = 0.0 if surv is None else (1.0 - surv)
            scores[code] += w * cell["window_rate"] * stop_given_window
            coverage[code] += w * cell["window_rate"]
    if total_w:
        for code in scores:
            scores[code] /= total_w
            coverage[code] /= total_w
    return [
        (code, HANDTRAPS.get(code, str(code)), score, coverage[code])
        for code, score in scores.most_common()
    ]


def run_matrix(decks, targets_path, hands=12, solve_ms=None, workers=None,
               fire_ms=3000, out=None, timeout=None):
    """funnel_eval across several decks with every handtrap fired."""
    import subprocess

    engine.require()
    engine.preflight(decks)
    solve_ms = solve_ms or engine.SOLVE_MS
    workers = workers or engine.DEFAULT_WORKERS
    out = Path(out) if out else engine.ROOT / "results" / "handtrap_matrix.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "nice", "-n", "19", str(engine.PYTHON), "tools/funnel_eval.py",
        *[str(Path(d).resolve()) for d in decks],
        "--targets", str(Path(targets_path).resolve()),
        "--hands", str(hands), "--solve-ms", str(solve_ms),
        "--fire-ms", str(fire_ms), "--workers", str(workers),
        "--no-filter", "--out", str(out),
    ]
    for code in sorted(HANDTRAPS):
        cmd += ["--fire", str(code)]
    subprocess.run(cmd, env=engine.env(), cwd=str(engine.VENDOR),
                   timeout=timeout, check=True)
    return [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
