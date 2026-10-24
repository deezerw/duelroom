#!/usr/bin/env python3
"""duelroom — Yu-Gi-Oh! deck laboratory. Interactive CLI.

Stage 1 commands only (meta ingestion and exact hand math). The engine-backed
commands arrive with Stage 2+.
"""

import json
import os
import shlex
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills import (breaking, card_db, carddb, config, db, drill, engine,
                    gate, gauntlet, handlab, images, importer,
                    meta_scrape as ms, optimizer, report, scenarios,
                    stats, sweep)
from skills.deck import Deck

ROOT = Path(__file__).resolve().parent
DECKS = ROOT / "assets" / "decks"

BANNER = """\
duelroom — Yu-Gi-Oh! deck laboratory

  cards                     refresh the card mirror from YGOPRODeck
  images [scope] [limit]    cache card art locally (small-meta, small-all, full-all)
  meta [days] [n]           scrape recent tournament lists (default 120 days, 500)
  archetypes [n]            what is being played, by tournament presence
  flex <archetype> [side]   core / flex / fringe breakdown
  hand <deck.ydk>           exact opening-hand math
  pilot <deck> <cards>      solve ONE hand (or `random`): board, steps, weak points
  check <deck.ydk>          legality and banlist check
  decks                     list .ydk files in assets/decks/
  import <src> [name]       add a deck: .ydk path, ydke:// URI, or ygoprodeck URL
  mydeck [name]             show or set the deck commands default to
  ydke <deck>               export a deck as a ydke:// URI to paste elsewhere
  report [deck] [archetype] everything known; archetype auto-detected
  drill [deck] [n]          export best lines as EDOPro replays to practise
  export <archetype> [name] write a scraped list out as a .ydk
  doctor                    run every self-check: engine, data, guards
  setup                     build the merged engine card database
  engine                    engine setup status and pinned versions
  solve <deck> [hands] [n]  turn-1 line search; n draws per goal (1 = old path)
  ladder <deck> [hands]     calibrate the solver budget (run this first)
  gate <deck> [hands] [ms]  validation gate: solve rate + every brick hand
  boards [load]             meta end-board library (what you must break)
  threats                   interruption counts + common pieces per archetype
  snapshot [label]          freeze today's inclusion rates for later comparison
  drift <archetype> [a] [b] what's rising or falling between two snapshots
  targets                   derive solver targets from the board library
  matrix                    handtrap matrix: which handtraps to main deck
  locals [auto [rogue%]]    your locals.toml weights; `auto` fills from the scrape
  optimize <deck> <arch>    hill-climb the flex slots (slow: hours)
  second <theirs> <ours>    going second into their real board
  gauntlet <deck> <hand>    rank this hand's end boards by what they stop
  rescore [deck] [w]        re-rank stored optimiser trials, no solver time
  floodgates <deck>         does the deck function under common floodgates
  help / quit
"""


def _since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def _busy_solvers():
    """How many solver processes are already running.

    Starting a second sweep while one is in flight oversubscribes the
    cores and slows both; the engine already takes 6 of 8. Worth saying
    before a command silently joins the queue.
    """
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-c", "-x", "combosolver"],
                           capture_output=True, text=True, timeout=5)
        return int((r.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _warn_if_busy(what):
    """Print a warning if the machine is already saturated. Never blocks."""
    n = _busy_solvers()
    if n:
        print(f"  ! {n} solver process(es) already running — {what} will compete")
        print(f"    for cores and both will be slower. Check with `doctor`.")
    return n


def _latest_matrix():
    """Newest handtrap matrix file, whatever hand count it used."""
    cands = sorted((ROOT / "results").glob("handtrap_matrix*.jsonl"),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def _resolve_deck(arg, conn=None):
    """Find a deck by path or name, falling back to the active deck.

    Passing nothing is the common case once `mydeck` is set, so every
    command that takes a deck accepts being called bare.
    """
    if not arg:
        if conn is None:
            return None
        name = db.get_meta(conn, "active_deck")
        if not name:
            return None
        cand = DECKS / f"{name}.ydk"
        return cand if cand.exists() else None
    p = Path(arg)
    if p.exists():
        return p
    for cand in (DECKS / arg, DECKS / f"{arg}.ydk"):
        if cand.exists():
            return cand
    return None


# --- commands -------------------------------------------------------------

def cmd_cards(conn, args):
    print(card_db.sync(conn, force=bool(args and args[0] == "force")))


def cmd_pilot(conn, args):
    """Solve one chosen opening hand and show where it breaks."""
    if len(args) < 2:
        print('usage: pilot <deck> "<card>, <card>, ..."  [solve_s] [fire_s]')
        print('       names may be fragments: pilot my-despia "nadir, aluber"')
        print('       pilot my-despia random        deal one')
        print('       5th arg picks the traps: pilot my-despia random 60 30 "ash, imperm"')
        print('       6th asks for N candidate lines, ranked by what they play around')
        print('       pilot my-despia random:1234   deal the same one again')
        return
    name, cards = args[0], args[1]
    path = DECKS / f"{name}.ydk" if not name.endswith(".ydk") else Path(name)
    if not path.exists():
        print(f"no deck at {path}")
        return
    d = Deck.from_ydk(path)
    d.canonicalise(conn)
    try:
        chosen = handlab.parse_hand(conn, d, cards)
    except ValueError as e:
        print(f"could not read the hand: {e}")
        return
    solve_ms = int(float(args[2]) * 1000) if len(args) > 2 else engine.SOLVE_MS
    fire_ms = int(float(args[3]) * 1000) if len(args) > 3 else 30_000
    try:
        traps = handlab.parse_traps(args[4] if len(args) > 4 else None)
    except ValueError as e:
        print(e)
        return

    candidates = int(args[5]) if len(args) > 5 else 1
    threads = int(args[6]) if len(args) > 6 else None
    r = handlab.analyse(conn, path, chosen, solve_ms=solve_ms, fire_ms=fire_ms,
                        handtraps=traps, candidates=candidates, threads=threads,
                        log=lambda m: print(f"  {m}", flush=True))
    out = handlab.save(r)
    _print_pilot(r)
    _print_candidates(r)
    print(f"\nsaved to {out.relative_to(ROOT)}")


def _print_pilot(r):
    print(f"\n{r['deck']} — {', '.join(r['hand_names'])}")
    if r["filled"]:
        print(f"  (you chose {len(r['chosen'])}; the rest were dealt from the "
              f"deck and are NOT your pick)")
    if r["status"] != "solved":
        print(f"\nno line found ({r['status']}). "
              f"That is {r['solve_ms']/1000:.0f}s of search, not a proof.")
        if r["error"]:
            print(f"  {r['error']}")
        return

    print(f"\nEND BOARD  ({r['interruptions']} interruption(s))")
    for b in r["board"]:
        print(f"  {b['zone']:10} {b['name']}{' (set)' if b['set'] else ''}")

    rd = r.get("reading") or {}
    if rd.get("interruptions"):
        print("\nWHAT IT STOPS")
        for x in rd["interruptions"]:
            print(f"  {x['kind']:8} from {x['where']:6} {x['name']}")
    if rd.get("follow_up"):
        print("\nWHAT IT CAN STILL DO on their turn")
        for x in rd["follow_up"]:
            also = " (also interrupts)" if x["also_interrupts"] else ""
            print(f"  from {x['where']:6} {', '.join(x['actions'])}"
                  f" — {x['name']}{also}")
    res = rd.get("resources") or {}
    if res:
        print(f"\n  left: {res.get('hand')} in hand, {res.get('grave')} in "
              f"grave, {res.get('banished')} banished, {res.get('deck')} deck, "
              f"{res.get('extra')} extra")

    print("\nTHE LINE")
    for s in r["steps"]:
        print(f"  {s['n']:2}. {s['verb']:16} {s['name']}")
        for d in s.get("did") or []:
            print(f"        {d['what']:14} {d['name']}")

    print("\nWHERE IT BREAKS")
    print("  A window the solver could not convert is a search that found no "
          "recovery")
    print(f"  in {r['fire_ms']/1000:.0f}s. It is not a proof that the hand "
          "is dead there.")
    for h in r["handtraps"]:
        w, c = h.get("windows"), h.get("converted")
        if not w:
            print(f"\n  {h['name']}: no legal window in this line")
            continue
        bits = f"{c if c is not None else '?'} recovered"
        if h.get("partial"):
            bits += (f" ({h.get('full', 0)} rebuilding the whole board, "
                     f"{h['partial']} only by giving up "
                     f"{r.get('spare_name') or 'a piece'})")
        print(f"\n  {h['name']}: live at {w} point(s), {bits}")
        if h["blind_spot"]:
            print(f"      note: {h['blind_spot']}")
        if h["parse_error"]:
            print(f"      per-window detail unavailable: {h['parse_error']}")
        if h.get("recovered_board") and (h.get("converted") or 0):
            got = ", ".join(b["name"] for b in h["recovered_board"]
                            if b["zone"] != "hand") or "nothing on board"
            print(f"      when it lands and you play on, you still reach: {got}")
        for d in h["detail"]:
            mark = "survives" if d["survived"] else "NO RECOVERY FOUND"
            print(f"      on {d['over'] or '?'}  ->  {mark}")
            if d["missing"] and not d["survived"]:
                print(f"         would have needed: {', '.join(d['missing'])}")
            if d["answers"]:
                print(f"         you can answer with: {', '.join(d['answers'])}")


def _print_candidates(r):
    rows = r.get("candidates") or []
    if len(rows) < 2:
        return
    print("\nPLAYING AROUND THE MOST")
    print("  Every verified line, fired at with the same hand traps. The")
    print("  strongest board and the safest line are not always the same one.")
    for i, c in enumerate(rows):
        tag = "  <- fewest openings" if i == 0 else ""
        board = ", ".join(x["name"] for x in c["reading"]["interruptions"]) \
            or "no interruptions"
        print(f"\n  line {c['line'] + 1}: {c['lost']} of {c['offered']} "
              f"windows unanswered{tag}")
        print(f"      board: {board}")
        for t in c["traps"]:
            w, k = t.get("windows") or 0, t.get("converted") or 0
            if w:
                print(f"      {t['name']:32} {w - k} of {w} unanswered")


def cmd_images(conn, args):
    """Cache card art locally. Resumable: re-run it after an interruption."""
    if args and args[0] == "status":
        for size, st in images.status(conn).items():
            print(f"  {size:6} {st['cached']:6}/{st['pool']} cached  "
                  f"{st['bytes'] / 1e6:7.1f} MB  "
                  f"{st['missing_upstream']} with no art upstream")
        return
    scope = args[0] if args else "small-meta"
    limit = int(args[1]) if len(args) > 1 else None
    r = images.fetch(conn, scope, limit, log=lambda m: print(m, flush=True))
    print(f"{r['fetched']} fetched, {r['absent']} absent upstream, "
          f"{r['failed']} left to retry (re-run to pick them up)")


def cmd_meta(conn, args):
    days = int(args[0]) if args else 120
    cap = int(args[1]) if len(args) > 1 else 500
    since = _since(days)
    print(f"scraping '{ms.DEFAULT_FORMAT}' since {since} (cap {cap}) ...")
    stored, skipped, tags = ms.scrape_recent(conn, since=since, max_decks=cap)
    print(f"stored {stored} new lists, skipped {skipped} duplicates")
    if tags:
        print("\ntop archetypes this sweep:")
        for a, n in tags.most_common(10):
            print(f"  {n:4}  {a}")
    print("\nrecomputing inclusion rates ...")
    done = 0
    for a, _ in tags.most_common(40):
        if ms.compute_inclusion(conn, a):
            done += 1
    print(f"inclusion computed for {done} archetypes")


def cmd_archetypes(conn, args):
    limit = int(args[0]) if args else 20
    rows = conn.execute(
        """SELECT t.tag, COUNT(*) AS n
             FROM deck_tags t JOIN decks d ON d.id = t.deck_id
         GROUP BY t.tag ORDER BY n DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        print("no decks stored yet — run `meta` first")
        return
    print(f"{'lists':>6}  archetype")
    for r in rows:
        print(f"{r['n']:>6}  {r['tag']}")


def cmd_flex(conn, args):
    if not args:
        # fall back to the active deck's archetype, the same way report
        # and optimize do — nobody importing a list knows its tag
        path = _resolve_deck(None, conn)
        if not path:
            print("usage: flex <archetype> [main|side|extra]")
            return
        deck = Deck.from_ydk(path)
        deck.canonicalise(conn)
        guesses = ms.identify_archetype(conn, deck)
        if not guesses or guesses[0][1] < 0.5:
            print(f"could not identify an archetype for {path.stem} — pass one")
            return
        args = [guesses[0][0]]
        print(f"({path.stem} identified as {args[0]})")
    section = args[-1] if args[-1] in ("main", "side", "extra") else "main"
    archetype = " ".join(args[:-1]) if args[-1] in ("main", "side", "extra") else " ".join(args)

    n = ms.compute_inclusion(conn, archetype)
    if not n:
        print(f"no stored lists tagged {archetype!r} — run `meta`, or check `archetypes`")
        return
    buckets = ms.classify(conn, archetype, section=section)
    print(f"\n{archetype} — {section} deck, {n} lists\n")
    for name, label in (("core", "CORE (>=90%, do not touch)"),
                        ("staple", "STAPLE (60-90%, standard but not automatic)"),
                        ("flex", "FLEX (20-60%, the search space)"),
                        ("fringe", "FRINGE (<20%, candidate pool)")):
        rows = buckets[name]
        print(f"  {label} — {len(rows)} cards")
        for r in rows:
            print(f"     {r['pct']*100:5.1f}%  {r['avg_copies']:4.1f}x  {r['name']}")
        print()


def cmd_hand(conn, args):
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: hand <deck.ydk>")
        return
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    d = Deck.from_ydk(path)
    unresolved = d.canonicalise(conn)
    size = len(d.main)
    print(f"\n{d.name}: {size} main / {len(d.extra)} extra / {len(d.side)} side")
    if unresolved:
        print(f"  warning: {len(unresolved)} passcodes not in the card mirror")

    print(f"\n  opening odds (going first, 5 cards / going second, 6)\n")
    print(f"  {'copies':>6}  {'first':>7}  {'second':>7}  card")
    for name, copies in d.named(conn, "main"):
        p5 = stats.open_chance(size, copies, 5) * 100
        p6 = stats.open_chance(size, copies, 6) * 100
        print(f"  {copies:>6}  {p5:6.1f}%  {p6:6.1f}%  {name}")

    print(f"\n  brick floor by starter count (P(zero starters), {size}-card deck)\n")
    print(f"  {'starters':>8}  {'first':>7}  {'second':>7}")
    for k in range(6, 19):
        b5 = stats.brick_chance(size, k, 5) * 100
        b6 = stats.brick_chance(size, k, 6) * 100
        print(f"  {k:>8}  {b5:6.1f}%  {b6:6.1f}%")
    print("\n  (a floor, not the real brick rate — a hand can hold a starter and")
    print("   still have no line. Stage 3 measures the real number.)")


def cmd_check(conn, args):
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: check <deck.ydk>")
        return
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    d = Deck.from_ydk(path)
    d.canonicalise(conn)
    problems = d.validate(conn)
    print(f"{d!r}")
    if not problems:
        print("  legal")
        return
    for p in problems:
        print(f"  ! {p}")


def _orphaned_workers():
    """Leaked pool workers: duelroom spawn children reparented to init.

    When a pool's parent dies -- a killed sweep, a crashed optimiser --
    its spawned children are reparented to PID 1 and never exit, because
    they sit blocked on a queue nobody will write to again. Found six of
    them on 2026-10-24 aged six days, 41 MB between them.

    Harmless to correctness and invisible without looking, but they
    accumulate, and stray Python processes make `_busy_solvers` and any
    CPU reading misleading. Returns [(pid, etime)].
    """
    import subprocess
    try:
        r = subprocess.run(
            ["ps", "-u", os.environ.get("USER", ""), "-o", "pid=,ppid=,etime=,args="],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etime, args = parts
        if ppid != "1" or "spawn_main" not in args:
            continue
        if "duelroom" not in args:
            continue
        out.append((pid, etime))
    return out


def cmd_doctor(conn, args):
    """Everything that can be verified without burning solver time.

    Five guards grew out of five different failure modes and none of
    them were wired together. This runs all of them plus the
    engine and data checks, and reports what is MISSING as loudly as
    what is broken -- an empty database passing every check is not a
    healthy install.
    """
    import subprocess
    ok = True

    print("\n  ENGINE")
    problems = engine.check()
    if problems:
        ok = False
        for p in problems:
            print(f"    ! {p}")
    else:
        v = engine.versions()
        print(f"    ready — ygorl {v['ygorl']}, core {v['ygopro_core']}, "
              f"scripts {v['card_scripts']}")
        print(f"    budget {engine.SOLVE_MS/1000:.0f}s/hand, "
              f"{engine.DEFAULT_WORKERS} workers")

    print("\n  DATA")
    counts = {
        "cards": "SELECT COUNT(*) n FROM cards",
        "scraped lists": "SELECT COUNT(*) n FROM decks",
        "end boards": "SELECT COUNT(*) n FROM boards",
        "solve trials": "SELECT COUNT(*) n FROM trials",
    }
    for label, q in counts.items():
        n = conn.execute(q).fetchone()["n"]
        flag = "" if n else "   <- empty, most commands will do nothing"
        if not n:
            ok = False
        print(f"    {n:>6}  {label}{flag}")
    decks = sorted((ROOT / "assets" / "decks").glob("*.ydk"))
    print(f"    {len(decks):>6}  deck files")
    active = db.get_meta(conn, "active_deck")
    print(f"            active deck: {active or '(none — `import` or `mydeck`)'}")

    print("\n  GUARDS")
    for label, cmd in (
        ("regression tests", [str(engine.ROOT / "venv/bin/pytest"), "tests/", "-q"]),
        ("invariants", [str(engine.PYTHON), "tools/check_invariants.py"]),
        ("field audit", [str(engine.PYTHON), "tools/audit_fields.py"]),
        ("doc drift", [str(engine.PYTHON), "tools/check_docs.py"]),
        # Added after two NameErrors in one session that no test could
        # see: a missing `import shutil` broke every sweep while its own
        # test passed, and the same mistake dropped `import contextlib`
        # an hour later. pyflakes finds both by name and line.
        ("undefined names", [str(engine.PYTHON), "tools/check_names.py"]),
    ):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
        tail = (r.stdout or r.stderr).strip().splitlines()
        summary = tail[-1] if tail else "(no output)"
        mark = "ok  " if r.returncode == 0 else "FAIL"
        if r.returncode != 0:
            ok = False
        print(f"    {mark}  {label:18} {summary[:60]}")

    orphans = _orphaned_workers()
    if orphans:
        ok = False
        print(f"\n  LEAKED PROCESSES")
        print(f"    ! {len(orphans)} orphaned pool worker(s) — a dead sweep or")
        print(f"      optimiser left children blocked on a queue nobody will")
        print(f"      write to. They never exit on their own.")
        for pid, etime in orphans[:6]:
            print(f"        pid {pid:>8}  age {etime}")
        if len(orphans) > 6:
            print(f"        ... and {len(orphans) - 6} more")
        print(f"      clear with: kill {' '.join(p for p, _ in orphans[:12])}")

    print("\n  WEAKEST DATA")
    mpath = _latest_matrix()
    if mpath:
        recs = [json.loads(l) for l in mpath.read_text().splitlines() if l.strip()]
        hands = min((r.get("summary", {}).get("hands") or 0) for r in recs) if recs else 0
        n_decks = len(decks) - 2  # snake_eye and imported user decks are not swept
        print(f"    handtrap matrix: {mpath.name}, {hands} hands/deck, "
              f"{len(recs)} deck(s)")
        if len(recs) < n_decks:
            print(f"      ! only {len(recs)} of ~{n_decks} decks — this file is "
                  f"PARTIAL (a sweep may still be running)")
        if hands and hands < 20:
            print("      ! under 20 hands — survival cells rest on a handful of windows")
    nb = conn.execute("SELECT COUNT(DISTINCT archetype) n FROM boards").fetchone()["n"]
    print(f"    board library covers {nb} archetypes")
    covered, missing = config.coverage([p.stem for p in decks])
    print(f"    locals.toml covers {covered*100:.0f}% of your stated meta")
    if missing:
        print(f"      no deck for: {', '.join(missing)}")

    print(f"\n  {'all checks passed' if ok else 'SOMETHING NEEDS ATTENTION (see ! above)'}\n")


def cmd_setup(conn, args):
    """Build the merged card database the engine needs.

    BabelCDB ships recent sets as separate release-*.cdb files that ygorl
    never loads, and a deck holding one of those cards segfaults the solver
    instead of erroring. Run this after any submodule bump.
    """
    print("building merged card database ...")
    path, added, total = carddb.build(force=bool(args and args[0] == "force"))
    for name in added:
        print(f"  merged {name}")
    print(f"  {total} cards -> {path}")
    print(f"  BabelCDB revision {carddb.source_revision()}")
    problems = engine.check()
    print("\n" + ("engine ready" if not problems else "still not ready:"))
    for p in problems:
        print(f"  ! {p}")
    print("\nNote: BabelCDB and CardScripts must be bumped together — cards")
    print("without their Lua scripts load fine and then do nothing.")


def cmd_engine(conn, args):
    problems = engine.check()
    if problems:
        print("engine NOT ready:")
        for p in problems:
            print(f"  ! {p}")
        print(f"\n  setup instructions are in {engine.__name__}'s docstring")
        return
    print(f"engine ready ({engine.DEFAULT_WORKERS} workers, "
          f"{engine.SOLVE_MS/1000:.0f}s solver budget per hand)")
    for k, v in engine.versions().items():
        print(f"  {k:14} {v}")


def cmd_solve(conn, args):
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: solve <deck.ydk> [hands]")
        return
    _warn_if_busy("this solve")
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    hands = int(args[1]) if len(args) > 1 else 8
    draws = int(args[2]) if len(args) > 2 else sweep.DRAWS
    # draws=1 is the old single-draw sweep, kept so the two can be compared
    # on the same hands. It is NOT the default: one draw of a randomized
    # search reports a floor as if it were a best (see skills/sweep.py).
    if draws <= 1:
        print(f"solving {hands} opening hands for {path.stem} — ONE draw per "
              f"goal (the old path; a floor, not a best)")
        summary = engine.solve_blocking(path, hands=hands)
        for row in engine.summarise(summary):
            print(f"\n  {row['deck']}")
            print(f"    solve rate        {row['solved']}/{row['hands']} "
                  f"({row['solve_rate']*100:.0f}%)")
            print(f"    mean interrupts   {row['mean_interruptions']:.2f}"
                  f"  (per SOLVED hand)")
            print(f"    per hand          {row['s_per_hand']:.1f}s")
            print(f"    distribution      {row['interruptions']}")
        return
    print(f"solving {hands} opening hands for {path.stem} "
          f"({engine.DEFAULT_WORKERS} workers, "
          f"{engine.SOLVE_MS/1000:.0f}s per draw, {draws} draws per goal)")
    print("this is CPU-bound and not fast — see skills/sweep.py on budgets\n")
    out = ROOT / "results" / f"sweep_{path.stem}.jsonl"
    table = sweep.sweep(path, hands=hands, draws=draws, out=out,
                        summary=ROOT / "results" / f"sweep_{path.stem}.json",
                        log=print)
    for name, row in sorted((table.get("decks") or {}).items()):
        print(f"\n  {name}")
        print(f"    solve rate        {row['solved']}/{row['hands']} "
              f"({row['solve_rate']*100:.0f}%)")
        # Solved-only: `summarize` appends to `interruptions` after its
        # `if r["status"] != "solved": continue`. Printed bare it reads as
        # per-hand, which for a deck that bricks a quarter of the time is
        # a different and lower number -- the mistake that produced a
        # "1.0 interruptions" claim for branded that stood all session.
        print(f"    mean interrupts   {row['mean_interruptions']:.2f}"
              f"  (per SOLVED hand, not per hand)")
        print(f"    per hand          {row['wall_per_hand_s']:.1f}s of solver")
        print(f"    distribution      {row['interruptions']}")
        print(f"    top pieces        "
              + ", ".join(f"{n} x{c}" for n, c in row["top_pieces"][:4]))


def cmd_ladder(conn, args):
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: ladder <deck.ydk> [hands]")
        return
    _warn_if_busy("this ladder")
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    hands = int(args[1]) if len(args) > 1 else 12
    print(f"budget ladder for {path.stem}, {hands} hands at "
          f"{[b//1000 for b in gate.LADDER]} seconds")
    print("looking for where the brick count stops moving — this takes a while\n")
    rows, converged = gate.budget_ladder(path, hands=hands)
    print(f"  {'budget':>7}  {'bricks':>8}  {'solved':>7}  {'false':>6}  {'solver':>8}")
    for r in rows:
        mark = " <- converged" if r["solve_ms"] == converged else ""
        print(f"  {r['solve_ms']//1000:>6}s  {r['bricks']:>3}/{r['hands']:<4} "
              f"{r['solved']:>7}  {r['false_bricks']:>6}  {r['solver_s']:>7.0f}s{mark}")
    if converged:
        print(f"\n  use --solve-ms {converged} for this deck "
              f"({converged//1000}s); anything lower measures the search, not the deck")


def cmd_gate(conn, args):
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: gate <deck.ydk> [hands] [solve_ms]")
        return
    _warn_if_busy("this gate")
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    hands = int(args[1]) if len(args) > 1 else 12
    ms = int(args[2]) if len(args) > 2 else engine.SOLVE_MS
    target = scenarios.ensure_target(path)
    if not target:
        print(f"could not establish a target for {path.stem} — nothing to gate")
        return
    spec = scenarios.load_targets().get(path.stem) or {}
    weak = scenarios.trivial_targets(
        {path.stem: {**spec, "_extra": scenarios.target_is_extra(conn, path, target)}})
    if weak:
        print(f"  ! WARNING: {weak[0][1]}")
        print("    Numbers below describe reaching THAT target, which this deck")
        print("    may simply never draw. A low solve rate here is not evidence")
        print("    the deck is bad. Treat it as unmeasured.\n")
    print(f"gate: {path.stem}, {hands} hands, {ms/1000:.0f}s budget\n")
    rec = gate.run_funnel(path, hands=hands, solve_ms=ms,
                          targets=scenarios.merged_targets_file())
    rep = gate.gate_report(rec, conn)

    lo, hi = rep["solve_rate_ci"]
    print(f"  solved      {rep['solved']}/{rep['hands']} "
          f"({rep['solve_rate']*100:.0f}%, 95% CI {lo*100:.0f}-{hi*100:.0f}%)")
    print(f"  solver time {rep['solver_s']:.0f}s")
    ca = rep["combo_actions"]
    if ca and ca.get("min") is not None:
        print(f"  line length min {ca.get('min')}, median {ca.get('median')}, "
              f"max {ca.get('max')}")
    for code, surv in (rep["survival_given_line"] or {}).items():
        row = card_db.resolve(conn, int(code))
        name = row["name"] if row else code
        # None when no hand solved: there is no "survival given a line"
        # if there were no lines. Saying so beats crashing or printing 0%.
        shown = "n/a (no hand solved)" if surv is None else f"{surv*100:.0f}% of solved hands"
        print(f"  survives {name}: {shown}")

    print(f"\n  hand classes: {dict(gate.classify_hands(rec))}")

    print("\n  BRICK HANDS — judge these by eye. A brick holding an obvious")
    print("  starter means the solver failed, whatever the rate says.")
    for b in rep["brick_hands"]:
        print(f"\n    hand {b['index']} ({b['solver_s']:.0f}s)")
        for c in b["cards"]:
            print(f"      {c}")

    dead = [r for r in gate.card_liveliness(rec, conn) if r["seen"] >= 2][:6]
    if dead:
        print("\n  least live cards (seen >=2 hands):")
        for r in dead:
            print(f"    {r['solve_rate']*100:3.0f}% solved when held  "
                  f"({r['in_solved']}/{r['seen']})  {r['name']}")

    run_id = gate.load_funnel(conn, rec)
    print(f"\n  stored as run {run_id} in data/duelroom.db")


def cmd_boards(conn, args):
    if args and args[0] == "load":
        path = ROOT / "results" / "boards_summary.json"
        if not path.exists():
            print(f"no {path} — run solve_blocking over the meta decks first")
            return
        n = gate.load_boards(conn, path)
        print(f"loaded {n} distinct boards")

    lib = gate.board_library(conn)
    if not lib:
        print("board library empty — run `boards load`")
        return
    for archetype, boards in lib.items():
        total = sum(b["frequency"] for b in boards)
        mean = sum(b["negates"] * b["frequency"] for b in boards) / (total or 1)
        # Over the SOLVED hands only -- the library stores end boards, and
        # a hand with no line contributes none. Printing a bare mean let
        # it be read as "per hand", which is a different and lower number:
        # branded is 1.6 over its 31 solved hands and 1.2 over all 40.
        print(f"\n  {archetype}  (mean {mean:.1f} interruptions per SOLVED "
              f"hand; hands with no line are not in this library)")
        for b in boards:
            print(f"    {b['frequency']*100:4.0f}%  {b['negates']} interrupts")
            for i in b["interruptions"]:
                print(f"            {i}")


def cmd_threats(conn, args):
    prof = gate.threat_profile(conn)
    if not prof:
        print("board library empty — run `boards load`")
        return
    mine = db.get_meta(conn, "active_deck")
    counts = sorted({n for d in prof.values() for n in d})
    print("  share of end boards by interruption count")
    print("  These are decks you will SIT ACROSS FROM: a higher mean is a")
    print("  harder board for you to break. The opposite reading applies to")
    print("  any row marked (yours).\n")
    print("  " + "archetype".ljust(28) + "".join(f"{n:>7}" for n in counts) + "    mean")
    for arch, dist in sorted(prof.items(),
                             key=lambda kv: -sum(k*v for k, v in kv[1].items())):
        mean = sum(k * v for k, v in dist.items())
        cells = "".join(f"{dist.get(n,0)*100:6.0f}%" for n in counts)
        tag = "  (yours)" if mine and config.slug(arch) == config.slug(mine) else ""
        print(f"  {arch.ljust(28)}{cells}  {mean:6.1f}{tag}")

    recs_path = _latest_matrix()
    if recs_path and recs_path.exists():
        recs = [json.loads(l) for l in recs_path.read_text().splitlines() if l.strip()]
        rw = scenarios.response_windows(recs)
        if rw:
            print("\n  interruption surface: times the opponent gets to respond\n")
            print("  " + "archetype".ljust(28) + "  windows/line   max")
            for deck, v in sorted(rw.items(), key=lambda kv: -kv[1]["mean"]):
                print(f"  {deck.ljust(28)}{v['mean']:13.1f}{v['max']:6}")
            print("\n  (Not special summons — Maxx \"C\" has no activation")
            print("   condition, so its window count tracks line length, r=0.94.)")

    bpath = ROOT / "results" / "boards.jsonl"
    if bpath.exists():
        cache = ROOT / "results" / "special_summons.json"
        if cache.exists():
            ss = json.loads(cache.read_text())
            print("\n  special summons per line (replayed, not inferred)\n")
            print("  " + "archetype".ljust(28) + "  SS/line   max   Nibiru live")
            for deck, v in sorted(ss.items(), key=lambda kv: -kv[1]["mean"]):
                print(f"  {deck.ljust(28)}{v['mean']:8.1f}{v['max']:6}"
                      f"{v['nibiru_rate']*100:12.0f}%")
            print("\n  (Nibiru live = share of lines passing 5 summons. This")
            print("   explains its window counts: r=0.94 against them.)")
            cost = scenarios.maxx_c_cost(ss)
            print("\n  cards surrendered to Maxx \"C\" if they combo through\n")
            print("  " + "archetype".ljust(28) + "  cards   worst")
            for deck, v in sorted(cost.items(), key=lambda kv: -kv[1]["cards"]):
                print(f"  {deck.ljust(28)}{v['cards']:7.1f}{v['worst']:8}")
            print("\n  (The survival table cannot see this — those lines mostly")
            print("   complete, which reads as 'fine'. Completing a line while")
            print("   handing over eight cards is not beating Maxx \"C\".)")

    print("\n  visible board vs cards held in hand\n")
    print("  " + "archetype".ljust(28) + "  on field   in hand")
    bh = gate.board_vs_hand(conn)
    for arch, v in sorted(bh.items(), key=lambda kv: -kv[1]["field"]):
        print(f"  {arch.ljust(28)}{v['field']:9.1f}{v['hand']:10.1f}")
    print("\n  (going second you can play around a visible negate; you cannot")
    print("   play around a handtrap you have not seen. Break rates weight the")
    print("   first column, survival weights the second.)")

    print("\n  most common interruptions (share of that deck's boards)\n")
    for arch, pieces in sorted(gate.common_pieces(conn).items()):
        print(f"  {arch}")
        for name, share in pieces:
            print(f"      {share*100:4.0f}%  {name}")


def cmd_snapshot(conn, args):
    label, n = ms.snapshot_inclusion(conn, args[0] if args else None)
    print(f"snapshot {label!r}: {n} inclusion rows frozen")
    print("\n  stored snapshots:")
    for lab, rows, when in ms.snapshots(conn):
        print(f"    {lab:24} {rows:6} rows   {when[:10]}")
    print("\n  re-scrape with `meta`, then `snapshot` again, then `drift` to")
    print("  see which cards are rising. One scrape cannot show drift.")


def cmd_drift(conn, args):
    snaps = ms.snapshots(conn)
    if len(snaps) < 2 and len(args) < 3:
        print("need two snapshots to compare — you have:")
        for lab, rows, when in snaps:
            print(f"    {lab} ({when[:10]})")
        print("\n  drift is a question about TIME. Re-scrape with `meta` on a")
        print("  later day, `snapshot` again, then come back. Slicing one scrape")
        print("  by date does not work: the stored lists cluster around when they")
        print("  were last updated, leaving a handful in any recent window.")
        return
    if not args:
        print("usage: drift <archetype> [old_label] [new_label]")
        return
    archetype = args[0]
    old = args[1] if len(args) > 1 else snaps[-2][0]
    new = args[2] if len(args) > 2 else snaps[-1][0]
    # Refuse before printing, not after. Drift across ONE scrape is zero by
    # construction, and a table of zeros reads as "the meta did not move"
    # when it means "no new lists were gathered". Both existing snapshots
    # are 5599 rows from the same scrape, 3.5 hours apart.
    shared = ms.same_scrape(conn, old, new)
    if shared:
        print(f"  !! NO DRIFT TO REPORT. {old} and {new} were both taken from")
        print(f"     the SAME scrape, so any difference between them is zero by")
        print(f"     construction — it is not evidence the meta is stable.")
        print(f"     Re-scrape with `meta` on a later day, `snapshot` again, and")
        print(f"     compare that against one of these.")
        return
    rows, note = ms.drift(conn, archetype, old, new)
    print(f"{archetype}: {old} -> {new}")
    if shared is None:
        print("  ! one of these snapshots predates the scrape fingerprint, so")
        print("    whether they share a scrape could not be checked.")
    if note:
        print(f"  ! {note}")
    if not rows:
        print("  no change")
        return
    print(f"\n  {'was':>6} {'now':>6} {'delta':>7}  card")
    for r in rows[:15]:
        row = card_db.resolve(conn, r["card_id"])
        name = row["name"] if row else r["card_id"]
        print(f"  {r['was']*100:5.0f}% {r['now']*100:5.0f}% "
              f"{r['delta']*100:+6.0f}%  {name}")


def cmd_targets(conn, args):
    src = ROOT / "results" / "boards.jsonl"
    if not src.exists():
        print(f"no {src} — build the board library first")
        return
    out = ROOT / "assets" / "derived_targets.json"
    t = scenarios.write_targets(src, out)
    print(f"derived targets for {len(t)} decks -> {out}\n")
    for deck, named in scenarios.describe_targets(conn, t):
        print(f"  {deck:28} {', '.join(named)}")
    weak = scenarios.trivial_targets(scenarios.derive_targets(src))
    if weak:
        print("\n  weak targets — these decks build little, so their solve rate")
        print("  and handtrap survival mean less than the numbers suggest:")
        for deck, reason in weak:
            print(f"    {deck}: {reason}")


def cmd_locals(conn, args):
    if args and args[0] == "auto":
        rogue = float(args[1]) / 100 if len(args) > 1 else config.DEFAULT_ROGUE
        if not 0 <= rogue < 1:
            print("rogue share must be between 0 and 100 (percent)")
            return
        written = config.autofill(conn, rogue=rogue)
        if not written:
            print("no scraped lists to derive from — run `meta` first")
            return
        print(f"wrote locals.toml from the scrape, {rogue:.0%} reserved for rogue\n")

    w = config.weights()
    if not w:
        print("locals.toml has no weights — edit it with your room's spread")
        return
    print("  weight  archetype")
    for k, v in sorted(w.items(), key=lambda kv: -kv[1]):
        print(f"  {v*100:5.1f}%  {k}")
    decks = [p.stem for p in (ROOT / "assets" / "decks").glob("*.ydk")]
    covered, missing = config.coverage(decks)
    print(f"\n  measured decks cover {covered*100:.0f}% of your stated meta")
    if missing:
        print(f"  no deck exported for: {', '.join(missing)}")


def cmd_matrix(conn, args):
    # newest by hand count: handtrap_matrix_40h beats _12h beats the
    # unsuffixed legacy file
    path = _latest_matrix()
    if not path:
        print("no handtrap matrix yet — run scenarios.run_matrix first")
        return
    _n = len([l for l in path.read_text().splitlines() if l.strip()])
    # Compare against the decks that actually exist, not a magic 5.
    # `doctor` already calls this same 7-deck file PARTIAL ("only 7 of ~10")
    # while this view said nothing, because 7 clears `_n < 5`. Two places
    # judging the same file by different standards is how a caveat goes
    # missing from the view that drives a decision -- and this one decides
    # which handtraps to main deck.
    _expected = max(0, len(list(DECKS.glob("*.ydk"))) - 2)
    print(f"  from {path.name} ({_n} deck(s))")
    if _expected and _n < _expected:
        print(f"  ! PARTIAL: {_n} of ~{_expected} decks "
              f"({_n / _expected:.0%} of the field). Rows below cover only")
        print(f"    the decks finished so far, and every ranking here is a")
        print(f"    ranking over those {_n}.")
        missing = sorted(p.stem for p in DECKS.glob("*.ydk"))
        print(f"    deck files present: {', '.join(missing[:8])}"
              + (" ..." if len(missing) > 8 else ""))
    elif _n < 5:
        print("  ! this file looks PARTIAL — a sweep may still be writing it.")
        print("    Rows below cover only the decks finished so far.")
    print()
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    m = scenarios.handtrap_matrix(recs)
    if not m:
        print("no usable records")
        return

    codes = sorted(scenarios.HANDTRAPS)
    short = {c: scenarios.HANDTRAPS[c].split(" &")[0].split(",")[0][:9] for c in codes}

    print("  SURVIVAL — of the windows where the handtrap could be used,")
    print("  how often the line still completed. Lower is better for you.")
    print("  '-' means it never had a legal window in any solved hand.\n")
    mine = db.get_meta(conn, "active_deck")
    def _tag(deck):
        return "  (yours: high is GOOD)" if mine and config.slug(deck) == config.slug(mine) else ""
    print("  " + "deck".ljust(26) + "".join(f"{short[c]:>10}" for c in codes))
    for deck, data in sorted(m.items()):
        cells = ""
        for c in codes:
            cell = data["cells"].get(c)
            v = cell["survival"] if cell else None
            cells += "         -" if v is None else f"{v*100:9.0f}%"
        print(f"  {deck.ljust(26)}{cells}{_tag(deck)}")

    print("\n  WINDOW RATE — how often it was live at all.")
    print("  A handtrap with no window is useless here for a different")
    print("  reason than one that gets played through.\n")
    print("  " + "deck".ljust(26) + "".join(f"{short[c]:>10}" for c in codes))
    for deck, data in sorted(m.items()):
        cells = ""
        for c in codes:
            cell = data["cells"].get(c)
            cells += "         -" if not cell else f"{cell['window_rate']*100:9.0f}%"
        print(f"  {deck.ljust(26)}{cells}")

    w = config.weights()
    ranking = scenarios.weighted_handtrap_ranking(m, w)
    covered, missing = config.coverage(m.keys())
    if ranking:
        print("\n  WHICH HANDTRAPS TO MAIN — weighted by locals.toml")
        print(f"  (covers {covered*100:.0f}% of your stated meta)\n")
        print("    stop%   live%   handtrap")
        for code, name, score, cov in ranking:
            print(f"    {score*100:5.1f}%  {cov*100:5.1f}%   {name}")
        print("\n    stop% = weighted chance it both has a window and stops the line")
        print("    live% = weighted chance it has a window at all")
        if missing:
            print(f"\n    no measured deck for: {', '.join(missing)}")

    print("\n  READ WITH CARE:")
    print("  * Maxx \"C\" and Droll score 0% stop because this metric asks only")
    print("    whether the line still reaches its target. Maxx \"C\" never stops")
    print("    a combo — it punishes one with cards. The metric is blind to")
    print("    resource punishment, so a 0% here is not a verdict on the card.")
    print("  * A low live% with a high stop-when-live (Nibiru) is a different")
    print("    card from a high live% with a low stop rate (Imperm). The first")
    print("    is a swingy answer, the second a consistent speed bump.")
    print("  * branded's row is thin because its derived target is just setting")
    print("    a backrow card — there is little in that line to interrupt.")
    print("  * 12 hands per deck. Several cells rest on 1-3 tested windows.")


def cmd_optimize(conn, args):
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print("usage: optimize [deck] [archetype] [hands] [rounds] [slots]"
              "   (or set one with `mydeck`)")
        print("  slots = cut slots tried per candidate add (default 1);"
              " widening multiplies runtime")
        return
    _warn_if_busy("this search")
    deck = Deck.from_ydk(path)
    deck.canonicalise(conn)

    rest = list(args[1:]) if args else []
    archetype = rest[0] if rest and not rest[0].isdigit() else None
    nums = [a for a in rest if a.isdigit()]
    hands = int(nums[0]) if nums else 8
    rounds = int(nums[1]) if len(nums) > 1 else 2
    # How many cut slots to try per candidate add. 1 is the historical
    # behaviour and the default, because widening multiplies trials and
    # each trial is `hands` full funnel runs.
    slots = int(nums[2]) if len(nums) > 2 else 1

    if archetype is None:
        guesses = ms.identify_archetype(conn, deck)
        if not guesses or guesses[0][1] < 0.5:
            print(f"could not identify an archetype for {path.stem} — pass one")
            return
        archetype = guesses[0][0]
        print(f"identified as {archetype} "
              f"({guesses[0][1]*100:.0f}% of its {guesses[0][2]}-card core)")
    # Announce the pool `hill_climb` will ACTUALLY use. It passes
    # pool_limit=12 by default, while candidate_pool's own default is 30 --
    # so this printed "26 candidates ... 78 trials" and then ran 36
    # (1 base + 12 x 3 slots = 37 records). Overstating the work by more
    # than 2x is the same defect as any other unqualified number.
    pool_limit = 12
    pool = optimizer.candidate_pool(conn, archetype, deck, limit=pool_limit)
    outs = optimizer.removable(conn, deck, archetype)
    if not pool:
        print(f"no flex/fringe pool for {archetype!r} — run `meta` and check `archetypes`")
        return
    cuts = [optimizer.card_name(conn, o[0]) for o in outs[:slots]] or ["?"]
    print(f"{deck.name}: {len(outs)} non-core slots, {len(pool)} candidates")
    if slots == 1:
        print(f"each trial cuts {cuts[0]} (your least-played non-core card) "
              f"and adds one")
        print(f"  that is {len(pool)} of {len(outs) * len(pool)} possible "
              f"swaps — pass a 3rd number to widen (e.g. `optimize "
              f"{path.stem} {archetype} {hands} {rounds} 3`)")
    else:
        print(f"each trial cuts one of {slots} slots ({', '.join(cuts)}) "
              f"and adds one")
        print(f"  {slots * len(pool)} trials of {len(outs) * len(pool)} "
              f"possible swaps")
    print(f"each trial is one funnel_eval run — {hands} hands at "
          f"{engine.SOLVE_MS/1000:.0f}s. This takes hours, not minutes.\n")

    target = scenarios.ensure_target(path)
    if not target:
        print(f"could not establish a target for {path.stem}")
        return
    print(f"holding the target fixed at {target} across all trials\n")
    best, history = optimizer.hill_climb(conn, deck, archetype, target,
                                         hands=hands, rounds=rounds,
                                         slots=slots, pool_limit=pool_limit)
    rows = optimizer.report(history, hands)
    constant, varying = optimizer.degenerate_terms(history)

    # The refusal comes FIRST, before any table. The warning used to print
    # underneath the ranking, and a ranked table with a caveat below it
    # still reads as a ranking -- which is the shape of every wrong answer
    # this project has produced. Measured on the stored kewl-tune run:
    # brick rate, survival and solve rate were each one distinct value
    # over all 10 trials, so the whole ordering came from the combo-length
    # tiebreak at weight -0.05.
    if len(varying) <= 1:
        only = varying[0] if varying else "nothing"
        print(f"\n  !! NO RANKING. {len(constant)} of 5 objective terms did "
              f"not vary at all over {len(history)} trials:")
        print(f"     {', '.join(constant)}")
        print(f"     Only {only} moved, and at weight "
              f"{'-0.05' if only == 'combo length' else 'a tiebreak'} that is "
              f"a tiebreak, not a measurement.")
        print(f"\n     These flex slots are INDISTINGUISHABLE at {hands} hands.")
        print(f"     Re-run with more hands before changing the list. Trials")
        print(f"     below are listed by name, NOT ranked:\n")
        for r in sorted(rows, key=lambda r: r["label"])[:12]:
            print(f"     {(r['brick_rate'] or 0)*100:5.0f}% brick  "
                  f"{(r['survival'] or 0)*100:5.0f}% surv  {r['label']}")
        out = ROOT / "results" / f"optimize_{deck.name}.ydk"
        print(f"\n  the base list is unchanged; nothing was written to {out}")
        return

    print(f"\nbest: {best.label}  score {best.score:+.3f}")
    print(f"\n  {'score':>7}  {'solve':>16}  {'brick':>6}  {'surv':>6}  change")
    for r in rows[:12]:
        lo, hi = r["ci"]
        print(f"  {r['score']:+7.3f}  {r['solve_rate']*100:4.0f}% "
              f"[{lo*100:3.0f}-{hi*100:3.0f}]  "
              f"{(r['brick_rate'] or 0)*100:5.0f}%  "
              f"{(r['survival'] or 0)*100:5.0f}%  {r['label']}")
    # `surv` is an ALL-WINDOWS standard and 0% is its normal reading, not a
    # broken metric. `fire_survives` counts a hand only when the solver
    # recovered from EVERY window the handtrap had, because the opponent
    # picks the timing. Measured on the active deck: Infinite Impermanence
    # gets a median of 11 windows per line (max 17) and about a third are
    # recoverable, so 0 of 15 hands recovered from all of them. Printing
    # 0% with no explanation invites either dismissing the column or
    # panicking at it; the window-recovery share is the number that moves.
    base_row = next((r for r in rows if r["label"] == "base"), None)
    wr = ((base_row or {}).get("window_recovery")
          or (best.summary or {}).get("window_recovery") or {})
    if wr:
        print("\n  surv = share of hands that recover from EVERY window of every")
        print("  fired handtrap (the opponent chooses when to drop it). 0% is")
        print("  common and is not a broken reading. Per-window recovery:")
        for code, share in sorted(wr.items()):
            nm = handlab._name(conn, int(code)) if str(code).isdigit() else code
            print(f"    {nm:34s} {(share or 0)*100:5.1f}% of windows recovered")
    print("\n  A gain inside its confidence interval is noise. With 8-12 hands")
    print("  most of these intervals overlap — treat the table as a shortlist")
    print("  to re-run at higher hand counts, not as a ranking.")
    # Only reached when 2+ terms varied; the degenerate case returned above
    # before printing any table.
    if constant:
        print(f"\n  ! {len(constant)} of 5 objective terms did not vary at all:")
        print(f"    {', '.join(constant)}")
        print(f"    Ranking rests on: {', '.join(varying)}.")
    out = ROOT / "results" / f"optimize_{deck.name}.ydk"
    best.deck.write_ydk(out)
    print(f"\n  best list written to {out}")


def cmd_gauntlet(conn, args):
    """Rank one hand's end boards by what they actually stop."""
    if not args:
        print("usage: gauntlet <deck> <hand | random[:seed]> [hands] [boards]"
              " [solve_s] [opponents]")
        print("  finds the distinct end boards this hand can reach, then")
        print("  deals each opposing deck going-second hands at every one")
        print("  hands     going-second hands PER opposing deck (default 4)")
        print("  boards    candidate boards to test (default 3, + a control)")
        print("  solve_s   per-search budget. Cheap is WRONG, not fast — see")
        print("            engine.SOLVE_MS. For smoke-testing, not measuring.")
        print("  opponents how many opposing decks, by locals weight (default 3)")
        return
    deck_path = _resolve_deck(args[0], conn)
    if not deck_path:
        print(f"deck not found: {args[0]}")
        return
    _warn_if_busy("this run")
    deck = Deck.from_ydk(deck_path)
    deck.canonicalise(conn)
    try:
        hand = handlab.parse_hand(conn, deck, args[1] if len(args) > 1
                                  else "random")
    except ValueError as e:
        print(f"bad hand: {e}")
        return
    hands = int(args[2]) if len(args) > 2 else 4
    boards = int(args[3]) if len(args) > 3 else 3
    solve_ms = int(float(args[4]) * 1000) if len(args) > 4 else None
    # Hands per deck and number of decks were ONE argument in the first
    # version, so asking for more hands silently widened the gauntlet and
    # vice versa -- and the test's resolution depends on the first while
    # its coverage depends on the second.
    n_opp = int(args[5]) if len(args) > 5 else 3

    # The opponents, weighted by YOUR room rather than global meta share --
    # the same rule every other weighted ranking in this project follows.
    others = [p for p in sorted(DECKS.glob("*.ydk")) if p != deck_path]
    others.sort(key=lambda p: -config.weight_for(p.stem))
    opponents = []
    for p in others[:n_opp]:
        target = scenarios.ensure_target(p)
        if not target:
            print(f"  no payoff target for {p.stem} — skipped")
            continue
        opponents.append((p, target))
    if not opponents:
        print("no opposing decks with a payoff target — run `targets` first")
        return

    print(f"{deck_path.stem}, hand: "
          + ", ".join(handlab._name(conn, c) for c in hand))
    print(f"  opponents: " + ", ".join(f"{p.stem} -> {t}"
                                       for p, t in opponents) + "\n")
    if solve_ms:
        print(f"  ! budget overridden to {solve_ms/1000:.0f}s per search — "
              f"a cheap run is a wrong run, not a fast one\n")
    rep = gauntlet.rank(deck_path, hand, opponents, keep=boards,
                        hands=hands, solve_ms=solve_ms, log=print)
    if not rep["results"]:
        print("\nno end board was reached at all — this hand is a brick")
        return

    print("\n  stops  board")
    for r in rep["results"]:
        c = r["candidate"]
        rate = ("  n/a " if r["stop_rate"] is None
                else f"{r['stopped']}/{r['tested']}")
        tag = "  (control: do nothing)" if r.get("control") else ""
        print(f"  {rate:>6s}  {c['interruptions']} int / {c['negates']} neg"
              f"  [{c['label']}]{tag}")
        for row in r["rows"]:
            if row.get("hand") is None:
                print(f"            ! {row['deck']}: {row['error']}")
    fr = rep.get("floor_rate")
    if fr is not None:
        f = rep["floor"]
        print(f"  {f['stopped']}/{f['tested']}  FLOOR — a board that does "
              f"nothing (40 vanilla monsters, one summoned)")
    # A stop rate only means something above the floor. Equal to it means
    # the opponents' own turn-2 task was the obstacle, not your board.
    if fr is not None and not rep.get("beats_floor"):
        print("\n  ! no board beat the FLOOR. Whatever stopped those hands, it")
        print("    was not your board — the opposing decks could not assemble")
        print("    their payoff going second even against a vanilla monster.")
        print("    Calibrate the attacker side (budget, or their targets)")
        print("    before reading anything into the ordering above.")
        return
    # No separation means the test had no resolution, not that the boards
    # are equal. Say so before anyone reads an ordering into it.
    if not rep.get("discriminated"):
        print("\n  ! every board stopped the SAME number of hands — this test")
        print("    did not discriminate. Raise `hands`, or the opposing decks")
        print("    cannot threaten any of these boards.")
        return
    best = rep["results"][0]
    print(f"\n  best board: {best['candidate']['label']}, stopping "
          f"{best['stopped']}/{best['tested']} going-second hands")
    if rep.get("control_rate") is not None:
        print(f"  passing with this hand stops {rep['control_rate']:.0%}, "
              f"a board that does nothing {rep['floor_rate']:.0%}"
              f" — the gap above those is what the board is worth")
    # The heuristic score and the measured one disagreeing is the whole
    # reason this command exists, so say when it happens.
    by_score = max(rep["results"],
                   key=lambda r: (r["candidate"]["interruptions"],
                                  r["candidate"]["negates"]))
    if by_score is not best:
        print(f"  NOTE: the old interruption count would have picked "
              f"{by_score['candidate']['label']} "
              f"({by_score['candidate']['interruptions']} int), which stops "
              f"{by_score['stopped']}/{by_score['tested']}")
    if best["tested"] and best["tested"] < 6:
        print(f"  ! {best['tested']} tested hands is too few to separate "
              f"boards — raise `hands` before believing this ordering")
    path = gauntlet.save(rep, ROOT / "results" /
                         f"gauntlet_{deck_path.stem}.json")
    print(f"  written to {path}")


def cmd_rescore(conn, args):
    """Re-rank a finished optimiser run under the current objective.

    Each `optimize` run costs hours of solver, and the objective's weights
    are judgement calls -- `recovery` especially. Without this, the weights
    could never be examined: you would have to re-run to ask what a
    different weighting would have ranked. Every term is already in the
    stored per-trial summaries.
    """
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: rescore [deck] [recovery_weight]")
        print("  re-ranks stored optimiser trials with no solver time at all")
        return
    path = _resolve_deck(args[0] if args else None, conn)
    name = path.stem if path else (args[0] if args else "")
    obj = optimizer.Objective()
    if len(args) > 1:
        try:
            obj = optimizer.Objective(recovery=float(args[1]))
        except ValueError:
            print(f"recovery weight must be a number, not {args[1]!r}")
            return
    rows = optimizer.rescore(name, objective=obj)
    if not rows:
        print(f"no stored optimiser trials for {name!r} — run `optimize` first")
        return
    print(f"{len(rows)} stored trial(s) for {name}, re-ranked with no solver time")
    print(f"  weights: brick {obj.brick}, solve {obj.solve_rate}, "
          f"survival {obj.survival}, actions {obj.actions}, "
          f"recovery {obj.recovery}\n")
    print(f"  {'score':>7} {'brick':>6} {'solve':>6} {'surv':>5} "
          f"{'combo':>6} {'recov':>6}  change")
    for r in rows:
        # Trial names are "<deck>_r<round>_<cut>_<add>" since the cut was
        # added to them. Reading only the last segment showed the ADD and
        # dropped the cut, so a 3-slot run rendered as duplicate rows --
        # "Called by the Grave" twice, over two different cuts.
        parts = r["label"].split("_")
        shown = r["label"]
        if r["label"] == "base":
            shown = "(your current list)"
        elif len(parts) >= 2 and parts[-1].isdigit() and parts[-2].isdigit():
            shown = (f"-{handlab._name(conn, int(parts[-2]))}"
                     f"  +{handlab._name(conn, int(parts[-1]))}")
        elif parts[-1].isdigit():
            shown = f"+{handlab._name(conn, int(parts[-1]))}"
        print(f"  {r['score']:+7.3f} {(r['brick_rate'] or 0):6.2f} "
              f"{(r['best_line_rate'] or 0):6.2f} {(r['survival'] or 0):5.2f} "
              f"{(r['combo_mean'] or 0):6.2f} {r['window_recovery']:6.3f}  "
              f"{shown}")
    if rows[0]["label"] == "base":
        print("\n  Your current list scores highest — none of the tested swaps "
              "improved it.")
    else:
        print(f"\n  Best stored swap: {rows[0]['label']}. Re-run `optimize` at "
              f"more hands before acting on it.")
    print("  Weights are judgement calls. Pass a recovery weight to see how "
          "the order moves.")


def cmd_second(conn, args):
    if len(args) < 2:
        print("usage: second <their_deck> <our_deck> [hands]")
        return
    _warn_if_busy("this run")
    theirs, ours = _resolve_deck(args[0], conn), _resolve_deck(args[1], conn)
    if not theirs or not ours:
        print("deck not found")
        return
    hands = int(args[2]) if len(args) > 2 else 4
    their_target = scenarios.ensure_target(theirs)
    our_target = scenarios.ensure_target(ours)
    if not their_target or not our_target:
        print("could not establish a target for both decks")
        return
    print(f"{ours.stem} going second into {theirs.stem}")
    print(f"  their board target: {their_target}")
    print(f"  our payoff target : {our_target}\n")
    rows = breaking.going_second(theirs, ours, their_target, our_target,
                                 hands=hands)
    failed = [r for r in rows if r["their_board"] is None]
    built = sum(1 for r in rows if r["their_board"])
    searched = sum(r["searched"] for r in rows)
    through = sum(r["broke"] for r in rows)
    for r in rows:
        if r["their_board"] is None:
            state = f"SOLVER FAILED — {r.get('error','')[:70]}"
        elif not r["their_board"]:
            state = "they bricked"
        elif not r["searched"]:
            state = "NO SEARCH (misconfigured)"
        else:
            state = "got through" if r["broke"] else "stopped"
        print(f"  hand {r['seed']}: {state}")
    print(f"\n  their board built {built}/{len(rows)}, "
          f"we searched {searched}, got through {through}")
    if failed:
        print(f"  ! {len(failed)} hand(s) the SOLVER failed on — not deck weakness,")
        print("    and excluded from the counts above")
    if searched < built:
        print("  ! some hands never entered a search — see breaking.searched()")


def cmd_floodgates(conn, args):
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: floodgates <deck.ydk> [hands]")
        return
    _warn_if_busy("this check")
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    hands = int(args[1]) if len(args) > 1 else 2
    our_target = scenarios.ensure_target(path)
    if not our_target:
        print(f"could not establish a target for {path.stem}")
        return
    print(f"{path.stem} under floodgates, target {our_target}")
    print("locals run these far more than top tables do\n")
    rows = breaking.floodgate_check(path, our_target, hands=hands)
    print(f"  {'functioned':>12}  floodgate")
    for r in rows:
        err = f"  ({r['errors']} solver failures)" if r.get("errors") else ""
        if not r["tested"]:
            print(f"  {'untested':>12}  {r['name']}  (could not build the board){err}")
        else:
            print(f"  {r['functioned']}/{r['tested']:<10}  {r['name']}{err}")
    print("\n  'functioned' = still assembled its payoff with that card set")
    print("  across from it. A set Continuous Trap cannot activate the turn")
    print("  it is set, so this models it flipping during your turn.")


def cmd_export(conn, args):
    if not args:
        print("usage: export <archetype> [filename]")
        return
    archetype = args[0]
    # Best PLACING list, not the first one scraped. `ORDER BY d.id LIMIT 1`
    # ranked by scrape order, which is how branded.ydk became a hybrid
    # with no Branded Fusion that averages 1.0 interruptions over 40 hands
    # while serving as a meta deck in the board library.
    row = ms.best_list(conn, archetype)
    if not row:
        print(f"no stored lists tagged {archetype!r}")
        return
    print(f"  best placing of the stored {archetype!r} lists: "
          f"{row['placement'] or 'placement unknown'}")
    secs = {"main": [], "extra": [], "side": []}
    for r in conn.execute(
        "SELECT card_id, section, copies FROM deck_cards WHERE deck_id = ?",
        (row["id"],),
    ):
        secs[r["section"]] += [r["card_id"]] * r["copies"]
    name = args[1] if len(args) > 1 else archetype.lower().replace(" ", "-")
    d = Deck(name=name, **secs)
    DECKS.mkdir(parents=True, exist_ok=True)
    d.write_ydk(DECKS / f"{name}.ydk")
    print(f"wrote {name}.ydk — {row['title']} ({row['placement']}) {d}")


def cmd_import(conn, args):
    if not args:
        print("usage: import <.ydk path | ydke://... | ygoprodeck deck URL> [name]")
        print("  the ydke:// form is what most deckbuilders put on your clipboard")
        return
    source = args[0]
    name = " ".join(args[1:]) if len(args) > 1 else None
    try:
        deck = importer.load(source, name)
    except Exception as e:
        print(f"import failed: {e}")
        return

    unresolved = deck.canonicalise(conn)
    path = importer.save(deck, name)
    print(f"imported {deck!r} -> {path}")
    if unresolved:
        print(f"  warning: {len(unresolved)} passcode(s) not in the card mirror "
              f"— run `cards` to refresh")

    problems = deck.validate(conn)
    if problems:
        print("  legality:")
        for p in problems:
            print(f"    ! {p}")
    else:
        print("  legality: legal")

    bad = carddb.check_deck(deck)
    if bad:
        print(f"  ! {len(bad)} card(s) the ENGINE cannot load — solver runs on this")
        print("    deck would segfault. Try `setup` to rebuild the merged database.")

    # Name what this REPLACED. Importing repoints every bare command --
    # solve, report, optimize, drill, gauntlet all default to the active
    # deck -- so importing an opponent's list or a meta deck for the
    # gauntlet silently moves the target away from your own list. The
    # behaviour is deliberate and right for the common case; the one-line
    # notice just did not convey the blast radius or how to undo it.
    previous = db.get_meta(conn, "active_deck")
    db.set_meta(conn, "active_deck", deck.name)
    conn.commit()
    if previous and previous != deck.name:
        print(f"  set as your active deck, replacing {previous}")
        print(f"    every bare command (solve / report / optimize / drill) now")
        print(f"    targets {deck.name}. To switch back:  mydeck {previous}")
    else:
        print(f"  set as your active deck (`mydeck` to change)")


def cmd_mydeck(conn, args):
    if args:
        path = _resolve_deck(args[0], conn)
        if not path:
            print(f"no such deck: {args[0]} (see `decks`)")
            return
        db.set_meta(conn, "active_deck", path.stem)
        conn.commit()
        print(f"active deck is now {path.stem}")
        return
    name = db.get_meta(conn, "active_deck")
    if not name:
        print("no active deck — `import` one, or `mydeck <name>` to pick an existing")
        return
    path = DECKS / f"{name}.ydk"
    if not path.exists():
        print(f"active deck {name!r} no longer on disk")
        return
    deck = Deck.from_ydk(path)
    deck.canonicalise(conn)
    print(f"{deck!r}")
    problems = deck.validate(conn)
    print("  legality:", "legal" if not problems else "")
    for p in problems:
        print(f"    ! {p}")
    print("\n  commands that take a deck will use this one when called bare")


def cmd_ydke(conn, args):
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print("usage: ydke <deck>  (or set one with `mydeck`)")
        return
    print(importer.to_ydke(Deck.from_ydk(path)))


def cmd_report(conn, args):
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print("usage: report <deck> [archetype]   (or set one with `mydeck`)")
        return
    archetype = " ".join(args[1:]) if len(args) > 1 else None
    r = report.deck_report(conn, path, archetype)

    m, e, s = r["sizes"]
    print(f"\n  {r['name']}  —  {m} main / {e} extra / {s} side")

    print("\n  LEGAL")
    if r["legality"]:
        for p in r["legality"]:
            print(f"    ! {p}")
    else:
        print("    passes deck construction and the TCG banlist")
    if r["unresolved"]:
        print(f"    ! {len(r['unresolved'])} passcode(s) unknown to the card mirror")
    if r["engine_blockers"]:
        print(f"    ! {len(r['engine_blockers'])} card(s) the solver cannot load "
              f"— run `setup`")

    print("\n  SOLVER TARGET")
    if not r["target"]:
        print("    none yet — `gate` will derive one (costs a solve pass)")
    else:
        code = int(r["target"].split("@")[0])
        row = card_db.resolve(conn, code)
        print(f"    {row['name'] if row else r['target']}  ({r['target']})")
        if r["target_warning"]:
            print(f"    ! {r['target_warning']}")
            print("      measurements below are about reaching THAT, so treat")
            print("      them as unmeasured rather than as a verdict")

    print("\n  MEASURED")
    f = r["funnel"]
    if not f:
        print("    no funnel run yet — `gate` to measure")
    else:
        pv = r.get("provenance") or {}
        ms = pv.get("solve_ms")
        print(f"    source        {pv.get('file','?')}, "
              f"{pv.get('hands','?')} hands at "
              f"{(ms/1000) if ms else '?'}s/hand")
        if ms and ms < 60000:
            print(f"    ! that budget is below the 60s default — a budget too low")
            print(f"      invents bricks. Re-run `gate` or `ladder` before trusting")
            print(f"      the brick rate below.")
        solved, hands = f.get("solved") or 0, f.get("hands") or 0
        p, lo, hi = stats.wilson(solved, hands)
        print(f"    solved        {solved}/{hands}  "
              f"({p*100:.0f}%, 95% CI {lo*100:.0f}-{hi*100:.0f}%)")
        ca = f.get("combo_actions") or {}
        if ca.get("median") is not None:
            print(f"    line length   median {ca['median']} actions "
                  f"({ca.get('min')}-{ca.get('max')})")
        # Direction matters and is the opposite of `matrix`. There, a low
        # survival is GOOD (the handtrap blanks an opponent). Here the
        # deck is YOURS, so a low survival means you fold to that card.
        # The same number printed without a direction invites the reader
        # to carry over the wrong reading.
        surv_rows = [
            (card_db.resolve(conn, int(code)), code, s)
            for code, s in (f.get("survival_given_line") or {}).items()
        ]
        live = [(r, c, s) for r, c, s in surv_rows if s is not None]
        if live:
            print("\n  PLAYS THROUGH (higher is better — this is YOUR deck)")
            blind = []
            for row, code, s in sorted(live, key=lambda t: t[2]):
                name = row["name"] if row else code
                if int(code) in scenarios.BLIND_SPOTS:
                    blind.append((name, int(code)))
                    verdict = "NOT MEASURABLE"
                else:
                    verdict = ("folds to it" if s <= 0.15 else
                               "struggles" if s <= 0.5 else "fine")
                print(f"    {s*100:4.0f}%  {name:<34} {verdict}")
            judged = [t for t in live
                      if int(t[1]) not in scenarios.BLIND_SPOTS]
            if judged:
                worst = min(judged, key=lambda t: t[2])
                wname = worst[0]["name"] if worst[0] else worst[1]
                print(f"    -> {wname} is your worst matchup card")
            for name, code in blind:
                print(f"    ! {name}: {scenarios.BLIND_SPOTS[code]}.")
                if code == 23434538:
                    cache = ROOT / "results" / "special_summons.json"
                    cost = None
                    if cache.exists():
                        ss = json.loads(cache.read_text())
                        if r["name"] in ss:
                            cost = ss[r["name"]]
                    if cost:
                        print(f"      Measured separately: comboing through it hands "
                              f"them {cost['mean']:.1f} cards")
                        print(f"      on average, {cost['max']} at worst. Surviving it "
                              f"is not beating it.")
                    else:
                        print(f"      Run `threats` for the cards it actually costs you.")
        for row, code, s in surv_rows:
            if s is None:
                name = row["name"] if row else code
                print(f"    n/a   {name:<34} never had a window")
        if hands and hands < 20:
            print(f"    ! only {hands} hands — the interval above is wide enough")
            print("      that most comparisons against it are noise")

    print("\n  OPENING ODDS (going first)")
    for name, copies, pc in r["hand_math"]:
        print(f"    {copies}x  {pc*100:5.1f}%  {name}")

    if r["inclusion"]:
        inc = r["inclusion"]
        g = r.get("archetype_guess")
        how = (f" — identified from your cards, {g[1]*100:.0f}% of its "
               f"{g[2]}-card core" if g else "")
        print(f"\n  VS THE META  ({inc['lists']} lists of {r['archetype']}){how}")
        for alt, ov, n_ in (r.get("archetype_alts") or []):
            print(f"    also matches {alt} ({ov*100:.0f}% of {n_} core)")
        print(f"    {inc['core']} core cards, {inc['flex']} contested slots")
        if inc["off_meta"]:
            print("    cards in your list that no scraped list runs:")
            for n in inc["off_meta"]:
                print(f"      {n}")
    print()


def cmd_drill(conn, args):
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print("usage: drill <deck> [count]   (or set one with `mydeck`)")
        return
    limit = int(args[1]) if len(args) > 1 else 3

    sources = sorted((ROOT / "results").glob("*.jsonl"),
                     key=lambda p: -p.stat().st_mtime)
    written = []
    for src in sources:
        try:
            written = drill.export(conn, src, path.stem, limit=limit)
        except Exception:
            continue
        if written:
            print(f"  from {src.name}")
            break
    if not written:
        print(f"no solved lines stored for {path.stem} — run `solve` or `gate` first")
        return

    for yrpx, txt, n in written:
        print(f"\n  {n} interruptions -> {yrpx.name}")
        for line in txt.read_text().splitlines():
            if line.startswith("  ") or line.startswith("ends on"):
                print(f"  {line}")
    print(f"\n  open the .yrpX files in EDOPro to watch the line back")
    print(f"  {written[0][0].parent}")


def cmd_decks(conn, args):
    DECKS.mkdir(parents=True, exist_ok=True)
    files = sorted(DECKS.glob("*.ydk"))
    if not files:
        print(f"no .ydk files in {DECKS}")
        return
    for f in files:
        print(f"  {f.stem}")


COMMANDS = {
    "cards": cmd_cards,
    "images": cmd_images,
    "pilot": cmd_pilot,
    "meta": cmd_meta,
    "archetypes": cmd_archetypes,
    "flex": cmd_flex,
    "hand": cmd_hand,
    "check": cmd_check,
    "decks": cmd_decks,
    "import": cmd_import,
    "mydeck": cmd_mydeck,
    "ydke": cmd_ydke,
    "report": cmd_report,
    "drill": cmd_drill,
    "export": cmd_export,
    "doctor": cmd_doctor,
    "setup": cmd_setup,
    "engine": cmd_engine,
    "solve": cmd_solve,
    "ladder": cmd_ladder,
    "gate": cmd_gate,
    "boards": cmd_boards,
    "threats": cmd_threats,
    "targets": cmd_targets,
    "snapshot": cmd_snapshot,
    "drift": cmd_drift,
    "matrix": cmd_matrix,
    "locals": cmd_locals,
    "optimize": cmd_optimize,
    "second": cmd_second,
    "gauntlet": cmd_gauntlet,
    "rescore": cmd_rescore,
    "floodgates": cmd_floodgates,
}


def main():
    # Subprocesses write to this same fd unbuffered while our own prints
    # sit in a block buffer until exit, so piped output arrives out of
    # order -- a warning printed BEFORE a long solve showed up after its
    # results, which makes it useless as a warning.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
    if n == 0:
        print("card mirror empty, syncing ...")
        print(card_db.sync(conn))
    argv = sys.argv[1:]
    if argv:  # one-shot mode, no banner
        run(conn, argv[0], argv[1:])
        return

    print(BANNER)

    while True:
        try:
            line = input("duelroom> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = shlex.split(line)
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("quit", "q", "exit"):
            break
        if cmd == "help":
            print(BANNER)
            continue
        run(conn, cmd, args)


def run(conn, cmd, args):
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"unknown command: {cmd} (try `help`)")
        return
    try:
        fn(conn, args)
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
