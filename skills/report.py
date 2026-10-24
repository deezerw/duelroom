"""One view of everything known about a deck.

The data is spread across the card mirror, the inclusion tables, the
funnel runs and the board library, and reading it meant knowing which of
a dozen commands to run. This assembles what exists, says plainly what
is missing, and refuses to present a number whose target was flagged as
unmeasurable.
"""

import json
from pathlib import Path

from . import card_db, config, engine, gate, meta_scrape as ms, scenarios, stats
from .deck import Deck


def latest_funnel(deck_name):
    """Most recent funnel record for a deck, with where it came from.

    Returns (record, source_path) so callers can state provenance. A
    solve rate is close to meaningless without the budget it was run
    at -- 10s reports a brick where 60s solves the same hand in nine
    actions -- and two runs of the same deck look identical on screen.
    """
    best, best_key, best_src = None, (-1, -1.0), None
    # rglob, not glob: `optimizer.evaluate` writes its funnel records into
    # results/optimizer/, a SUBDIRECTORY, so a top-level glob missed every
    # measurement a deck had been through `optimize` for. The active deck
    # had a full 20-hand funnel record -- 15/20 solved, brick 0.25, the
    # one `paired_check` validated -- and this view still said "no funnel
    # run yet". A report whose job is to assemble what exists must look
    # where it is written.
    #
    # Trial records cannot be picked up by mistake: their `summary.deck`
    # is the generated trial name ("..._r0_17782288"), not the deck's.
    for p in (engine.ROOT / "results").rglob("*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = rec.get("summary") or {}
            if s.get("deck") == deck_name:
                cand = (_fired_count(s), mtime)
                if cand > best_key:
                    best, best_key, best_src = rec, cand, p
                break
    return best, best_src


def _fired_count(summary):
    """How many distinct hand traps a run actually fired.

    Recency alone is the wrong ranking, and switching to `rglob` proved
    it: the optimiser's base record for kewl-tune is NEWER than its gate
    run but fires only the top two threats (Imperm, Ash), so picking it
    silently dropped the Maxx "C" blind-spot note the report is supposed
    to carry -- a test caught that. More handtraps fired is more of the
    question answered, so that leads, with recency as the tiebreak.
    """
    keys = set()
    for field in ("survival_given_line", "window_recovery"):
        v = summary.get(field)
        if isinstance(v, dict):
            keys |= set(v)
    return len(keys)


def deck_report(conn, path, archetype=None):
    """Assemble everything known. Missing pieces are reported, not faked."""
    deck = Deck.from_ydk(path)
    unresolved = deck.canonicalise(conn)
    out = {
        "name": deck.name,
        "sizes": (len(deck.main), len(deck.extra), len(deck.side)),
        "unresolved": unresolved,
        "legality": deck.validate(conn),
        "engine_blockers": [],
        "target": None,
        "target_warning": None,
        "funnel": None,
        "provenance": None,
        "hand_math": [],
        "archetype": archetype,
        "archetype_guess": None,
        "archetype_alts": [],
        "inclusion": None,
    }

    from . import carddb
    out["engine_blockers"] = carddb.check_deck(deck)

    target = scenarios.target_for(deck.name)
    out["target"] = target
    if target:
        weak = scenarios.trivial_targets({deck.name: {
            "_extra": scenarios.target_is_extra(conn, path, target),
            "_zone_rank": 3 if "@" not in target else
                          scenarios._zone_rank(target),
            "_mean_actions": 99,
        }})
        if weak:
            out["target_warning"] = weak[0][1]

    rec, src = latest_funnel(deck.name)
    if rec:
        out["funnel"] = rec["summary"]
        cfg = rec.get("config") or {}
        out["provenance"] = {
            "file": src.name if src else "?",
            "solve_ms": cfg.get("solve_ms"),
            "hands": cfg.get("hands") or rec["summary"].get("hands"),
            "targets": rec.get("targets"),
        }

    size = len(deck.main)
    for name, copies in deck.named(conn, "main")[:6]:
        out["hand_math"].append(
            (name, copies, stats.open_chance(size, copies, 5))
        )

    if archetype is None:
        guesses = ms.identify_archetype(conn, deck)
        if guesses and guesses[0][1] >= 0.5:
            archetype = guesses[0][0]
            out["archetype"] = archetype
            out["archetype_guess"] = guesses[0]
            out["archetype_alts"] = guesses[1:3]

    if archetype:
        n = ms.compute_inclusion(conn, archetype)
        if n:
            buckets = ms.classify(conn, archetype)
            out["inclusion"] = {
                "lists": n,
                "core": len(buckets["core"]),
                "flex": len(buckets["flex"]),
                "off_meta": [
                    card_db.resolve(conn, c)["name"]
                    for c in set(deck.main)
                    if card_db.resolve(conn, c) and not any(
                        r["card_id"] == c
                        for b in buckets.values() for r in b)
                ][:8],
            }
    return out
