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

# The solver writes its report in a mix of English and French, and
# CHANGES CASE between outcomes: a failure reads "pas convertie", a
# success "CONVERTIE (board COMPLET)". Matched case-insensitively, and
# the failure is tested first because it contains the success word.
_CONVERTED = ("convertie", "converted")
_NOT = ("pas convertie", "non convertie", "not converted")
_PASS_ONLY = ("passer seulement", "pass only")
# A window on an empty chain lands on no card of ours at all.
_OPEN = ("ouverte", "open")


class LogFormatChanged(RuntimeError):
    """The solver's window report did not parse. Never swallow this."""


# "=== --fire verdict: 3 window(s) out of 3 converted (3 full board, 0
#  without the sacrificed card) ==="
_VERDICT = re.compile(
    r"--fire verdict:\s*(?P<conv>\d+)\s*window\(s\) out of\s*(?P<tot>\d+)"
    r"\s*converted\s*\((?P<full>\d+)\s*full board,\s*(?P<part>\d+)\s*"
    r"without the sacrificed card\)", re.IGNORECASE)


def parse_verdict(log_text):
    """{converted, windows, full, partial} from the solver's summary line.

    `partial` is how many windows were only answerable by giving up the
    spare card. It is always 0 unless --fire-spare was offered, which is
    the difference between asking "can you rebuild this exact board" and
    "how much of it can you keep".
    """
    m = _VERDICT.search(log_text or "")
    if not m:
        return None
    return {"converted": int(m.group("conv")), "windows": int(m.group("tot")),
            "full": int(m.group("full")), "partial": int(m.group("part"))}


def _is_open(pos):
    return any(f.strip().lower() in _OPEN for f in pos.split(","))


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
        low = verdict.lower()
        survived = (not any(p in low for p in _NOT)
                    and any(p in low for p in _CONVERTED))
        rows[i] = {
            "window": i,
            "over": over.group("card").strip() if over else None,
            # "OUVERTE": an empty-chain window. It interrupts the turn
            # rather than a particular card of ours, so it is placed by
            # summon count instead of by name.
            #
            # Matched as a whole comma-separated field, never as a
            # substring: "over Branded Opening" contains "open".
            "open_window": _is_open(pos),
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


def _move_kind(prev, cur, reason, C):
    """What a card movement MEANS, in the words a player would use."""
    p, q = getattr(prev, "location", 0), getattr(cur, "location", 0)
    if reason & C.REASON_MATERIAL:
        return "material"
    if q == C.LOCATION_HAND and p in (C.LOCATION_DECK, C.LOCATION_EXTRA):
        return "searched"
    if q == C.LOCATION_HAND and p == C.LOCATION_GRAVE:
        return "recovered"
    if reason & C.REASON_DISCARD:
        return "discarded"
    if reason & C.REASON_COST:
        return "paid as cost"
    if p == C.LOCATION_DECK and q == C.LOCATION_GRAVE:
        return "milled"
    if q == C.LOCATION_REMOVED:
        return "banished"
    if p == C.LOCATION_EXTRA and q == C.LOCATION_MZONE:
        return None          # the summon itself, already its own step
    if q == C.LOCATION_GRAVE and p in (C.LOCATION_MZONE, C.LOCATION_SZONE):
        return "to the grave"
    return None


def steps(conn, record, line_index=0):
    """The solved line as readable steps, by replaying it.

    The line is stored only as raw replay responses; the engine is what
    turns those back into "normal summon Aluber". No search happens here,
    so it costs a fraction of a second.

    Each step also carries what it actually did -- what it searched, what
    it used as material, what it paid. Those come from MSG_MOVE, and they
    are attributed by CHAIN LINK, not by whichever activation happened to
    be printed last: a chain resolves backwards, so the nearest preceding
    Chaining is usually the wrong card to credit. In the demonstration
    hand that mistake hands Aluber's search to Branded Opening.
    """
    from ygorl.engine import constants as C
    from ygorl.engine.replay import Replay
    import ygorl.engine.messages as M

    lines = record.get("lines") or []
    start = record.get("start") or {}
    if not lines or not (start.get("decks") or {}).get("a"):
        return []
    line = lines[line_index]

    out = []
    link_step = {}      # chain link -> index into `out`
    state = {"current": None, "pending": None}

    summons = [0]

    def add(verb, code):
        # Nibiru counts summons, and so do the solver's window reports,
        # so every summon carries its ordinal: "the 5th summon" has to be
        # a thing you can point at.
        idx = None
        if verb.endswith("summon"):
            summons[0] += 1
            idx = summons[0]
        out.append({"verb": verb, "code": code, "name": _name(conn, code),
                    "did": [], "summon_index": idx,
                    "summons_so_far": summons[0]})
        state["current"] = len(out) - 1
        return len(out) - 1

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                kind = type(m).__name__

                if kind == "Chaining":
                    i = add("activate", m.code)
                    link_step[getattr(m, "chain_count", None)] = i
                    continue
                if kind in ("Summoning", "SpSummoning", "FlipSummoning",
                            "SetCard"):
                    add(STEP_KINDS[kind], m.code)
                    continue
                if kind == "ChainSolving":
                    # Resolution, in reverse order. Everything until the
                    # next one belongs to THIS link.
                    state["current"] = link_step.get(
                        getattr(m, "chain_count", None), state["current"])
                    continue
                if kind != "Move" or state["current"] is None:
                    continue
                what = _move_kind(m.previous, m.current, m.reason, C)
                if what is None:
                    continue
                step = out[state["current"]]
                entry = {"what": what, "code": m.code,
                         "name": _name(conn, m.code)}
                if entry not in step["did"]:
                    step["did"].append(entry)

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


def exposure(rows):
    """Windows a line offers that no recovery was found for. Lower is better.

    Counting only failures would call a line that gives Ash four chances
    and survives three (one loss) worse than one that gives her a single
    chance and loses it. Both lose once; the second is the safer line to
    take, so the number of chances offered breaks the tie.
    """
    lost = sum(max(0, (r.get("windows") or 0) - (r.get("converted") or 0))
               for r in rows)
    offered = sum(r.get("windows") or 0 for r in rows)
    return lost, offered


def analyse(conn, deck_path, chosen, solve_ms=None, fire_ms=30_000,
            handtraps=None, fill_seed=0, pieces=4, log=None, candidates=1,
            threads=None):
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
    # ONE hand, so there is nothing to parallelise across -- the batch
    # path runs six hands as six single-threaded processes, and copying
    # its threads=1 here left a pilot run using one core of eight while
    # the other seven sat idle. The solver parallelises its own search,
    # so give it the cores instead.
    #
    # The cost is reproducibility: --threads 1 makes two runs do exactly
    # the same work. That was never worth 1/6th of the machine here,
    # because the budget is wall-clock anyway, so a pilot report is not
    # reproducible at any thread count.
    threads = engine.DEFAULT_WORKERS if threads is None else max(1, threads)
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
            solve_ms=solve_ms, fire_ms=fire_ms, threads=threads,
            hand=tuple(hand),
            binary=find_solver(), keep_files=True, lines=max(1, candidates),
            timeout_s=solve_ms / 1000 + 300)

        rec = solve_for_hand(job, hand, pieces=pieces, say=say)
        base = Demonstration.from_json(rec)
        say(f"solve: {base.status}")

        out = {
            "deck": deck_path.stem,
            "threads": threads,
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
            "reading": {"interruptions": [], "follow_up": [], "resources": {}},
            "handtraps": [],
            "error": rec.get("error") or "",
        }
        if base.status != "solved":
            return out

        out["steps"] = steps(conn, rec)
        out["board"] = _board(conn, rec)
        out["reading"] = read_board(conn, rec)
        out["candidates"] = []

        # The goal the climb settled on, so the LAST piece it added can be
        # offered as expendable. Without a spare the fire test asks only
        # "can you rebuild this exact board", and a line that keeps two
        # of three interrupters reads identically to one that keeps none.
        blocking = (rec.get("solver") or {}).get("blocking", {})
        attempts = blocking.get("attempts") or []
        chosen = attempts[blocking.get("chosen", 0)] if attempts else {}
        goal = [t for t in (chosen.get("targets") or [])]
        spare = goal[-1] if len(goal) > 1 else None
        out["goal"] = goal
        out["spare"] = spare
        out["spare_name"] = (_name(conn, int(spare))
                             if spare and str(spare).isdigit() else spare)
        if spare:
            say(f"offering {spare} as expendable, so a partial rebuild counts")

        traps = scenarios.HANDTRAPS if handtraps is None else handtraps
        # Each trap is an INDEPENDENT solver run, and running them one at
        # a time left most of the machine idle: thread-parallelism inside
        # one solver tops out well short of the core count because the
        # replay and the fresh-replay verification of every candidate
        # line are serial. Processes over independent work is the model
        # the batch path already proves, so the traps are fired together.
        for code, name, j in _fire_all(job, base, traps, threads, say,
                                       spare=spare):
            meta = j.get("solver") or {}
            row = {"code": code, "name": name, "status": j.get("status"),
                   "windows": meta.get("windows"),
                   "converted": meta.get("converted"),
                   "blind_spot": scenarios.BLIND_SPOTS.get(code),
                   "detail": [], "parse_error": None,
                   # How far you still get when it lands and you play on:
                   # the board the recovery reached, and its line.
                   "recovered_board": _board(conn, j) if j.get("lines") else [],
                   "recovered_reading": (read_board(conn, j)
                                         if j.get("lines") else None),
                   "recovered_steps": (steps(conn, j) if j.get("lines") else [])}
            logf = Path(job.scratch) / f"fire_{code}" / "solver.log"
            if logf.exists():
                try:
                    detail, declared = parse_windows(logf.read_text())
                    if declared is not None and len(detail) != declared:
                        raise LogFormatChanged(
                            f"solver reported {declared} windows, parsed "
                            f"{len(detail)}")
                    row["detail"] = detail
                    v = parse_verdict(logf.read_text())
                    if v:
                        row["full"] = v["full"]
                        row["partial"] = v["partial"]
                except (LogFormatChanged, ValueError) as e:
                    # Never return a short list that reads as "few windows".
                    row["parse_error"] = str(e)
            out["handtraps"].append(row)
        # Completion order is whatever finished first; the report is read
        # by a person, so put it back in a stable order.
        out["handtraps"].sort(key=lambda r: r["name"])

        # The best board and the board that plays around the most hand
        # traps are not the same line. With more than one verified line
        # kept, every one is fired at and the trade is shown rather than
        # decided here.
        if candidates > 1 and len(rec.get("lines") or []) > 1 and traps:
            out["candidates"] = _rank_candidates(
                conn, job, base, rec, traps, say)

        # The line and the threats are the same story told twice unless
        # they are joined: each step carries the windows that land on it.
        out["steps"], out["unplaced"] = annotate_steps(out)
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


def random_hand(deck, seed=None, size=HAND_SIZE):
    """A hand the deck can really open, drawn from its actual copies.

    Returns (hand, seed). The seed comes back so the deal can be repeated
    -- a report nobody can reproduce is an anecdote.
    """
    import random

    if len(deck.main) < size:
        raise ValueError(f"the main deck holds {len(deck.main)} cards")
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    return random.Random(seed).sample(list(deck.main), size), seed


def parse_traps(text):
    """'ash, imperm' -> {code: name}. Empty text means every one we know."""
    from . import scenarios

    raw = (text or "").strip()
    if not raw or raw.lower() == "all":
        return dict(scenarios.HANDTRAPS)
    if raw.lower() in ("none", "-"):
        return {}
    want = [p.strip().lower() for p in re.split(r"\s*[|,]\s*", raw) if p.strip()]
    out, unknown = {}, []
    for w in want:
        hit = [(c, n) for c, n in scenarios.HANDTRAPS.items()
               if w == str(c) or w == n.lower() or w in n.lower()]
        if len(hit) != 1:
            unknown.append(w)
            continue
        out[hit[0][0]] = hit[0][1]
    if unknown:
        raise ValueError(
            f"unknown hand trap(s): {', '.join(unknown)} — known: "
            + ", ".join(sorted(scenarios.HANDTRAPS.values())))
    return out


def parse_hand(conn, deck, text):
    """'nadir, aluber, branded opening' -> [codes], order kept.

    'random' deals one instead; 'random:1234' repeats a given deal.
    """
    raw = str(text).strip()
    if raw.lower().startswith("random"):
        _, _, s = raw.partition(":")
        return random_hand(deck, int(s) if s.strip().isdigit() else None)[0]
    parts = [p for p in re.split(r"\s*[|,]\s*", raw) if p.strip()]
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


def report_path(deck_name, chosen, solve_ms, fire_ms, traps="", lines=1):
    """Deterministic path for one (deck, hand, budget) report.

    The budget is in the key on purpose. The same hand at 30s and at 60s
    are different measurements -- this module's own demonstration hand
    was unsolved at 30s and solved at 60s -- and overwriting one with the
    other would make a report say something its budget line denies.
    """
    key = "|".join([deck_name, ",".join(str(c) for c in sorted(chosen)),
                    str(solve_ms), str(fire_ms),
                    ",".join(sorted(str(t) for t in
                                    (traps.split(",") if traps else []))),
                    str(lines)])
    return REPORTS / deck_name / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"


def save(report, path=None):
    path = Path(path) if path else report_path(
        report["deck"], report["chosen"], report["solve_ms"], report["fire_ms"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1))
    return path


def load(path):
    """A stored report, brought up to the current shape.

    Reports written before the line and the threats were joined have
    steps with no windows attached, so they render as a plain list with
    nothing to open. They can be joined on read by card name. What they
    cannot get is summon ordinals -- those need the original replay,
    which the report does not keep -- so their empty-chain windows stay
    unplaced and the page says how many.
    """
    path = Path(path)
    if not path.exists():
        return None
    r = json.loads(path.read_text())
    # Reports measured before the verdict parser was fixed stored every
    # window as a failure, including the ones the run's own totals say
    # converted. Detect the contradiction rather than render it: the
    # counts are from the solver's event and still true, the per-window
    # verdicts are not.
    for h in r.get("handtraps") or []:
        detail = h.get("detail") or []
        h["detail_stale"] = bool(
            detail and (h.get("converted") or 0) > 0
            and not any(d.get("survived") for d in detail))

    if r.get("steps") and "threats" not in r["steps"][0]:
        r["steps"], r["unplaced"] = annotate_steps(r)
        r["legacy"] = True
    else:
        # Already joined, but the staleness above is decided at read
        # time, so it has to reach the badges on the line too.
        stale = {h["code"] for h in (r.get("handtraps") or [])
                 if h.get("detail_stale")}
        for st in r.get("steps") or []:
            for t in st.get("threats") or []:
                t["stale"] = t.get("code") in stale
    r.setdefault("unplaced", [])
    r.setdefault("legacy", False)
    return r


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
            entry = {"name": h["name"], "code": h["code"],
                     "survived": d.get("survived"),
                     "stale": bool(h.get("detail_stale")),
                     "missing": d.get("missing") or [],
                     "answers": d.get("answers") or [],
                     "decision": d.get("decision"),
                     "after_summons": d.get("after_summons"),
                     "open_window": bool(d.get("open_window"))}
            if over:
                hit = next((i for i, s in enumerate(steps_)
                            if s["name"] == over and i not in used), None)
            else:
                # An empty-chain window lands on no card of ours. It is
                # placed by summon count instead -- which is how Nibiru
                # is reasoned about anyway: "after the fifth summon".
                # Not subject to `used`: two empty-chain windows at the
                # same summon count are genuinely the same point in the
                # line, told apart by their decision index. The one-per-
                # step rule exists for a card activated twice, which is
                # a different situation.
                k = d.get("after_summons")
                hit = next((i for i, s in enumerate(steps_)
                            if s.get("summon_index") == k), None) if k else None
            if hit is None:
                unplaced.append(dict(entry, over=over))
                continue
            if over:
                used.add(hit)
            steps_[hit]["threats"].append(entry)
    return steps_, unplaced


def solve_for_hand(job, hand, pieces=4, pair=True, cards=None, scripts=None,
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

    # Which interrupters the hand can reach at all, one at a time.
    solved_pieces = []
    for p in plan.pieces[:pieces]:
        if attempt([*plan.base, p.target], f"piece:{p.password}"):
            solved_pieces.append(p.target)

    # Then CLIMB. Upstream stops at a pair, so the best board it ever
    # asks for is base + 2 -- and a deck with four reachable interrupters
    # is never asked whether it can make three. That is not the solver
    # failing to find a good board; it is nobody requesting one, and it
    # is why these reports keep coming back with two interruptions.
    #
    # Greedy: add the next piece to the best goal that still solves, and
    # stop at the first refusal. Each rung costs one solve, so a deck
    # with four pieces pays three extra at most, and only when the
    # earlier rungs succeeded.
    if pair and len(solved_pieces) >= 2:
        goal = [solved_pieces[0]]
        for nxt in solved_pieces[1:]:
            trying = goal + [nxt]
            label = "pair" if len(trying) == 2 else f"stack:{len(trying)}"
            if not attempt([*plan.base, *trying], label):
                break
            goal = trying
        say(f"  best goal reached: {len(goal)} interrupter(s) "
            f"+ {len(plan.base)} from hand")

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
    # `score.to_json()` carries its own "pieces" -- the cards the END BOARD
    # interrupts with -- so splatting it last silently ATE the plan's piece
    # list in every record ever written (271 of 280 in results/boards.jsonl
    # hold the board's pieces under that key, the other 9 being the unsolved
    # ones where there is no score to overwrite it). Both are wanted, and
    # the pair of them is the only way to ask the question this report
    # exists to answer: what was the ladder offered, and how far up did it
    # get? Keep the score's shape, name the plan's separately.
    demo.solver["blocking"] = {
        "base": plan.base, "plan_pieces": [p.target for p in plan.pieces],
        "chosen": k, "attempts": [r[0] for r in results],
        **(score.to_json() if score else {}),
    }
    demo.solver["wall_s"] = round(sum(r[0]["wall_s"] for r in results), 2)
    return demo.to_json()


# --- reading the end board ----------------------------------------------
#
# A list of card names is not an answer to "what does this board do".
# ygorl already derives interruptions from the card SCRIPTS -- not from a
# hand-written table -- and it covers the graveyard and the hand as well
# as the field, which is most of what follow-up means. What it leaves out
# is every quick effect that is not a negate or a disruption: a Spirit
# Dragon tagging out, a searcher that still has its effect. Those come
# from the same script facts, read for their categories instead.

# Categories worth naming when a card can use them on the opponent's turn
# but they are neither a negate nor a disruption.
FOLLOW_UP = {
    "SPECIAL_SUMMON": "special summon",
    "FUSION_SUMMON": "fusion summon",
    "SEARCH": "search",
    "TOHAND": "add to hand",
    "RECOVER": "recover",
    "DRAW": "draw",
    "TOGRAVE": "send to grave",
    "LEAVE_GRAVE": "leave the grave",
    "EQUIP": "equip",
    "SET": "set",
    "TOEXTRA": "return to extra",
    "ATKCHANGE": "change ATK",
    "COUNTER": "counters",
}

ZONE_WHERE = {"mzone": "field", "szone": "field", "hand": "hand",
              "grave": "grave"}


def quick_actions(password, cards):
    """{where: [category names]} a card can use on the opponent's turn.

    Mirrors ygorl's own place derivation so that "from the field" here
    means the same thing it means in the interruption count, and reads
    the categories instead of collapsing them to negate / disrupt.
    """
    from ygorl.build.scripts import load_constants
    from ygorl.solver.blocking import script_facts

    code = cards.canonical(password)
    card = cards.get(code)
    if card is None:
        return {}
    facts = script_facts(code)
    if facts is None:
        return {}
    K = load_constants()
    quick = K["EFFECT_TYPE_QUICK_O"] | K["EFFECT_TYPE_QUICK_F"]
    monster = bool(card.type & K["TYPE_MONSTER"])
    settable = bool(card.type & K["TYPE_TRAP"]) or bool(
        card.type & K["TYPE_SPELL"] and card.type & K["TYPE_QUICKPLAY"])

    out = {}
    for eff in facts.effect_facts:
        names = [label for key, label in FOLLOW_UP.items()
                 if eff.categories & K.get(f"CATEGORY_{key}", 0)]
        if not names:
            continue
        places = []
        if eff.type & quick:
            rng = eff.range or (K["LOCATION_MZONE"] if monster
                                else K["LOCATION_SZONE"])
            if rng & (K["LOCATION_MZONE"] | K["LOCATION_SZONE"]
                      | K["LOCATION_FZONE"] | K["LOCATION_PZONE"]):
                places.append("field")
            if rng & K["LOCATION_HAND"]:
                places.append("hand")
            if rng & K["LOCATION_GRAVE"]:
                places.append("grave")
        elif eff.type & K["EFFECT_TYPE_ACTIVATE"] and settable:
            places.append("set")
        for w in places:
            for n in names:
                if n not in out.setdefault(w, []):
                    out[w].append(n)
    return out


def read_board(conn, record, line_index=0):
    """What the end board can actually DO, not just what is on it.

    Returns {"interruptions": [...], "follow_up": [...], "resources": {...}}.
    Every entry says where it acts from, because a negate on the field and
    a negate in the graveyard are not the same card to play around.
    """
    from ygorl.engine import constants as C
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import card_interruptions

    lines = record.get("lines") or []
    if not lines:
        return {"interruptions": [], "follow_up": [], "resources": {}}
    players = (lines[line_index].get("board") or {}).get("players") or []
    if not players:
        return {"interruptions": [], "follow_up": [], "resources": {}}
    me = players[0]
    cards = default_cards()

    seats = []            # (code, where, face_up)
    for c in me.get("mzone") or []:
        if c.get("position", 0) & C.POS_FACEUP:
            seats.append((c["code"], "field"))
    for c in me.get("szone") or []:
        seats.append((c["code"],
                      "field" if c.get("position", 0) & C.POS_FACEUP else "set"))
    for code in me.get("hand") or []:
        seats.append((code, "hand"))
    for code in me.get("grave") or []:
        seats.append((code, "grave"))

    interruptions, follow_up, seen_follow = [], [], set()
    for code, where in seats:
        hit = next((i for i in card_interruptions(code, cards)
                    if i.where == where), None)
        if hit is not None:
            interruptions.append({
                "name": _name(conn, code), "code": code, "where": where,
                "kind": "negate" if hit.negate else "disrupt"})
        acts = quick_actions(code, cards).get(where) or []
        if acts and (code, where) not in seen_follow:
            seen_follow.add((code, where))
            follow_up.append({"name": _name(conn, code), "code": code,
                              "where": where, "actions": acts,
                              "also_interrupts": hit is not None})

    order = {"field": 0, "set": 1, "hand": 2, "grave": 3}
    interruptions.sort(key=lambda r: (order.get(r["where"], 9),
                                      r["kind"] != "negate", r["name"]))
    follow_up.sort(key=lambda r: (order.get(r["where"], 9), r["name"]))
    return {
        "interruptions": interruptions,
        "follow_up": follow_up,
        "resources": {
            "hand": len(me.get("hand") or []),
            "grave": len(me.get("grave") or []),
            "banished": len(me.get("banished") or []),
            "deck": me.get("deck_count"),
            "extra": me.get("extra_count"),
        },
    }


def _rank_candidates(conn, job, base, rec, traps, say):
    """Fire every hand trap at every verified line and rank by exposure."""
    from dataclasses import replace as dreplace


    # lines x traps independent solver runs -- 21 of them for three
    # lines and seven traps, which is the bulk of the work in this mode.
    # Fired together for the same reason the plain trap loop is.
    import multiprocessing

    items = sorted(traps.items(), key=lambda kv: kv[1])
    n_lines = len(rec.get("lines") or [])
    workers = max(1, min(n_lines * len(items), engine.DEFAULT_WORKERS))
    per_threads = max(1, engine.DEFAULT_WORKERS // workers)
    say(f"testing {n_lines} lines x {len(items)} hand traps, "
        f"{workers} at a time")

    tasks = []
    for k in range(n_lines):
        only = dreplace(base, lines=[base.lines[k]])
        sub = dreplace(job, threads=per_threads,
                       scratch=Path(job.scratch) / f"c{k}")
        for code, _ in items:
            tasks.append(((sub, only, code, None), k))

    results = {}
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(workers) as pool:
        done = 0
        for (code, j), k in zip(
                pool.imap(_fire_one, [t[0] for t in tasks]),
                [t[1] for t in tasks]):
            meta = j.get("solver") or {}
            results.setdefault(k, []).append(
                {"code": code, "name": dict(items)[code],
                 "windows": meta.get("windows"),
                 "converted": meta.get("converted")})
            done += 1
            if done % len(items) == 0:
                say(f"  {done // len(items)} of {n_lines} lines done")

    rows = []
    for k in range(n_lines):
        per = sorted(results.get(k, []), key=lambda r: r["name"])
        lost, offered = exposure(per)
        rows.append({
            "line": k, "lost": lost, "offered": offered, "traps": per,
            "steps": steps(conn, {**rec, "lines": [rec["lines"][k]]}),
            "board": _board(conn, {**rec, "lines": [rec["lines"][k]]}),
            "reading": read_board(conn, {**rec, "lines": [rec["lines"][k]]}),
        })
        say(f"  line {k + 1}: {lost} of {offered} windows unanswered")
    # Fewest unanswered windows first; then fewest chances offered; then
    # the strongest board.
    rows.sort(key=lambda r: (r["lost"], r["offered"],
                             -len(r["reading"]["interruptions"])))
    return rows


def _fire_one(args):
    """One hand trap against one line. Runs in its own process.

    `spare` is a card the rebuilt board is allowed to be missing. ygorl's
    SolveRequest has no field for it and its fire branch ignores
    `targets` entirely, so the flag is injected around the call -- in the
    CHILD, because that is where the request is built.
    """
    from ygorl.solver import batch as BT

    job, base, code, spare = args
    if not spare:
        return code, BT.solve_fire(job, base, code).to_json()

    orig = BT.SolveRequest

    def with_spare(**kw):
        if kw.get("fire") is not None:
            kw["extra_args"] = tuple(kw.get("extra_args") or ()) + (
                "--fire-spare", str(spare))
        return orig(**kw)

    BT.SolveRequest = with_spare
    try:
        return code, BT.solve_fire(job, base, code).to_json()
    finally:
        BT.SolveRequest = orig


def _fire_all(job, base, traps, threads, say, spare=None):
    """Fire every trap, several at once, yielding (code, name, json).

    One solver with N threads does not use N cores -- measured at 137% of
    one on a long line, where a second deck reached 387%. N independent
    solvers do, which is exactly how the batch path fills this machine.
    So the cores are split across traps rather than inside one search.

    The pool uses "spawn", like upstream's, because the engine is a C++
    library with global state that must not be forked. A consequence
    worth knowing: a spawned child re-imports ``__main__``, so any script
    that calls ``analyse`` from module level and has no
    ``if __name__ == "__main__":`` guard re-runs its whole self in every
    worker. That is not theoretical -- the first run of this code solved
    the hand seven times over before anyone noticed, because the waste
    looks exactly like the work.
    """
    import multiprocessing
    from dataclasses import replace as dreplace

    from ygorl.solver.batch import solve_fire

    items = sorted(traps.items(), key=lambda kv: kv[1])
    if len(items) <= 1:
        for code, name in items:
            say(f"firing {name} ...")
            yield code, name, _fire_one((job, base, code, spare))[1]
        return

    workers = max(1, min(len(items), engine.DEFAULT_WORKERS))
    per = max(1, engine.DEFAULT_WORKERS // workers)
    say(f"firing {len(items)} hand traps, {workers} at a time "
        f"({per} thread(s) each)")
    jobs_ = [(dreplace(job, threads=per,
                       scratch=Path(job.scratch)), base, code, spare)
             for code, _ in items]
    names = dict(items)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(workers) as pool:
        for code, j in pool.imap_unordered(_fire_one, jobs_):
            say(f"  {names[code]}: done")
            yield code, names[code], j
