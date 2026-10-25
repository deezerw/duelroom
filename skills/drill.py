"""Export a deck's best lines as EDOPro replays, so they can be drilled.

A line the tool found and you cannot execute at the table is worth
nothing. The solver already verifies every line by replaying it in the
rules core, and those replays can be written as `.yrpX` -- the format
EDOPro opens -- so you can watch your own deck's best turn back, step
through it, and learn the sequence.

Alongside each replay this writes a plain-text card-by-card summary of
the end board, because scrubbing a replay to remember what a line
produced is slower than reading six lines of text.
"""

import json
from pathlib import Path

from . import card_db, engine

OUT = engine.ROOT / "results" / "drills"


def _name(conn, code):
    row = card_db.resolve(conn, int(code))
    return row["name"] if row is not None else f"<{code}>"


def interruption_counts(jsonl_path):
    """hand index -> interruption count, read from the run's summary.

    FALLBACK ONLY as of 2026-10-24: `lines_from` reads the record's own
    `solver.blocking.interruptions` first. This map is keyed by hand and
    silently yields nothing for hands the summary does not cover, which a
    `.get(..., 0)` turned into a real-looking zero.

    The per-line `interruptions` field in the JSONL is always None; the
    counts live in the companion `*_summary.json` under each deck's
    `boards`, keyed by hand. Ranking on the line field silently scored
    every line 0, so "best board first" returned the records in file
    order while claiming they were sorted.
    """
    summary = Path(str(jsonl_path).replace(".jsonl", "_summary.json"))
    if not summary.exists():
        summary = Path(str(jsonl_path).replace(".jsonl", ".json"))
    if not summary.exists():
        return {}
    try:
        data = json.loads(summary.read_text())
    except json.JSONDecodeError:
        return {}
    counts = {}
    for deck, d in (data.get("decks") or {}).items():
        for b in d.get("boards") or []:
            counts[(deck, b.get("hand"))] = len(b.get("interruptions") or [])
    return counts


def lines_from(jsonl_path, deck_name=None):
    """Solved records for a deck, best board first."""
    counts = interruption_counts(jsonl_path)
    out = []
    for raw in Path(jsonl_path).read_text().splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)
        if rec.get("status") != "solved":
            continue
        d = rec.get("deck")
        name = d.get("name") if isinstance(d, dict) else str(d)
        if deck_name and name != deck_name:
            continue
        # The record's OWN count first; the companion summary only as a
        # fallback. `counts.get(..., 0)` returned 0 for any hand the
        # summary did not cover, which is indistinguishable from "this
        # board has no interruptions" -- and the summary covers only the
        # hands the `sweep()` wrapper wrote. results/sweep_active.jsonl
        # has 40 hands and a summary for 0-19, so hands 20-39 all read 0
        # while their records held 4, 5 and 3.
        blocking = (rec.get("solver") or {}).get("blocking") or {}
        n = blocking.get("interruptions")
        if n is None:
            n = counts.get((name, rec.get("hand_index")))
        if n is None:
            n = _on_field(rec.get("lines", [{}])[0] if rec.get("lines") else {})
        for ln in rec.get("lines") or []:
            out.append((rec, ln, n))
    # Rank by what is ON THE FIELD first, then by the flat count.
    #
    # The flat interruption count scores a card HELD IN HAND the same as a
    # negate on the field, so the top line offered for practice on the
    # active deck was: open five handtraps with no engine, set two
    # Dominus Purge, hold three. Five "interruptions", 12 actions, and it
    # outranked two real boards (Blue-Eyes Spirit Dragon + Hieratic Seal)
    # at four. Practising "I had no engine so I passed" is not practice,
    # and `drill` exists to tell you which lines to rehearse.
    out.sort(key=lambda t: (-_on_field(t[1]), -t[2]))
    return out


def _on_field(line):
    """Interruptions the end board holds on the FIELD, set or face-up.

    Cards in hand are real interaction but they are not a line you can
    practise -- you hold them whatever you drew. Ranking on them puts the
    hands with no engine at the top.
    """
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import card_interruptions

    board = line.get("board") or {}
    players = board.get("players") or []
    if not players:
        return 0
    me, cards, n = players[0], default_cards(), 0
    for c in me.get("mzone") or []:
        if any(i.where == "field" for i in card_interruptions(c["code"], cards)):
            n += 1
    for c in me.get("szone") or []:
        faceup = bool(c.get("position", 0) & 0x5)
        where = "field" if faceup else "set"
        if any(i.where == where
               for i in card_interruptions(c["code"], cards)):
            n += 1
    return n


def describe(conn, line):
    """What the line ended on, as text."""
    board = line.get("board") or {}
    players = board.get("players") or []
    if not players:
        return []
    me = players[0]
    rows = []
    for zone, label in (("mzone", "monster"), ("szone", "spell/trap")):
        for c in me.get(zone) or []:
            pos = c.get("position")
            # EDOPro position bits: 0x4|0x8 are the face-down ones.
            facing = " (set)" if pos in (4, 8, 0xA) else ""
            rows.append(f"{label}: {_name(conn, c['code'])}{facing}")
    for c in (me.get("hand") or []):
        rows.append(f"hand: {_name(conn, c)}")
    return rows


def export(conn, jsonl_path, deck_name, limit=3, out_dir=None):
    """Write the top lines as .yrpX plus a text summary. Returns the files."""
    from ygorl.engine.replay import Replay

    out_dir = Path(out_dir or (OUT / deck_name))
    out_dir.mkdir(parents=True, exist_ok=True)
    found = lines_from(jsonl_path, deck_name)
    if not found:
        return []

    written = []
    for i, (rec, line, interrupts) in enumerate(found[:limit]):
        start = rec.get("start") or {}
        decks = start.get("decks") or {}
        if not decks.get("a"):
            continue
        rep = Replay(
            seed=0, first=0,
            rule_flags=start.get("rule_flags", 0),
            player=start.get("player", {}),
            shuffle_decks=False,
            decks=decks,
            responses=[bytes.fromhex(r) for r in line.get("responses") or []],
            seed_words=start.get("core_seed"),
        )
        stem = f"{deck_name}_line{i + 1}_{interrupts}int"
        yrpx = out_dir / f"{stem}.yrpX"
        rep.to_yrpx(yrpx, names=(deck_name, "goldfish"))

        hand = [_name(conn, c) for c in rec.get("hand") or []]
        txt = out_dir / f"{stem}.txt"
        body = [
            f"{deck_name} — line {i + 1}",
            f"{interrupts} interruption(s), {len(line.get('actions') or [])} actions",
            "",
            "opening hand:",
            *(f"  {c}" for c in hand),
            "",
            "ends on:",
            *(f"  {r}" for r in describe(conn, line)),
            "",
            f"replay: {yrpx.name}  (open in EDOPro)",
        ]
        txt.write_text("\n".join(body) + "\n")
        # Carry the ranked number too. Callers printed `interrupts` (the
        # flat count, which includes cards merely held in hand) while the
        # ORDER comes from on-field interruptions, so a "4" could sit
        # above a "5" with nothing explaining it -- three times in the
        # active deck's 80 lines.
        written.append((yrpx, txt, interrupts, _on_field(line)))
    return written
