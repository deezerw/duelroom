"""Solve ONE chosen opening hand, in full detail.

Everything else in this project asks statistical questions: over forty
random hands, how often does this deck function. This asks the question
you actually have at a table -- I am holding *these* cards, what is the
best I can do, and where does it fall apart.

Three answers, from three mechanisms that already exist in the engine:

* **The board.** ``HandJob(hand=...)`` forces an exact opening hand
  instead of sampling one from a seed, so the solver searches the hand
  you asked about.
* **The steps.** The solved line is stored as raw replay responses. Feed
  them back through the rules core and decode the message stream, and the
  line becomes "normal summon Aluber, activate Aluber, ...".
* **The critical points.** ``--fire`` already plays a hand trap at every
  legal window of the line, one attempt per window, and the solver's own
  log names the window: which card it landed on, at which decision, and
  what the recovery was missing.

Two honesty constraints carried from the rest of the project.

An unconverted window is **not** proof that the hand is dead there. The
solver says so itself -- it is a sampling budget per window, and the same
hand measured twice gave 1 of 3 Ash windows converted and then 0 of 3.
The report says "no recovery found in Ns", never "this loses".

And the per-window detail is scraped from a human-readable log that is
partly in French. If the format moves, parsing must fail loudly rather
than return an empty list that reads as "no windows".
"""

import json
import re
import shutil
import tempfile
from pathlib import Path

from . import card_db, engine

ROOT = Path(__file__).resolve().parent.parent
HAND_SIZE = 5

# "window  1 (dec.  13, summon  1, over Aluber the Jester of Despia): pas
#  convertie  [17393 rollouts, best 4/6]  manque : Rindbrumm, Albion"
_WINDOW = re.compile(
    r"^\s*window\s+(?P<i>\d+)\s*\((?P<pos>[^)]*)\):\s*(?P<verdict>.+?)\s*$")
_OVER = re.compile(r"over\s+(?P<card>.+?)\s*$")
_DEC = re.compile(r"dec\.\s*(?P<dec>\d+)")
_SUMMON = re.compile(r"summon\s*(?P<n>\d+)")
_MISSING = re.compile(r"manque\s*:\s*(?P<cards>.+?)\s*(?:\[|$)")
_ROLLOUTS = re.compile(r"\[(?P<n>\d+)\s+rollouts(?:,\s*best\s*(?P<a>\d+)/(?P<b>\d+))?")
_ANSWERS = re.compile(r"^\s*window\s+(?P<i>\d+):\s*answers to the threat:\s*(?P<a>.+?)\s*$")
_COUNT = re.compile(r"(\d+)\s+window\(s\) where")

# The solver writes its report in a mix of English and French.
_CONVERTED = ("convertie", "converted")
_NOT = ("pas convertie", "not converted")
_PASS_ONLY = ("passer seulement", "pass only")


class LogFormatChanged(RuntimeError):
    """The solver's window report did not parse. Never swallow this."""


def parse_windows(log_text):
    """Per-window verdicts from one --fire solver log.

    Returns (windows, declared_count). `declared_count` is what the solver
    said it found, so the caller can check we parsed all of them rather
    than silently reporting a subset.
    """
    answers, rows = {}, {}
    for line in log_text.splitlines():
        m = _ANSWERS.match(line)
        if m:
            a = m.group("a").strip()
            answers[int(m.group("i"))] = (
                [] if any(p in a for p in _PASS_ONLY)
                else [x.strip() for x in a.split(",") if x.strip()])
            continue
        m = _WINDOW.match(line)
        if not m or "answers to the threat" in line:
            continue
        i = int(m.group("i"))
        pos, verdict = m.group("pos"), m.group("verdict")
        over = _OVER.search(pos)
        dec = _DEC.search(pos)
        sm = _SUMMON.search(pos)
        roll = _ROLLOUTS.search(verdict)
        miss = _MISSING.search(verdict)
        survived = not any(p in verdict for p in _NOT) and any(
            p in verdict for p in _CONVERTED)
        rows[i] = {
            "window": i,
            "over": over.group("card").strip() if over else None,
            "decision": int(dec.group("dec")) if dec else None,
            "after_summons": int(sm.group("n")) if sm else None,
            "survived": survived,
            "rollouts": int(roll.group("n")) if roll else None,
            "missing": ([x.strip() for x in miss.group("cards").split(",")]
                        if miss else []),
            "answers": [],
        }
    for i, a in answers.items():
        if i in rows:
            rows[i]["answers"] = a

    declared = None
    found = _COUNT.findall(log_text)
    if found:
        declared = int(found[-1])
    return [rows[i] for i in sorted(rows)], declared


def _name(conn, code):
    row = card_db.resolve(conn, code)
    return row["name"] if row else f"<{code}>"


# Message types worth showing. Everything else in the stream is bookkeeping.
STEP_KINDS = {
    "Summoning": "normal summon",
    "SpSummoning": "special summon",
    "FlipSummoning": "flip summon",
    "Chaining": "activate",
    "SetCard": "set",
}


def steps(conn, record, line_index=0):
    """The solved line as readable steps, by replaying it.

    The line is stored only as raw replay responses; the engine is what
    turns those back into "normal summon Aluber". No search happens here,
    so it costs a fraction of a second.
    """
    from ygorl.engine.replay import Replay
    import ygorl.engine.messages as M

    lines = record.get("lines") or []
    start = record.get("start") or {}
    if not lines or not (start.get("decks") or {}).get("a"):
        return []
    line = lines[line_index]

    out = []

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                kind = type(m).__name__
                verb = STEP_KINDS.get(kind)
                if not verb or not hasattr(m, "code"):
                    continue
                out.append({"verb": verb, "code": m.code,
                            "name": _name(conn, m.code)})

    rep = Replay(
        seed=0, first=0, rule_flags=start["rule_flags"], player=start["player"],
        shuffle_decks=False, decks=start["decks"],
        responses=[bytes.fromhex(r) for r in line.get("responses") or []],
        seed_words=start.get("core_seed"),
    )
    rep.duel().replay(rep.responses, observer=_Obs())
    for n, s in enumerate(out, 1):
        s["n"] = n
    return out


def fill_hand(conn, deck, chosen, seed=0, size=HAND_SIZE):
    """Complete a partial hand from the rest of the deck.

    Picking three cards leaves two unknown, and those two decide plenty.
    The filler is drawn from the deck's actual remaining copies so the
    hand is one that can really be held, and the caller is told what was
    added -- a filled slot must never read as a card the user chose.
    """
    import random

    pool = list(deck.main)
    for c in chosen:
        if c in pool:
            pool.remove(c)
        else:
            raise ValueError(f"{_name(conn, c)} is not in this deck's main deck")
    if len(chosen) > size:
        raise ValueError(f"a hand is {size} cards; you picked {len(chosen)}")
    rng = random.Random(seed)
    filler = rng.sample(pool, size - len(chosen)) if len(chosen) < size else []
    return list(chosen) + filler, filler


def analyse(conn, deck_path, chosen, solve_ms=30_000, fire_ms=20_000,
            handtraps=None, fill_seed=0, pieces=3, log=None):
    """The whole report for one chosen opening hand.

    Returns a dict with the filled hand, the end board, the readable
    steps, and one entry per hand trap giving its windows in this line.
    """
    from ygorl.solver import Demonstration, HandJob, Workdir, find_solver
    from ygorl.solver.batch import solve_fire
    from ygorl.solver.blocking import solve_blocking

    from . import scenarios
    from .deck import Deck

    say = log or (lambda m: None)
    engine.require()
    deck_path = Path(deck_path).resolve()
    deck = Deck.from_ydk(deck_path)
    deck.canonicalise(conn)
    engine.preflight([deck_path])

    hand, filler = fill_hand(conn, deck, list(chosen), seed=fill_seed)
    say(f"hand: {', '.join(_name(conn, c) for c in hand)}"
        + (f"  (filled: {', '.join(_name(conn, c) for c in filler)})" if filler else ""))

    scratch = Path(tempfile.mkdtemp(prefix="duelroom-hand-"))
    try:
        job = HandJob(
            deck_path=deck_path, hand_index=0, hand_seed=fill_seed, targets=(),
            workdir=Workdir.create(scratch / "wd").path, scratch=scratch / "job",
            solve_ms=solve_ms, fire_ms=fire_ms, threads=1, hand=tuple(hand),
            binary=find_solver(), keep_files=True, lines=1,
            timeout_s=solve_ms / 1000 + 300)

        rec = solve_blocking(job, pieces=pieces, pair=True)
        base = Demonstration.from_json(rec)
        say(f"solve: {base.status}")

        out = {
            "deck": deck_path.stem,
            "hand": hand,
            "hand_names": [_name(conn, c) for c in hand],
            "chosen": list(chosen),
            "filled": filler,
            "status": base.status,
            "solve_ms": solve_ms,
            "fire_ms": fire_ms,
            "interruptions": (rec.get("solver") or {}).get(
                "blocking", {}).get("interruptions"),
            "steps": [],
            "board": [],
            "handtraps": [],
            "error": rec.get("error") or "",
        }
        if base.status != "solved":
            return out

        out["steps"] = steps(conn, rec)
        out["board"] = _board(conn, rec)

        for code, name in sorted((handtraps or scenarios.HANDTRAPS).items(),
                                 key=lambda kv: kv[1]):
            say(f"firing {name} ...")
            fd = solve_fire(job, base, code)
            j = fd.to_json()
            meta = j.get("solver") or {}
            row = {"code": code, "name": name, "status": fd.status,
                   "windows": meta.get("windows"),
                   "converted": meta.get("converted"),
                   "blind_spot": scenarios.BLIND_SPOTS.get(code),
                   "detail": [], "parse_error": None}
            logf = scratch / "job" / f"fire_{code}" / "solver.log"
            if logf.exists():
                try:
                    detail, declared = parse_windows(logf.read_text())
                    if declared is not None and len(detail) != declared:
                        raise LogFormatChanged(
                            f"solver reported {declared} windows, parsed "
                            f"{len(detail)}")
                    row["detail"] = detail
                except (LogFormatChanged, ValueError) as e:
                    # Never return a short list that reads as "few windows".
                    row["parse_error"] = str(e)
            out["handtraps"].append(row)
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _board(conn, record, line_index=0):
    lines = record.get("lines") or []
    if not lines:
        return []
    players = (lines[line_index].get("board") or {}).get("players") or []
    if not players:
        return []
    me, rows = players[0], []
    for zone, label in (("mzone", "monster"), ("szone", "spell/trap")):
        for c in me.get(zone) or []:
            facing = " (set)" if c.get("position") in (4, 8, 0xA) else ""
            rows.append({"zone": label, "name": _name(conn, c["code"]),
                         "code": c["code"], "set": bool(facing)})
    for c in me.get("hand") or []:
        rows.append({"zone": "hand", "name": _name(conn, c), "code": c,
                     "set": False})
    return rows


def resolve_in_deck(conn, deck, text):
    """A card the deck actually plays, from a name or fragment.

    Restricted to the deck's own main deck on purpose: "ash" should mean
    the Ash Blossom in your list, and a fragment matching nothing you
    play is a typo, not a request to test a card you do not own.
    """
    text = str(text).strip()
    if not text:
        raise ValueError("empty card")
    if text.isdigit() and int(text) in deck.main:
        return int(text)

    low = text.lower()
    names = {}
    for code in set(deck.main):
        names[_name(conn, code)] = code
    exact = [c for n, c in names.items() if n.lower() == low]
    if exact:
        return exact[0]
    hits = sorted(n for n in names if low in n.lower())
    if not hits:
        raise ValueError(f"no main-deck card matches {text!r}")
    if len(hits) > 1:
        raise ValueError(
            f"{text!r} matches {len(hits)}: " + ", ".join(hits[:4])
            + (" ..." if len(hits) > 4 else ""))
    return names[hits[0]]


def parse_hand(conn, deck, text):
    """'nadir, aluber, branded opening' -> [codes], order kept."""
    parts = [p for p in re.split(r"\s*[|,]\s*", str(text)) if p.strip()]
    if not parts:
        raise ValueError("name at least one card")
    if len(parts) > HAND_SIZE:
        raise ValueError(f"a hand is {HAND_SIZE} cards; you named {len(parts)}")
    return [resolve_in_deck(conn, deck, p) for p in parts]
