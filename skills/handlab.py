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

import hashlib
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


def analyse(conn, deck_path, chosen, solve_ms=None, fire_ms=30_000,
            handtraps=None, fill_seed=0, pieces=3, log=None):
    """The whole report for one chosen opening hand.

    Returns a dict with the filled hand, the end board, the readable
    steps, and one entry per hand trap giving its windows in this line.
    """
    from ygorl.solver import Demonstration, HandJob, Workdir, find_solver
    from ygorl.solver.batch import solve_fire

    from . import scenarios
    from .deck import Deck

    say = log or (lambda m: None)
    # The project default, not something cheaper. A 30s budget reported
    # this module's own demonstration hand as UNSOLVED on one run and
    # solved on another -- a cheap run is a wrong run, not a fast one.
    solve_ms = engine.SOLVE_MS if solve_ms is None else solve_ms
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

        rec = solve_for_hand(job, hand, pieces=pieces, say=say)
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


# --- where a report lives ------------------------------------------------
#
# The web UI runs this through the job queue, so the page that asked for a
# report and the worker that produces it need to agree on the filename
# without passing one around.

REPORTS = ROOT / "results" / "pilot"


def report_path(deck_name, chosen, solve_ms, fire_ms):
    """Deterministic path for one (deck, hand, budget) report.

    The budget is in the key on purpose. The same hand at 30s and at 60s
    are different measurements -- this module's own demonstration hand
    was unsolved at 30s and solved at 60s -- and overwriting one with the
    other would make a report say something its budget line denies.
    """
    key = "|".join([deck_name, ",".join(str(c) for c in sorted(chosen)),
                    str(solve_ms), str(fire_ms)])
    return REPORTS / deck_name / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"


def save(report, path=None):
    path = Path(path) if path else report_path(
        report["deck"], report["chosen"], report["solve_ms"], report["fire_ms"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1))
    return path


def load(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None


def annotate_steps(report):
    """Hang each hand trap's windows on the step it interrupts.

    The solver names the card a window lands "over", and the step list
    names the card of every action, both in line order -- so the two can
    be matched and the combo shown with its weak points marked in place,
    which is the whole question: not "Ash beats this hand" but "Ash on
    your Branded Opening beats this hand".

    Matching is greedy and in order, because a card can be activated more
    than once in one line. A window whose card appears in no step is
    reported separately rather than attached to the wrong step -- a
    threat pinned to the wrong action is worse than one left unplaced.
    """
    steps_ = [dict(s, threats=[]) for s in report.get("steps") or []]
    unplaced = []
    for h in report.get("handtraps") or []:
        used = set()
        for d in h.get("detail") or []:
            over = d.get("over")
            hit = next((i for i, s in enumerate(steps_)
                        if s["name"] == over and i not in used), None)
            entry = {"name": h["name"], "code": h["code"],
                     "survived": d.get("survived"),
                     "missing": d.get("missing") or [],
                     "answers": d.get("answers") or [],
                     "decision": d.get("decision")}
            if hit is None:
                unplaced.append(dict(entry, over=over))
                continue
            used.add(hit)
            steps_[hit]["threats"].append(entry)
    return steps_, unplaced


def solve_for_hand(job, hand, pieces=3, pair=True, cards=None, scripts=None,
                   say=None):
    """``blocking.solve_blocking``, but planning targets from the REAL hand.

    Upstream's version takes an explicit ``job.hand`` and honours it in the
    duel -- and then plans what to aim for from ``sample_hand(deck,
    job.hand_seed)``, a *different*, seeded hand. The solver therefore
    searches your cards for a board chosen for somebody else's, and
    reports "no line found" when it cannot get there.

    That is not a hypothetical: the demonstration hand in this module's
    docstring solved under one seed and came back unsolved under another,
    with the hand identical both times. The seed was choosing the goal.

    So the hand goes into the plan as well as the duel. Everything else
    mirrors upstream: try each interrupter piece, try the best two
    together, fall back to whatever the hand can set or keep.
    """
    from dataclasses import replace

    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards
    from ygorl.solver.batch import solve_hand
    from ygorl.solver.blocking import (_score_demo, blocking_plan,
                                       solve_blocking as _upstream)

    say = say or (lambda m: None)
    cards = cards if cards is not None else default_cards()
    deck = load_ydk(job.deck_path)
    plan = blocking_plan(deck.main, deck.extra, list(hand), cards)

    results = []

    def attempt(targets, label):
        sub = replace(job, targets=tuple(targets),
                      scratch=Path(job.scratch) / f"a{len(results)}")
        demo = solve_hand(sub, cards=cards, scripts=scripts)
        score = _score_demo(demo, cards)
        info = {"label": label, "targets": list(targets),
                "status": demo.status, "wall_s": demo.solver.get("wall_s", 0.0),
                **(score.to_json() if score else {})}
        results.append((info, demo, score))
        say(f"  {label}: {demo.status} ({info['wall_s']:.0f}s)")
        return demo.status == "solved"

    solved_pieces = []
    for p in plan.pieces[:pieces]:
        if attempt([*plan.base, p.target], f"piece:{p.password}"):
            solved_pieces.append(p.target)
    if pair and len(solved_pieces) >= 2:
        attempt([*plan.base, *solved_pieces[:2]], "pair")
    if plan.base and not solved_pieces:
        attempt(list(plan.base), "base")
    if not results:
        # Nothing to aim for at all. Upstream builds the empty record for
        # this case; reuse it rather than inventing a second shape.
        return _upstream(job, pieces=pieces, pair=pair, cards=cards,
                         scripts=scripts)

    scored = [(i, r) for i, r in enumerate(results) if r[2] is not None]
    if scored:
        k, (_, demo, score) = max(scored, key=lambda t: (t[1][2].key(), -t[0]))
    else:
        k, (_, demo, score) = 0, results[0]
    demo.solver["blocking"] = {
        "base": plan.base, "pieces": [p.target for p in plan.pieces],
        "chosen": k, "attempts": [r[0] for r in results],
        **(score.to_json() if score else {}),
    }
    demo.solver["wall_s"] = round(sum(r[0]["wall_s"] for r in results), 2)
    return demo.to_json()
