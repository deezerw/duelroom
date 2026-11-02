"""A solved line as playmat frames, for watching it play out.

The solver stores a line as raw replay responses. `handlab.steps` turns
those into readable sentences; this turns them into BOARDS -- what both
fields, both hands and every pile hold after each thing that happens --
so the web page can draw the line the way a duel client does.

Two things here are deliberate.

**Every frame is the core's own answer, not a board kept by hand.**
After each message buffer the replay observer asks the live core for
both players' zones (`board_summary`). The tempting alternative is to
start from the opening hand and apply each MSG_MOVE to a model of the
zones, which is what a duel client does -- and it drifts: hand cards
renumber on every move, Xyz materials live at OVERLAY with the
monster's sequence, and a missed message is wrong forever after. A
snapshot cannot drift. The moves are still recorded, but only so the
page can animate a card flying from its old slot to its new one; they
never decide what is on the board.

**A frame is kept only when something visible happened.** The observer
sees several buffers per response, most of them phase changes and
prompts that change nothing on the field. Those fold into the next
frame that does, so stepping forward always shows a change.
"""

from . import card_db, engine  # noqa: F401  (engine exports the ygorl env)
from .handlab import _move_kind

# How a zone is named on the wire to the page. Index = the core's location bit.
ZONES = {0x1: "deck", 0x2: "hand", 0x4: "mz", 0x8: "sz", 0x10: "gy",
         0x20: "ban", 0x40: "extra", 0x80: "xyz"}

PHASES = {0x1: "Draw Phase", 0x2: "Standby Phase", 0x4: "Main Phase 1",
          0x8: "Battle Phase", 0x10: "Battle Phase", 0x20: "Battle Phase",
          0x40: "Battle Phase", 0x80: "Battle Phase", 0x100: "Main Phase 2",
          0x200: "End Phase"}


def _loc(loc):
    """A message location as the page addresses it."""
    return {"p": loc.controller, "z": ZONES.get(loc.location & 0xff, "?"),
            "s": loc.sequence}


def _side(player):
    """One player's half of `board_summary`, trimmed to what is drawn."""
    def cards(rows):
        return [{"c": r["code"], "pos": r["position"], "s": r["sequence"],
                 "o": list(r.get("overlay") or [])} for r in rows]
    return {
        "lp": player["lp"],
        "mz": cards(player["mzone"]),
        "sz": cards(player["szone"]),
        "hand": list(player["hand"]),
        "gy": list(player["grave"]),
        "ban": list(player["banished"]),
        "extra_up": list(player["extra_faceup"]),
        "deck": player["deck_count"],
        "extra": player["extra_count"],
    }


def frames(conn, start, responses, trim=True, cut_at_turn=True):
    """Replay a line and return {"frames": [...], "names": {code: name}}.

    `start` is the record's ``start`` dict and `responses` the line's
    hex response strings -- exactly what `handlab.steps` replays.

    Each frame: ``board`` (both sides), ``turn``, ``phase``, ``events``
    (headline sentences: activations, summons, sets, LP changes),
    ``moves`` (card movements, for animation only) and ``focus`` (the
    slot of the card the headline is about, to highlight it).
    """
    from ygorl.engine import constants as C
    from ygorl.engine.replay import Replay
    from ygorl.solver.targets import board_summary
    import ygorl.engine.messages as M

    out = []
    names = {}
    st = {"turn": 0, "phase": "", "lp": [8000, 8000],
          "events": [], "moves": [], "focus": None}

    def name(code):
        if code and code not in names:
            row = card_db.resolve(conn, code)
            names[code] = row["name"] if row else f"#{code}"
        return names.get(code, "a face-down card")

    def event(kind, text, code=0, loc=None):
        e = {"kind": kind, "text": text, "c": code}
        if kind == "activate" and loc is not None:
            # Where it was activated from: the trainer's "avoid this
            # move" is per zone (Engraver's hand effect is not its GY one).
            e["z"] = ZONES.get(loc.location & 0xff, "?")
        st["events"].append(e)
        if loc is not None:
            st["focus"] = _loc(loc)

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                k = type(m).__name__
                if k == "NewTurn":
                    st["turn"] += 1
                    if st["turn"] > 1:   # turn 1 is dealt before the first frame kept
                        # Replayed with first=0: seat 0 is you.
                        event("turn", "Turn passes to the opponent"
                              if m.player != 0 else "Your turn")
                elif k == "NewPhase":
                    st["phase"] = PHASES.get(m.phase, "")
                elif k == "Chaining":
                    event("activate", f"Activate {name(m.code)}", m.code, m.loc)
                elif k == "Summoning":
                    event("summon", f"Normal Summon {name(m.code)}", m.code, m.loc)
                elif k == "SpSummoning":
                    event("summon", f"Special Summon {name(m.code)}", m.code, m.loc)
                elif k == "FlipSummoning":
                    event("summon", f"Flip Summon {name(m.code)}", m.code, m.loc)
                elif k == "SetCard":
                    event("set", f"Set {name(m.code)}", m.code, m.loc)
                elif k == "Move":
                    st["moves"].append({"c": m.code, "from": _loc(m.previous),
                                        "to": _loc(m.current)})
                    what = _move_kind(m.previous, m.current, m.reason, C)
                    if what:
                        event("move", f"{name(m.code)} — {what}", m.code)
                elif k in ("Damage", "PayLpCost"):
                    st["lp"][m.player] -= m.amount
                    event("lp", f"Player {m.player + 1} loses {m.amount} LP")
                elif k == "Recover":
                    st["lp"][m.player] += m.amount
                    event("lp", f"Player {m.player + 1} gains {m.amount} LP")
                elif k == "LpUpdate":
                    st["lp"][m.player] = m.amount

            board = board_summary(core, st["turn"], tuple(st["lp"]))
            sides = [_side(p) for p in board["players"]]
            for s in sides:
                for code in (*s["hand"], *s["gy"], *s["ban"], *s["extra_up"]):
                    name(code)
                for row in (*s["mz"], *s["sz"]):
                    name(row["c"])
                    for o in row["o"]:
                        name(o)
            prev = out[-1]["board"] if out else None
            if sides == prev and not st["events"]:
                return          # nothing to see: fold into the next frame
            if sides == prev and not st["moves"] and not out[-1]["events"]:
                # The core moves a card and announces it in separate
                # buffers ("Move", then "Summoning"), so the summon's
                # caption would land one step after the card did. A
                # caption that changes nothing belongs to the frame
                # that moved the card.
                out[-1]["events"] = st["events"]
                out[-1]["focus"] = st["focus"]
                st["events"], st["focus"] = [], None
                return
            out.append({"board": sides, "turn": st["turn"],
                        "phase": st["phase"], "events": st["events"],
                        "moves": st["moves"], "focus": st["focus"]})
            st["events"], st["moves"], st["focus"] = [], [], None

    rep = Replay(
        seed=0, first=0, rule_flags=start["rule_flags"],
        player=start["player"], shuffle_decks=False, decks=start["decks"],
        responses=[bytes.fromhex(r) for r in responses],
        seed_words=start.get("core_seed"),
    )
    rep.duel().replay(rep.responses, observer=_Obs())

    # The frames before the first action are the hands being dealt. Start
    # on the last of them: the full opening hand, nothing played yet. NOT
    # "the first Main Phase frame" -- a line may open in the Draw Phase
    # (Branded Opening does), and cutting there hid its first two plays.
    first = next((i for i, f in enumerate(out) if f["events"]), len(out))
    if trim and first:
        out = out[first - 1:]
        out[0]["events"] = [{"kind": "start", "text": "Opening hand", "c": 0}]
        out[0]["turn"] = max(1, out[0]["turn"])
        out[0]["phase"] = out[0]["phase"] or "Draw Phase"
        out[0]["moves"], out[0]["focus"] = [], None
    # The line is over when the turn passes; what follows is the
    # opponent drawing for a turn the solver never played.
    end = next((i for i, f in enumerate(out)
                if any(e["kind"] == "turn" for e in f["events"])), None)
    # A whole game (STAGE9) keeps every turn: `cut_at_turn=False`.
    if trim and cut_at_turn and end is not None:
        out = out[:end + 1]
    return {"frames": out, "names": {str(k): v for k, v in names.items()}}


def line_moves(conn, start, responses):
    """The plays a line makes, as a set: ("activate", code, zone) and
    ("summon", code). What the trainer checks a line against the moves
    the player asked it to avoid."""
    out = set()
    for f in frames(conn, start, responses, trim=False)["frames"]:
        for e in f["events"]:
            if e["kind"] == "activate":
                out.add(("activate", e["c"], e.get("z", "?")))
            elif e["kind"] == "summon":
                out.add(("summon", e["c"]))
    return out


def from_record(conn, record, line_index=0):
    """`frames` for one line of a solver record (`start` + `lines`)."""
    lines = record.get("lines") or []
    start = record.get("start") or {}
    if not lines or not (start.get("decks") or {}).get("a"):
        return None
    return frames(conn, start, lines[line_index].get("responses") or [])
