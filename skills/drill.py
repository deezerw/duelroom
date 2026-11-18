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


# Interrupters that do nothing without a Link Monster equipped to them, and
# the equip a goal asks for with them. Desirae negates "up to the total Link
# Rating of Link Monsters equipped to this card": bare, that is zero cards,
# but upstream's script reading counts its Quick Effect regardless -- so the
# trainer kept bare-Desirae boards as bests and the games played them.
# Sequence (Link-2) is the equip the Fiendsmith line itself makes.
NEEDS_LINK_EQUIP = {82135803: 49867899}   # Fiendsmith's Desirae -> Sequence
TYPE_LINK = 0x4000000


# Interrupters that need a card of their own in your Monster Zone. Trivikarma
# targets "1 'Visas Starfrost' in your Monster Zone": imported runs no Visas
# (and Perlereino searches only Tearlaments or Visas), so its set copy negates
# nothing -- 55 games lost on turn 2 with it unused -- yet upstream's reading
# counts it, and 5 of the trainer's bests were built around it. Its GY search
# (banish -> a Spell/Trap that mentions Visas) still works: a card, not a piece.
NEEDS_ON_FIELD = {7436169: 56099748}      # Trivikarma -> Visas Starfrost


def _me(board, player=0):
    players = board.get("players") or []
    return players[player] if len(players) > player else {}


def link_equips(board, cards, player=0):
    """Total Link Rating of the Link Monsters in that player's spell/trap
    zone. A monster there is an equip; the board does not say equipped TO
    WHAT, so a Sequence on an Engraver is read as Desirae's too."""
    me = _me(board, player)
    n = 0
    for c in me.get("szone") or []:
        card = cards.get(c["code"])
        if card and card.type & TYPE_LINK:
            n += bin(card.link_marker).count("1")
    return n


def dead_on_field(board, cards, player=0):
    """Codes whose interruption cannot fire from that player's field (face-up
    or set) on this board: a bare Desirae, a Trivikarma without Visas."""
    dead = set() if link_equips(board, cards, player) else set(NEEDS_LINK_EQUIP)
    mzone = {cards.canonical(c["code"]) for c in _me(board, player).get("mzone") or []}
    return dead | {c for c, need in NEEDS_ON_FIELD.items() if need not in mzone}


def never_live(deck_codes):
    """Pieces a deck can never make work: their partner is not in it."""
    have = {int(c) for c in deck_codes}
    return {c for c, need in NEEDS_ON_FIELD.items() if need not in have}


def _on_field(line, weight=None):
    """Interruptions the end board holds on the FIELD, set or face-up.
    `weight(code)`: what each one is worth (default 1 each).

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
    dead = dead_on_field(board, cards)
    for c in me.get("mzone") or []:
        if cards.canonical(c["code"]) in dead:
            continue
        if any(i.where == "field" for i in card_interruptions(c["code"], cards)):
            n += weight(c["code"]) if weight else 1
    for c in me.get("szone") or []:
        if cards.canonical(c["code"]) in dead:
            continue
        faceup = bool(c.get("position", 0) & 0x5)
        where = "field" if faceup else "set"
        if any(i.where == where
               for i in card_interruptions(c["code"], cards)):
            n += weight(c["code"]) if weight else 1
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
            # POS_FACEDOWN is 0xA (0x2 face-down attack | 0x8 face-down
            # defence). The old test was `pos in (4, 8, 0xA)` under a
            # comment claiming 0x4|0x8 were the face-down bits -- but 0x4
            # is POS_FACEUP_DEFENSE, which is FACE UP. It therefore called
            # every face-up defence monster "(set)" and would have missed
            # a face-down attack one. 1480 of the 5416 board cards in
            # results/*.jsonl are position 4: 27.3% of them read as set
            # when they are face up, which for a monster is the difference
            # between its effect being live and not.
            #
            # `_on_field` below already used `& 0x5` correctly, so the
            # ranking and this text disagreed on the same card.
            pos = c.get("position", 0) or 0
            facing = " (set)" if pos & 0xA else ""
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
