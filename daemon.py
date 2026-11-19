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
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills import (breaking, card_db, carddb, config, db, drill, engine,
                    gate, gauntlet, handlab, images, importer,
                    meta_scrape as ms, optimizer, report, scenarios,
                    stats, sweep, trainer, game)
from skills.deck import Deck

ROOT = Path(__file__).resolve().parent
DECKS = engine.DECKS

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
  banlist [ack]             is there a newer TCG banlist? (run daily by cron); ack clears the banner
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
  draws [deck]              what each extra solver draw bought, no CPU
  floodgates <deck>         does the deck function under common floodgates
  train <deck> [hours] [n]  play n hands over and over, keep each best line
  train <enemy> --vs <deck> [hours] [n]  train the n hands the enemy opens with vs <deck>
  games <deck> <enemy> [runs] [enemy hands] [--second] [--workers N] [--from-end]  finish the game: win rate by turn 3 (going 2nd: turn 2)
  impact <deck> [enemy] [--first|--second]  card impact scores from the games played
  stops <deck> [enemy] [--first|--second]  which of their cards you stopped, with what, and what it won
  starters <deck> <1-3> [hours]  what every 1/2/3-card hand makes on its own
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


def _board_library(args, why, usage):
    """Pick a board library: the one named, or the single-draw default.

    Shared by `targets` and `threats` so the choice is made in ONE place,
    for the same reason `scenarios.survival_from_hands` is one function
    (ISSUES #49): two implementations of a choice drift apart.

    The default stays results/boards.jsonl deliberately. Silently
    re-pointing at whatever file is newest would change every downstream
    number with nobody asking, and newer is NOT better here -- the
    `boards_multi.*` arms are controls preserved BECAUSE they were built
    by defective code. Newer candidates are reported, never used.

    Returns (path, stamp), or (None, None) when there is nothing usable.
    """
    src = Path(args[0]) if args else ROOT / "results" / "boards.jsonl"
    if not src.is_absolute():
        src = src if src.exists() else ROOT / "results" / src.name
    if not src.exists():
        print(f"no {src} — build the board library first, or name one: {usage}")
        return None, None
    stamp = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    print(f"library: {src.name} ({stamp})")
    cands = [q for q in (ROOT / "results").glob("boards*.jsonl")
             if q != src and q.stat().st_mtime > src.stat().st_mtime]
    # Newest LAST by MTIME. Sorting by NAME recommended
    # boards_multi.stale-ranking.jsonl -- a deliberately-defective arm.
    cands.sort(key=lambda q: q.stat().st_mtime)
    if cands:
        print(f"  ! newer libraries exist; none is used automatically, "
              f"because {why}:")
        for q in reversed(cands):
            when = datetime.fromtimestamp(q.stat().st_mtime).strftime("%m-%d %H:%M")
            # A dot in the stem marks a preserved variant
            # (boards_multi.narrow-stacks vs boards_multi).
            tag = "  (preserved variant)" if "." in q.stem else ""
            # sweep._checkpoint writes <out>.partial for a run's whole
            # duration, so its presence means that library is incomplete.
            if Path(str(q) + ".partial").exists():
                tag += "  (RUN IN PROGRESS — incomplete)"
            print(f"      {q.name:<40} {when}{tag}")
        pick = next((q for q in reversed(cands)
                     if "." not in q.stem
                     and not Path(str(q) + ".partial").exists()), None)
        if pick:
            print(f"    Re-run as `{usage.split()[0]} {pick.name}` to switch.")
        else:
            print("    Nothing complete to switch to yet.")
    return src, stamp


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
        # The GRADE, not the verdict. "0 recovered" over windows that each
        # kept four of five board cards is a different hand to hold from
        # one that kept none, and the solver graded every window.
        if h.get("of"):
            print(f"      you get back {h['kept']}/{h['of']} of the target "
                  f"board's cards ({h['kept_share']*100:.0f}%) over those "
                  f"points")
            if h.get("complete_but_unmatched"):
                print(f"      of which {h['complete_but_unmatched']} point(s) "
                      f"got EVERY card back in a board that did not match "
                      f"the original")
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
            grade = ""
            if d.get("of") and not d["survived"]:
                grade = f"  [got back {d['kept']}/{d['of']}"
                grade += ("; all of them, but not in a matching board]"
                          if d["kept"] == d["of"] else "]")
            print(f"      on {d['over'] or '?'}  ->  {mark}{grade}")
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

    # Per-card odds cannot answer "do I open interaction at all", because
    # a handtrap is interchangeable with every other handtrap. The active
    # deck's best single card is 31.0% while the group of 8 is 65.3%, so
    # reading the per-card column as the answer understates the deck by
    # more than two to one. `stats.group_chance` has always been correct
    # and tested, and nothing in the app could reach it.
    held = Counter(d.main)
    group = {cid: n for cid, n in held.items() if cid in scenarios.HANDTRAPS}
    if group:
        total = sum(group.values())
        print(f"\n  interaction: {total} handtrap copies across "
              f"{len(group)} card(s)\n")
        names = ", ".join(f"{n}x {scenarios.HANDTRAPS[c]}"
                          for c, n in sorted(group.items(), key=lambda t: -t[1]))
        print(f"    {names}")
        print(f"\n    {'want':>5}  {'first':>7}  {'second':>7}")
        for want in (1, 2):
            g5 = stats.group_chance(size, total, 5, want) * 100
            g6 = stats.group_chance(size, total, 6, want) * 100
            print(f"    {'>=' + str(want):>5}  {g5:6.1f}%  {g6:6.1f}%")
        print(f"\n    (the GROUP, not any one card — they are "
              f"interchangeable for this question)")

    print(f"\n  brick floor by starter count (P(zero starters), {size}-card deck)\n")
    print(f"  {'starters':>8}  {'first':>7}  {'second':>7}")
    for k in range(6, 19):
        b5 = stats.brick_chance(size, k, 5) * 100
        b6 = stats.brick_chance(size, k, 6) * 100
        print(f"  {k:>8}  {b5:6.1f}%  {b6:6.1f}%")
    print("\n  (a floor, not the real brick rate — a hand can hold a starter and")
    print("   still have no line. Stage 3 measures the real number.)")


def cmd_banlist(conn, args):
    from skills import banlist, card_db

    if args and args[0] == "ack":
        banlist.ack(conn)
        print("banlist notice cleared")
        return
    note = banlist.check(conn)
    print(f"engine list: {banlist.parse(banlist.LOCAL.read_text())[0]}; "
          f"upstream: {db.get_meta(conn, 'banlist_upstream')}")
    if note:
        print(f"NEW BANLIST: {note['name']} -- {len(note['changes'])} cards changed")
        for c, a, b in note["changes"]:
            row = card_db.resolve(conn, c)
            print(f"  {row['name'] if row else c}: {banlist.WORDS[a]} -> {banlist.WORDS[b]}")
        for deck, bad in note["decks"].items():
            print(f"  {deck} breaks it: " + ", ".join(
                f"{n}x {(card_db.resolve(conn, c) or {'name': c})['name']} (max {m})"
                for c, n, m in bad))
    else:
        print("no new banlist")
    # the card mirror's own banlist (YGOPRODeck, used by `check`)
    print(card_db.sync(conn))


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
    decks = sorted(DECKS.glob("*.ydk"))
    print(f"    {len(decks):>6}  deck files")
    active = db.get_meta(conn, "active_deck")
    print(f"            active deck: {active or '(none — `import` or `mydeck`)'}")

    print("\n  GUARDS")
    # `tests/test_cli.py` calls this function, so shelling out to the suite
    # from inside the suite re-enters it: each `doctor` starts a pytest that
    # starts a `doctor` that starts a pytest. It only ever looked harmless
    # because the inner run died instantly on an ImportError -- a broken
    # collection was the only thing ending the recursion. With that fixed
    # (tests/conftest.py) the same loop ran the machine's load average to
    # 13 before the timeout killed the tree. Under pytest, skip the guard
    # that IS pytest; the other four are plain scripts and cannot re-enter.
    in_pytest = "PYTEST_CURRENT_TEST" in os.environ
    for label, cmd in (
        ("regression tests", None if in_pytest
         else [str(engine.ROOT / "venv/bin/pytest"), "tests/", "-q"]),
        ("invariants", [str(engine.PYTHON), "tools/check_invariants.py"]),
        ("field audit", [str(engine.PYTHON), "tools/audit_fields.py"]),
        ("doc drift", [str(engine.PYTHON), "tools/check_docs.py"]),
        # Added after two NameErrors in one session that no test could
        # see: a missing `import shutil` broke every sweep while its own
        # test passed, and the same mistake dropped `import contextlib`
        # an hour later. pyflakes finds both by name and line.
        ("undefined names", [str(engine.PYTHON), "tools/check_names.py"]),
    ):
        if cmd is None:
            print(f"    --    {label:18} skipped — already running under pytest")
            continue
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
        # A solver crash is counted in `hands` and not in `solved`, so it
        # reads as a brick. Name it, the way `floodgates` already does.
        errs = row.get("errors") or 0
        err = f"  ({errs} SOLVER FAILURE(S), not deck bricks)" if errs else ""
        print(f"    solve rate        {row['solved']}/{row['hands']} "
              f"({row['solve_rate']*100:.0f}%){err}")
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
    # The honest per-hand number, not `summary.survival_given_line`:
    # that field counts a hand where the handtrap had NO legal window as
    # a survival, so it is never lower and is badly wrong for a card that
    # is rarely live. Measured over 47 cells, mean gap 10.3 points, max
    # 91 -- kewl-tune vs Nibiru reads 91% there and 0% here.
    cells = scenarios.survival_from_hands(rec)
    for code, surv in (rep["survival_given_line"] or {}).items():
        row = card_db.resolve(conn, int(code))
        name = row["name"] if row else code
        cell = cells.get(int(code)) or {}
        tested, offered = cell.get("tested") or 0, cell.get("offered") or 0
        honest = cell.get("survival")
        # None when no hand solved: there is no "survival given a line"
        # if there were no lines. Saying so beats crashing or printing 0%.
        if surv is None:
            print(f"  survives {name}: n/a (no hand solved)")
        elif not tested:
            print(f"  survives {name}: never had a legal window in "
                  f"{offered} solved hand(s) — UNMEASURED, not harmless")
        else:
            print(f"  survives {name}: {honest*100:.0f}% of the "
                  f"{tested} hand(s) where it was live "
                  f"(live in {tested}/{offered})")

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

    # The library was hardcoded here too, so the special-summon and
    # "Nibiru live" figures came only from the single-draw library. It
    # matters: the multi-draw arm gives elfnote 8.2 summons and 90%
    # Nibiru-live against 6.1 and 68%, and despia 3.9/48% against 3.0/32%
    # -- better boards need more summons, so single-draw UNDERSTATES
    # Nibiru exposure by up to 22 points. Same shared selector as
    # `targets`, same deliberate default.
    bpath, _bstamp = _board_library(
        args, "the summon counts below are derived from it",
        "threats [boards_multi.jsonl]")
    if bpath is not None:
        cache = ROOT / "results" / "special_summons.json"
        # Nothing used to WRITE this cache. `scenarios.special_summons`
        # is the producer and no command called it, so the three tables
        # below were rendered from a file created out of band -- stale the
        # moment the board library is rebuilt, and gone for good if it
        # were ever deleted. Replaying is cheap (6.6 s for all 280
        # records, no search involved), so regenerate it whenever it is
        # missing, older than the library, or was built from a DIFFERENT
        # library -- the last of which a timestamp alone cannot see.
        cached = {}
        if cache.exists():
            try:
                cached = json.loads(cache.read_text())
            except json.JSONDecodeError:
                cached = {}
        stale = (not cache.exists()
                 or cache.stat().st_mtime < bpath.stat().st_mtime
                 or cached.get("_source") != bpath.name)
        if stale:
            print(f"\n  (rebuilding special-summon counts from "
                  f"{bpath.name} — replaying lines, no search)")
            recs_b = [json.loads(l) for l in
                      bpath.read_text().splitlines() if l.strip()]
            fresh = scenarios.special_summons(recs_b, limit=20)
            if fresh:
                cache.write_text(json.dumps(
                    {"_source": bpath.name, **fresh}, indent=1))
                cached = {"_source": bpath.name, **fresh}
        if cached:
            ss = {k: v for k, v in cached.items() if not k.startswith("_")}
            age = "rebuilt just now" if stale else "cached"
            print(f"\n  special summons per line (replayed, not inferred; "
                  f"{age} from {cached.get('_source', bpath.name)})\n")
            print("  " + "archetype".ljust(28) + "  SS/line   max   Nibiru live")
            for deck, v in sorted(ss.items(), key=lambda kv: -kv[1]["mean"]):
                print(f"  {deck.ljust(28)}{v['mean']:8.1f}{v['max']:6}"
                      f"{v['nibiru_rate']*100:12.0f}%")
            print("\n  (Nibiru live = share of lines passing 5 summons. This")
            print("   explains its window counts: r=0.94 against them.)")
            # A line that will not replay used to be skipped in silence,
            # which left every figure above with an invisible
            # denominator. Measured 0 failures over all 271 lines of
            # boards.jsonl, so this normally prints nothing -- it exists
            # so that a submodule bump breaking replay reads as a
            # failure rather than as a smaller sample.
            bad = [(d, v) for d, v in ss.items() if v.get("failed")]
            if bad:
                print("\n  ! lines that would NOT replay, so they are NOT in "
                      "the figures above:")
                for d, v in sorted(bad, key=lambda kv: -kv[1]["failed"]):
                    print(f"      {d.ljust(28)}{v['failed']} of "
                          f"{v['attempted']} failed")
                print("      Run `setup` after any submodule bump — a deck "
                      "holding a card whose")
                print("      Lua did not load replays as nothing.")
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
    # The source was hardcoded to results/boards.jsonl -- the SINGLE-DRAW
    # library, which CLAUDE.md documents as understating every deck
    # ("one attempt is one draw"). Targets decide what every later solve
    # aims at, so that choice propagates into every number downstream,
    # and it could not be changed or even compared against.
    #
    # It matters: deriving from the 4-deck multi-draw arm changes the
    # target for 2 of 5 decks -- branded goes Rindbrumm -> Stardust
    # Dragon - Victim Sanctuary, fiendsmith goes Fiendsmith's Desirae ->
    # F.A. Dawn Dragster.
    #
    # The default stays boards.jsonl, deliberately: silently re-pointing
    # the whole measurement chain at a newer file would change every
    # downstream number with no one asking for it, and a newer library is
    # not automatically a better one (the multi-draw arm above came from
    # a run abandoned mid-way). So: the source is nameable, it is always
    # printed with its date, and a newer candidate is reported rather
    # than used.
    src, _stamp = _board_library(
        args, "targets decide what every later solve aims at",
        "targets [boards_multi.jsonl]")
    if src is None:
        return
    out = ROOT / "assets" / "derived_targets.json"
    t = scenarios.write_targets(src, out)
    print(f"\nderived targets for {len(t)} decks -> {out}\n")
    for deck, named in scenarios.describe_targets(conn, t):
        print(f"  {deck:28} {', '.join(named)}")
    weak = scenarios.trivial_targets(scenarios.derive_targets(src))
    if weak:
        print(f"\n  weak targets (from {src.name}) — these decks build little,")
        print("  so their solve rate")
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
    decks = [p.stem for p in DECKS.glob("*.ydk")]
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

    # Weighted coverage, not just the deck count. The ranking below is
    # correct over what it measured, and `threat_handtraps` turns it into
    # the handtraps the optimiser fires -- so how much of the room it saw
    # is a decision input. locals.toml's largest single slice is `rogue`
    # at 0.250 and there is no deck for it.
    _share, _missing = scenarios.covered_share(m, config.weights())
    if _missing:
        print(f"  ! WEIGHTED COVERAGE {_share:.0%} of your stated meta. "
              f"No deck measured for:")
        print("    " + ", ".join(f"{d} ({w:.0%})" for d, w in _missing[:6]))
        print(f"    Every ranking below is over the {_share:.0%} that was "
              f"measured, correctly normalised — but a handtrap that only")
        print(f"    answers an unmeasured deck cannot appear in it at all.")
        print()
    codes = sorted(scenarios.HANDTRAPS)
    short = {c: scenarios.HANDTRAPS[c].split(" &")[0].split(",")[0][:9] for c in codes}

    print("  SURVIVAL — of the HANDS where the handtrap had a legal window,")
    print("  how often the line survived it being fired in EVERY window.")
    print("  Not a per-window rate: 8 windows converted of 11 counts as 0.")
    print("  Lower is better for you.")
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
        # "Re-run with more hands" without a number is advice the reader
        # cannot act on. `stats.trials_needed` answers it and was
        # unreachable from the app.
        need = stats.trials_needed(0.10)
        print(f"     To resolve a 10-point difference you need ~{need} hands "
              f"per arm unpaired; paired")
        print(f"     seeds (what this uses) need far fewer, but {hands} is "
              f"well short either way.")
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
        # This share is BINARY per window: the funnel counts a window as
        # recovered only when the whole board came back, and it never sees
        # the solver's own grade ("best 4/5"), which lives in a fire log
        # the funnel discards. So a window that rebuilt four of five board
        # cards is a 0 here. `hand <deck> <cards>` reads those logs and
        # reports the grade; this column is the floor of it, not a
        # different measurement of the same thing.
        print("  Binary per window: only a WHOLE board counts, so a window")
        print("  that rebuilt 4 of 5 cards reads 0 here. `hand` grades them.")
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
    """Rank one hand's end boards by what the meta plays THROUGH."""
    if not args:
        print("usage: gauntlet <deck> <hand | random[:seed]> [boards] "
              "[draws] [solve_s]")
        print("  finds the distinct end boards this hand can reach, then")
        print("  ranks them by what each one STOPS, from the measured")
        print("  handtrap matrix — no going-second search (see below)")
        print("  boards    candidate boards to rank (default 3)")
        print("  draws     solver draws per goal (default "
              f"{gauntlet.DRAWS}; the search is randomised)")
        print("  solve_s   per-search budget. Cheap is WRONG, not fast — see")
        print("            engine.SOLVE_MS. For smoke-testing, not measuring.")
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
    boards = int(args[2]) if len(args) > 2 else 3
    draws = int(args[3]) if len(args) > 3 else gauntlet.DRAWS
    solve_ms = int(float(args[4]) * 1000) if len(args) > 4 else None

    path = _latest_matrix()
    if not path:
        print("no handtrap matrix yet — run `matrix` (scenarios.run_matrix) "
              "first; this ranking is read from it")
        return
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    m = scenarios.handtrap_matrix(recs)
    if not m:
        print(f"{path.name} holds no usable records")
        return
    weights = config.weights()
    share, missing = scenarios.covered_share(m, weights)

    print(f"{deck_path.stem}, hand: "
          + ", ".join(handlab._name(conn, c) for c in hand))
    print(f"  ranked against {path.name}: {len(m)} deck(s), "
          f"{share:.0%} of your stated meta by weight")
    if missing:
        print("  ! not measured: "
              + ", ".join(f"{d} {w:.0%}" for d, w in missing[:4]))
    if solve_ms:
        print(f"  ! budget overridden to {solve_ms/1000:.0f}s per search — "
              f"a cheap run is a wrong run, not a fast one")
    print()

    found = gauntlet.candidate_boards(deck_path, hand, keep=None,
                                      draws=draws, solve_ms=solve_ms,
                                      log=print)
    if not found:
        print("\nno end board was reached at all — this hand is a brick")
        return
    cands = found[:boards]
    # The `base` rung -- build nothing, keep what the hand holds -- survives
    # the cut even when the heuristic score buries it, and it matters MORE
    # to this method than to the old one: its pieces are the hand traps you
    # kept, which is exactly what `--fire` measures. "Is building anything
    # better than holding your traps?" is the comparison, and without the
    # rung there is nothing to compare against.
    base = next((c for c in found if c.label == "base"), None)
    if base is not None and base not in cands:
        cands.append(base)
        print(f"  + [base]: {base.interruptions} int (do nothing, hold what "
              f"the hand holds) — carried as the comparison")

    print("\nranking the boards by what gets through them")
    rows = gauntlet.rank_boards(cands, m, weights, log=print)

    print("\n  through  board")
    for r in rows:
        c = r["candidate"]
        thr = ("    n/a" if r["through"] is None
               else f"  {r['through']:5.0%}")
        tag = "  (built nothing)" if c["label"] == "base" else ""
        print(f"  {thr}  {c['interruptions']} int / {c['negates']} neg"
              f"  [{c['label']}]{tag}")
        # Three classes, kept apart on purpose. "Held in hand" is what
        # the fire test reproduces exactly; a held card with no matrix
        # cell is eligible but unmeasured; a field or graveyard piece is
        # not something anyone can activate from hand, so no flag on this
        # solver plays it. Printing the first two together read as a
        # board fully measured when half of it was not scored at all.
        no_cell = set()
        for cell in r["per_deck"].values():
            no_cell.update(cell.get("no_cell") or ())
        held = [handlab._name(conn, p[0]) for p in r["measured"]
                if p[0] not in no_cell]
        unmeas = [handlab._name(conn, c) for c in sorted(no_cell)]
        rest = [f"{handlab._name(conn, p[0])} ({p[1]})" for p in r["untested"]]
        print(f"            measured (held in hand): "
              + (", ".join(held) or "nothing"))
        if unmeas:
            print(f"            held but NOT in the matrix: "
                  + ", ".join(unmeas))
        if rest:
            print(f"            NOT measurable by firing: " + ", ".join(rest))
        for dname, cell in sorted(r["per_deck"].items(),
                                  key=lambda kv: (kv[1]["through"] is None,
                                                  kv[1]["through"] or 0)):
            if cell["through"] is None:
                continue
            bits = ", ".join(
                f"{handlab._name(conn, d['code'])} {d['through']:.0%}"
                for d in cell["pieces"] if d["through"] is not None)
            print(f"              vs {dname:24s} {cell['through']:5.0%}"
                  + (f"   ({bits})" if bits else ""))

    scored = [r for r in rows if r["through"] is not None]
    if not scored:
        print("\n  ! NOTHING MEASURABLE. Every interruption on every one of")
        print("    these boards sits on the field or in the graveyard, and")
        print("    `--fire` plays a card from the opponent's HAND — so this")
        print("    method cannot see any of them. The ordering above is not")
        print("    a ranking; it is the heuristic score in a different column.")
        return
    best = scored[0]
    print(f"\n  best board: [{best['candidate']['label']}], "
          f"{best['through']:.0%} of the weighted meta plays through it")
    # The comparison that decides whether building was worth it. If the
    # base rung scores the same, this method is saying your hand traps did
    # the work and the board added nothing it can see -- which is a real
    # answer, not a failure, and the field pieces it cannot see are where
    # the difference would be.
    base_row = next((r for r in scored
                     if r["candidate"]["label"] == "base"), None)
    gap = (base_row["through"] - best["through"]) if base_row else None
    if gap is not None and round(gap, 4) > 0:
        print(f"  building nothing and holding your traps lets "
              f"{base_row['through']:.0%} through, so the board is worth "
              f"{gap:+.0%} on what was measured")
    elif base_row is not None:
        print("  ! holding your traps and building NOTHING scores as well as "
              "the best board.")
        print("    On the pieces this method can see, the board added "
              "nothing — and what it")
        print("    cannot see (the field) is where any difference would be.")
    if len({round(r["through"], 4) for r in scored}) == 1:
        print("  ! every scored board came out the SAME. They hold the same")
        print("    measurable pieces, so this method cannot separate them —")
        print("    the difference between them is in the pieces it cannot see.")
    by_score = max(scored, key=lambda r: (r["candidate"]["interruptions"],
                                          r["candidate"]["negates"]))
    if by_score is not best:
        print(f"  ! the heuristic score prefers [{by_score['candidate']['label']}]"
              f" ({by_score['candidate']['interruptions']} int), which lets "
              f"{by_score['through']:.0%} through. Counting interruptions and")
        print(f"    measuring them disagree here, which is why this exists.")
    untested_n = len(best["untested"]) + len(
        {c for cell in best["per_deck"].values()
         for c in (cell.get("no_cell") or ())})
    if untested_n:
        print(f"  ! {untested_n} piece(s) of the winning board were not "
              f"measured. The percentage is what gets through the pieces it")
        print(f"    could see, NOT through the board.")
    print("  ! the per-board figure multiplies its pieces, which assumes they")
    print("    are independent. They are not — a handtrap landing shortens "
          "the line")
    print("    the next one faces — so it is a lower bound on the board.")
    path = gauntlet.save({"deck": deck_path.stem, "hand": list(hand),
                          "matrix": str(path.name), "coverage": share,
                          "results": rows},
                         ROOT / "results" / f"gauntlet_{deck_path.stem}.json")
    print(f"  written to {path}")


def _trial_change_text(conn, row):
    """What a trial changed, from its stored decklist.

    This used to be parsed out of the trial's FILENAME, and the filenames
    are not all the same shape: trial names gained the cut partway
    through the project, so `<deck>_r<round>_<add>` from the earlier runs
    rendered as a bare "+Card" -- an addition with nothing cut, which
    would be a 45-card deck and was never what ran. The .ydk next to each
    trial says exactly what it did, so `optimizer.trial_change` reads
    that and the filename is no longer load-bearing.
    """
    if row["label"] == "base":
        return "(your current list)"
    cut, add = row.get("cut"), row.get("add")
    if cut is None and add is None:
        return row["label"]          # no .ydk kept; say nothing we cannot show
    parts = [f"-{handlab._name(conn, c)}" for c in (cut or [])]
    parts += [f"+{handlab._name(conn, c)}" for c in (add or [])]
    return "  ".join(parts) if parts else "(no change)"


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

    # Same check `optimize` makes, on the same data, through the same
    # function. `rescore` prints the same kind of ordering and had no
    # check at all -- the guard was added to one of the two commands that
    # rank trials. Measured on the stored kewl-tune run: brick rate,
    # solve rate and handtrap survival are each ONE value across all 10
    # trials, so the ordering there rests entirely on combo length
    # (weight -0.05) and window recovery (0.5), and its top swap beats
    # the current list by 0.006.
    constant, varying = optimizer.degenerate_terms(
        [r["summary"] for r in rows])
    if len(varying) <= 1:
        only = varying[0] if varying else "nothing"
        print(f"  !! NO RANKING. {len(constant)} of 5 objective terms did "
              f"not vary over {len(rows)} stored trial(s):")
        print(f"     {', '.join(constant)}")
        print(f"     Only {only} moved, and that is a tiebreak, not a "
              f"measurement.")
        print(f"     Re-run `optimize` at more hands. Trials below are "
              f"listed by name, NOT ranked:\n")
        rows = sorted(rows, key=lambda r: _trial_change_text(conn, r))
    elif constant:
        print(f"  ! {len(constant)} of 5 terms did not vary at all: "
              f"{', '.join(constant)}.")
        print(f"    The order below is decided by the {len(varying)} that "
              f"did ({', '.join(varying)}).\n")

    print(f"  {'score':>7} {'brick':>6} {'solve':>6} {'surv':>5} "
          f"{'combo':>6} {'recov':>6}  change")
    for r in rows:
        shown = _trial_change_text(conn, r)
        if r.get("inconsistent"):
            shown += f"  [!{r['inconsistent']} runs DISAGREE]"
        elif r.get("measured", 1) > 1:
            shown += f"  (measured {r['measured']}x, same result)"
        print(f"  {r['score']:+7.3f} {(r['brick_rate'] or 0):6.2f} "
              f"{(r['best_line_rate'] or 0):6.2f} {(r['survival'] or 0):5.2f} "
              f"{(r['combo_mean'] or 0):6.2f} {r['window_recovery']:6.3f}  "
              f"{shown}")
    if len(varying) <= 1:
        pass          # refused above; naming a "best" would undo the refusal
    elif rows[0]["label"] == "base":
        print("\n  Your current list scores highest — none of the tested swaps "
              "improved it.")
    else:
        # Was printing the raw trial label ("kewl-tune_r0_49036338") while
        # the table above rendered the same row as a readable swap.
        print(f"\n  Best stored swap: {_trial_change_text(conn, rows[0])}. "
              f"Re-run `optimize` at more hands before acting on it.")
    print("  Weights are judgement calls. Pass a recovery weight to see how "
          "the order moves.")


def cmd_draws(conn, args):
    """What each extra draw bought, per deck, from a finished sweep.

    The budget has `ladder`; `draws` had nothing, so `sweep.DRAWS` was one
    guess applied to every deck. Measured: four draws bought +0.10 mean
    interruptions on despia for 2.6x the solver time, and far more on an
    elfnote hand. Costs no solver time -- every record stores each attempt
    with its draw index.
    """
    name = None
    if args:
        path = _resolve_deck(args[0], conn)
        name = path.stem if path else args[0]
    sources = sorted((ROOT / "results").rglob("*.jsonl"),
                     key=lambda p: -p.stat().st_mtime)
    for src in sources:
        try:
            curve = sweep.draws_curve(src, name)
        except (OSError, ValueError):
            continue
        if curve:
            print(f"  from {src.name}\n")
            for deck, row in sorted(curve.items()):
                solved = row["hands"] - row["unsolved"]
                print(f"  {deck}")
                print(f"    {row['hands']} hands, {row['solves']} solves, "
                      f"mean best {row['mean_best_solved']} interruptions "
                      f"per SOLVED hand")
                if row["unsolved"]:
                    print(f"    {row['unsolved']} hand(s) never solved at all — "
                          f"excluded below, and {row['mean_best']} counting "
                          f"them as zero")
                print(f"    hands at their best board by end of draw "
                      f"(of {solved} that solved):")
                for k, v in row["at_draw"].items():
                    print(f"       draw {k:>2}  {v}")
                print(f"    solves after the best was already found: "
                      f"{row['wasted']} ({row['wasted_share']:.0%})")
                # On the COUNTS, never on the rendered "n/total" strings. The
                # denominator used to move -- hands that stop early dropped out
                # of it rather than carrying forward -- so branded compared
                # "40/40" against "31/31", called them different, and reported
                # that later draws kept improving when in fact every hand had
                # peaked in the breadth round and 372 of its 655 solves bought
                # nothing.
                counts = list(row["peaked"].values())
                if counts and counts[0] == counts[-1]:
                    print(f"    -> every hand peaked in the breadth round. "
                          f"draws=1 would have given the same boards here.")
                else:
                    gained = counts[-1] - counts[0] if counts else 0
                    print(f"    -> {gained} more hand(s) reached their best "
                          f"board after draw {row['draw_keys'][0]}, so draws "
                          f"is earning its budget on this deck.")
                print()
            return
    print("no sweep records with per-draw attempts yet — run `solve` first")


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
    print(f"  ! UNSUPPORTED: {breaking.UNSUPPORTED}\n")
    rows = breaking.going_second(theirs, ours, their_target, our_target,
                                 hands=hands)

    def describe(cell):
        if cell is None:
            return "not run"
        if cell.get("error"):
            return f"NOT MEASURED — {cell['error'][:60]}"
        if not cell.get("searched"):
            return "NO SEARCH (misconfigured)"
        return "got through" if cell["through"] else "stopped"

    through = ctrl_through = measured = 0
    for r in rows:
        board, ctrl = r.get("board"), r.get("control")
        bits = f"  hand {r['seed']}: vs their board {describe(board)}"
        if board and not board.get("error"):
            bits += (f"  (board {board.get('board_cards')} card(s), we hold "
                     f"{board.get('attacker_hand')})")
        print(bits)
        print(f"            vs an empty field  {describe(ctrl)}")
        if board and not board.get("error") and board.get("searched"):
            measured += 1
            through += bool(board["through"])
            if ctrl and ctrl.get("searched"):
                ctrl_through += bool(ctrl["through"])
    print(f"\n  measured on {measured}/{len(rows)} hand(s): got through their "
          f"board {through}, through an empty field {ctrl_through}")
    # The paired control is the only thing that gives the first number a
    # scale. Same decks, same seed, same cards dealt to us -- only the
    # board differs -- so if an empty field stops as many hands, the
    # obstacle is our own turn-2 search and not their board.
    if measured and ctrl_through <= through:
        print("  ! an EMPTY FIELD stopped at least as many hands as their")
        print("    board, so nothing here is a measurement OF the board.")


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
    def load(deck_id, name="x"):
        secs = {"main": [], "extra": [], "side": []}
        for r in conn.execute(
            "SELECT card_id, section, copies FROM deck_cards WHERE deck_id = ?",
            (deck_id,),
        ):
            secs[r["section"]] += [r["card_id"]] * r["copies"]
        return Deck(name=name, **secs)

    # Usable lists first: a pre-banlist Winner is not what you will face, and
    # a list with cards the engine has no script for cannot be simulated
    # (the newest Angelechy support was unscripted even upstream, 2026-11).
    from skills import carddb

    def problems_of(i):
        d = load(i)
        out = d.validate(conn)
        miss = carddb.missing_codes(set(d.main + d.extra))
        if miss:
            out.append(f"{len(miss)} card(s) the engine cannot load: "
                       + ", ".join(map(str, sorted(miss))))
        return out

    problems = {}
    row = ms.best_list(conn, archetype, legal=lambda i: not problems.setdefault(
        i, problems_of(i)))
    if not row:
        print(f"no stored lists tagged {archetype!r}")
        return
    print(f"  best placing of the stored {archetype!r} lists: "
          f"{row['placement'] or 'placement unknown'}")
    if problems.get(row["id"]):
        print("  ! no stored list is both legal today and loadable by the engine; "
              "exporting the best anyway:")
        for p in problems[row["id"]]:
            print(f"    {p}")
    else:
        skipped = sum(1 for v in problems.values() if v)
        if skipped:
            print(f"  ({skipped} other list(s) skipped: not legal on today's "
                  f"banlist, or cards the engine cannot load)")
    name = args[1] if len(args) > 1 else archetype.lower().replace(" ", "-")
    d = load(row["id"], name)
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
        # The HONEST survival, from the per-hand records, not
        # `summary.survival_given_line` -- that field counts a hand where
        # the card had no legal window as a survival, so it never reads
        # lower. This block was the costly place: the verdict and "your
        # worst matchup card" were both computed from the inflated
        # number, so Nibiru at a summary 91% was labelled "fine" while
        # the deck beat it in 0 of the 3 hands where it was live.
        cells = r.get("survival_tested") or {}
        surv_rows = []
        for code in (f.get("survival_given_line") or {}):
            cell = cells.get(int(code)) or {}
            surv_rows.append((card_db.resolve(conn, int(code)), code,
                              cell.get("survival"),
                              cell.get("tested") or 0,
                              cell.get("offered") or 0))
        live = [t for t in surv_rows if t[2] is not None and t[3]]
        unmeasured = [t for t in surv_rows if t[2] is None or not t[3]]
        # Bound before `if live:`, not inside it. The blind-spot loop
        # below reads it whenever there are UNMEASURED rows, so a deck
        # with no live handtrap and at least one unmeasured one raised
        # UnboundLocalError -- `report Blitzclique` did exactly that.
        blind = []
        if live:
            print("\n  PLAYS THROUGH (higher is better — this is YOUR deck)")
            print("    % is of the hands where the card was LIVE, not of all hands")
            for row, code, sv, tested, offered in sorted(live,
                                                         key=lambda t: t[2]):
                name = row["name"] if row else code
                if int(code) in scenarios.BLIND_SPOTS:
                    blind.append((name, int(code)))
                    verdict = "NOT MEASURABLE"
                else:
                    verdict = ("folds to it" if sv <= 0.15 else
                               "struggles" if sv <= 0.5 else "fine")
                print(f"    {sv*100:4.0f}%  {name:<30} "
                      f"live {tested:>2}/{offered:<3} {verdict}")
            judged = [t for t in live
                      if int(t[1]) not in scenarios.BLIND_SPOTS]
            if judged:
                # Ties are the common case, not an edge: branded folds to
                # Imperm, Ash AND Veiler at 0%, and `min` named whichever
                # the stored record happened to list first -- a card
                # picked by JSON key order, presented as the answer.
                floor = min(t[2] for t in judged)
                tied = [t for t in judged if t[2] == floor]
                names = [(t[0]["name"] if t[0] else str(t[1])) for t in tied]
                if len(names) == 1:
                    print(f"    -> {names[0]} is your worst matchup card")
                else:
                    print(f"    -> {len(names)} cards tie as your worst "
                          f"matchup at {floor*100:.0f}%: "
                          f"{', '.join(sorted(names))}")
        for row, code, _sv, tested, offered in unmeasured:
            uname = (row["name"] if row else code)
            print(f"    n/a   {uname:<30} never had a legal window in "
                  f"{offered} solved hand(s) — UNMEASURED")
        # Dedented out of the loop above, where it had been printing once
        # per unmeasured row (branded printed the Maxx "C" note twice) and
        # not at all for a deck with no unmeasured rows -- which is every
        # deck whose handtraps were all live, i.e. the ones where the
        # blind spot most needed saying. It also shadowed that loop's
        # `name` and `code`.
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
        # The "never had a window" rows are printed by the `unmeasured`
        # loop above, which names how many solved hands the card was
        # absent from. This was a second, stale copy left behind by the
        # survival fix (#49) and it still unpacked the OLD 3-tuple, so
        # `report` raised ValueError. The dispatcher catches and prints
        # the traceback, so the suite stayed green while the project's
        # headline command was broken -- which is why there is now a CLI
        # smoke walk (#58) to match the web's route walk.
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
        # `identify_archetype` returns (archetype, overlap, n_core,
        # n_lists); this unpacked the first three and raised ValueError on
        # every deck with an alternative guess. Indexed rather than
        # unpacked because this is the SECOND time a widened tuple broke a
        # stale call site in `report` -- #49 did it to surv_rows -- and
        # indexing cannot break that way again.
        for guess in (r.get("archetype_alts") or []):
            alt, ov, n_core = guess[0], guess[1], guess[2]
            n_lists = guess[3] if len(guess) > 3 else None
            seen = f", {n_lists} lists" if n_lists else ""
            print(f"    also matches {alt} ({ov*100:.0f}% of {n_core} "
                  f"core{seen})")
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
    problems = []
    for src in sources:
        try:
            written = drill.export(conn, src, path.stem, limit=limit)
        except Exception as exc:
            # A broad `except: continue` here read a REAL error in
            # drill.export -- a bug, a bad record, a vendor change -- as
            # "this source has no lines", silently, for every source in
            # turn. The loop still moves on, because a malformed record
            # genuinely should not stop the search, but the failures are
            # now reported if nothing is found.
            problems.append((src.name, f"{type(exc).__name__}: {exc}"))
            continue
        if written:
            print(f"  from {src.name}")
            break
    if not written and problems:
        print(f"  {len(problems)} source(s) raised rather than being empty:")
        for name, why in problems[:4]:
            print(f"    {name}: {why}")
    if not written:
        print(f"no solved lines stored for {path.stem} — run `solve` or `gate` first")
        return

    for yrpx, txt, n, on_field in written:
        # Ranked on ON-FIELD interruptions, so printing the flat count
        # alone put "4 interruptions" above "5 interruptions" three times
        # in the active deck's 80 lines. Lead with what the order is by.
        extra = (f", {n} counting cards held in hand" if n != on_field else "")
        print(f"\n  {on_field} on field{extra} -> {yrpx.name}")
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


def cmd_train(conn, args):
    """The deck trainer (skills/trainer.py): `train <deck> [hours] [hands]
    [enemy] [--seconds N] [--second]`, `train <deck> status`, `train <deck>
    stop`. `--second`: the games at the plateau are played going second.
    `--vs <deck>`: train the hands <deck> is dealt as the enemy of that
    deck in the games (`trainer.ensure_enemy_pool`); with status, read them."""
    args = list(args)
    solve_ms = None
    first = "--second" not in args
    args = [a for a in args if a != "--second"]
    vs = None
    if "--vs" in args:
        at = args.index("--vs")
        vs = _resolve_deck(args[at + 1] if at + 1 < len(args) else None, conn)
        if not vs:
            print("--vs needs the deck these are the enemy's hands against")
            return
        del args[at:at + 2]
    if "--seconds" in args:
        at = args.index("--seconds")
        try:
            solve_ms = int(float(args[at + 1]) * 1000)
        except (IndexError, ValueError):
            print("--seconds needs a number")
            return
        if not 10_000 <= solve_ms <= 1_800_000:
            print("--seconds must be between 10 and 1800")
            return
        del args[at:at + 2]
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: train <deck> [hours] [hands] | train <deck> status | stop")
        return
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    name, rest = path.stem, args[1:]
    if rest and rest[0] == "stop":
        trainer.request_stop(name)
        print(f"{name}: the trainer will stop after its current generation")
        return
    if rest and rest[0] == "status":
        dh = trainer.deck_hash(path)
        if vs:
            dh = trainer.enemy_key(dh, trainer.deck_hash(vs))
        gens = trainer.curve(conn, dh)
        if not gens:
            print(f"{name}: never trained")
            return
        print(f"{name}: {len(gens) - 1} generation(s)")
        for g in gens:
            print(f"  gen {g['gen']:>3}  {g['solves']:>4} solves  "
                  f"{g['improved']:>3} improved  {g['solved_hands']}/{g['pool']} solved  "
                  f"mean {g['mean_on_field']:.2f} on field, "
                  f"{g['mean_interruptions']:.2f} interruptions")
        return
    _warn_if_busy("training")
    hours = float(rest[0]) if rest else 8.0
    hands = int(rest[1]) if len(rest) > 1 else (game.ENEMY_HANDS if vs else trainer.HANDS)
    enemy = rest[2] if len(rest) > 2 else None
    if vs:
        if enemy:
            print("--vs trains the enemy's hands; it plays no games")
            return
        print(f"training {name}'s {hands} game hands against {vs.stem}, up to {hours:g} h "
              f"(stop early: train {name} stop)")
        n = trainer.train(conn, path, name, hours=hours, hands=hands, solve_ms=solve_ms,
                          plateau=PLATEAU, vs=vs)
        print(f"{n} generation(s) run")
        return
    print(f"training {name}: {hands} hands, up to {hours:g} h "
          f"(stop early: train {name} stop)"
          + (f"; at a plateau, games against {enemy}" if enemy else ""))
    n = trainer.train(conn, path, name, hours=hours, hands=hands,
                      solve_ms=solve_ms, plateau=PLATEAU if enemy else None)
    print(f"{n} generation(s) run")
    # Only a plateau starts the games. `train` clears the stop flag before it
    # returns, so asking it whether a stop was requested always said no and
    # a stopped training went straight into hours of games.
    if enemy and trainer.plateaued(conn, trainer.deck_hash(path), PLATEAU):
        _print_games(game.run(conn, path, name, enemy, first=first), first)
    elif enemy:
        print(f"no plateau yet (stopped or out of time): games against {enemy} "
              f"not started -- `games {name} {enemy}"
              f"{'' if first else ' --second'}` plays them anyway")


# Generations in a row with no hand improved before `train ... <enemy>`
# calls the deck as good as it will get and starts the games.
PLATEAU = trainer.PLATEAU


def _print_games(rows, first=True):
    print("\nwin rate by turn 3 (going first):" if first else
          "\nwin rate on turn 2 (going second, through their turn-1 board):")
    for r in rows:
        tier = f"  {r['tier'].upper()}" if r["tier"] else ""
        print(f"  hand {r['hand_index'] + 1:>2}  {r['wins']:>2}/{r['games']:<2} "
              f"{r['win_rate']:>5.0%} ({r['ci'][0]:.0%}-{r['ci'][1]:.0%})"
              + (f"  lost on turn 2: {r['lost_t2']}" if first
                 else f"  best damage: {r['best_damage'] or 0}")
              + tier + ("" if r["complete"] else "  (incomplete)"))


def _device_arg(args):
    """(`--device NAME` value or None, args without it)."""
    args = list(args)
    if "--device" not in args:
        return None, args
    i = args.index("--device")
    dev = args[i + 1] if i + 1 < len(args) else ""
    del args[i:i + 2]
    return dev, args


def _sides(conn, dh, args):
    """[(enemy, first, device)] a deck has games for, narrowed to `args[0]`
    (an enemy), `--first` / `--second` and `--device NAME` when given."""
    device, args = _device_arg(args)
    side = (1 if "--first" in args else 0 if "--second" in args else None)
    args = [a for a in args if a not in ("--first", "--second")]
    rows = conn.execute("SELECT DISTINCT enemy, first, device FROM game_results WHERE "
                        "deck_hash=? ORDER BY enemy, first DESC, device", (dh,)).fetchall()
    return [(e, f, d) for e, f, d in rows
            if (not args or e == args[0]) and (side is None or f == side)
            and (device is None or d == device)]


def cmd_starters(conn, args):
    """The starter test: `starters <deck> <1-3> [hours] [--seconds N]
    [--skip code,code] [--with-handtraps]` trains every k-card hand the deck
    can hold; `starters <deck> <k> status` prints the best of them."""
    args = list(args)
    solve_ms, skip, with_ht = trainer.STARTER_MS, None, False
    if "--with-handtraps" in args:
        args.remove("--with-handtraps")
        with_ht = True
    for flag in ("--seconds", "--skip"):
        if flag in args:
            at = args.index(flag)
            try:
                val = args[at + 1]
                if flag == "--seconds":
                    solve_ms = int(float(val) * 1000)
                    if not 10_000 <= solve_ms <= 1_800_000:
                        raise ValueError
                else:
                    skip = {int(c) for c in val.split(",") if c}
            except (IndexError, ValueError):
                print("--seconds takes 10-1800, --skip a comma list of card codes")
                return
            del args[at:at + 2]
    if len(args) < 2 or args[1] not in {str(k) for k in trainer.STARTER_SIZES}:
        print("usage: starters <deck> <1-3> [hours] [--seconds N] [--skip codes] "
              "[--with-handtraps] | starters <deck> <k> status")
        return
    path = _resolve_deck(args[0], conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    name, k, rest = path.stem, int(args[1]), args[2:]
    if skip is None:
        skip = trainer.starter_skip(path, with_ht)
    if rest and rest[0] == "status":
        from skills import card_db
        rows = sorted(trainer.hands_table(conn, trainer.starter_key(trainer.deck_hash(path), k)),
                      key=trainer.rank)
        if not rows:
            print(f"{name}: no {k}-card starter test yet")
            return
        for r in rows[:25]:
            if not r["key"]:
                break
            print(f"  {r['on_field']:>4} on field  {r['interruptions']:>4} int  "
                  + " + ".join(r["hand"]))
        dead = sum(1 for r in rows if not r["key"])
        print(f"{dead} of {len(rows)} hand(s) made no board")
        return
    hours = float(rest[0]) if rest else 8.0
    n, est = trainer.starter_estimate(path, k, skip, solve_ms)
    _warn_if_busy("the starter test")
    print(f"starter test {name}: {n} {k}-card hand(s), {solve_ms // 1000} s a try, "
          f"first generation ~{est:.1f} h, up to {hours:g} h (stop: train {name} stop)")
    ran = trainer.train(conn, path, name, hours=hours, solve_ms=solve_ms,
                        plateau=PLATEAU, starters=k, skip=skip)
    print(f"{ran} generation(s) run")
    cmd_starters(conn, [name, str(k), "status"])


def cmd_impact(conn, args):
    """`impact <deck> [enemy] [--first|--second]`: read every game not read
    yet, then print each card's impact on the win rate (skills/impact.py),
    per enemy deck and per side."""
    from skills import card_db, impact

    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print("usage: impact <deck> [enemy] [--first|--second]")
        return
    dh, name = trainer.deck_hash(path), path.stem
    sides = _sides(conn, dh, args[1:])
    if not sides:
        print(f"no games played for {name} yet: games {name} <enemy>")
        return
    for enemy, first, device in sides:
        impact.fill(conn, dh, name, enemy, say=print, first=first, device=device)
        rows = impact.scores(conn, dh, enemy, own=impact.deck_codes(path), first=first,
                             device=device)
        print(f"\n{name} vs {enemy} going {'first' if first else 'second'} on {device}: "
              f"{rows[0]['games'] if rows else 0} game(s) read")
        for r in rows:
            nm = (card_db.resolve(conn, r["code"]) or {"name": str(r["code"])})["name"]
            if r["status"] == "every game":
                print(f"  {'every game':>14}  {nm}  (used in {r['used']}, never without)")
            else:
                flag = "  few games" if r["status"] == "few games" else ""
                print(f"  {r['impact'] * 100:+6.0f} ±{r['ci'] * 100:3.0f} pts  {nm}  "
                      f"(won {r['used_win']:.0%} used / {r['not_win']:.0%} without, "
                      f"{r['compared']} compared){flag}")


def cmd_stops(conn, args):
    """`stops <deck> [enemy] [--first|--second]`: which of the enemy's cards
    you stopped, with which of yours, and how much each stop was worth to
    the win rate (skills/stops.py), per enemy deck and per side."""
    from skills import card_db, impact, stops

    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print("usage: stops <deck> [enemy] [--first|--second]")
        return
    dh, name = trainer.deck_hash(path), path.stem
    sides = _sides(conn, dh, args[1:])
    if not sides:
        print(f"no games played for {name} yet: games {name} <enemy>")
        return
    nm = lambda code: ("a field effect" if code == stops.FIELD else "a set card" if not code
                       else (card_db.resolve(conn, code) or {"name": str(code)})["name"])
    score = lambda r: ("every game" if r["status"] == "every game" else
                       f"{r['impact'] * 100:+4.0f} ±{r['ci'] * 100:3.0f}")
    flag = lambda r: "  few games" if r["status"] == "few games" else ""
    for enemy, first, device in sides:
        stops.fill(conn, dh, name, enemy, say=print, first=first, device=device)
        res = stops.scores(conn, dh, enemy, own=impact.deck_codes(path), first=first,
                           device=device)
        print(f"\n{name} vs {enemy} going {'first' if first else 'second'} on {device}: "
              f"{res['games']} game(s) read")
        print("\n  THEIR cards -- win-rate points when stopped vs when it went through")
        for r in res["theirs"]:
            rate = (f"won {r['stopped_win']:.0%} stopped / "
                    + (f"{r['through_win']:.0%} through" if r["through_win"] is not None
                       else "never through"))
            by = ", ".join(f"{nm(b)} {n}" for b, n in r["by"][:3])
            print(f"  {score(r):>14}  {nm(r['code'])}  (stopped {r['stopped']} of "
                  f"{r['played']} games played, turn {r['turn']}; {rate}){flag(r)}")
            print(f"  {'':>14}    by {by}")
        print("\n  YOUR cards -- win-rate points in games it stopped something vs not")
        for r in res["yours"]:
            hit = ", ".join(f"{nm(c)} {n}" for c, n in r["hit"][:3])
            print(f"  {score(r):>14}  {nm(r['code'])}  ({r['stops']} stop(s) in "
                  f"{r['games_with']} games, turn {r['turn']}){flag(r)}")
            print(f"  {'':>14}    stopped {hit}")


def cmd_games(conn, args):
    """`games <deck> <enemy> [runs] [enemy hands] [--second] [--workers N]
    [--from-end] [--device NAME]` / `games <deck> <enemy> status [--second]
    [--device NAME]`: your turn 1,
    their turn 2, your turn 3 -- does the hand win by then? `--second`: their
    turn 1, your turn 2 -- do you kill through their board? `--from-end`
    plays the list backwards, so a second runner beside a running one takes
    the games it has not reached. `--device`: run the searches on another
    machine (skills/remote.py; default the server)."""
    from skills import remote

    device, args = _device_arg(args)
    if device is not None and device not in remote.DEVICES:
        print(f"no such device: {device} (known: {', '.join(remote.DEVICES)})")
        return
    if len(args) < 2:
        print("usage: games <deck> <enemy> [runs] [enemy hands] [--second] [--workers N] [--from-end] | games <deck> <enemy> status [--second]")
        return
    path = _resolve_deck(args[0], conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    enemy, rest = args[1], args[2:]
    from_end = "--from-end" in rest
    first = "--second" not in rest
    rest = [a for a in rest if a not in ("--from-end", "--second", "--first")]
    workers = None      # the device's own default
    if "--workers" in rest:
        i = rest.index("--workers")
        try:
            workers = int(rest[i + 1])
        except (IndexError, ValueError):
            print("--workers takes a whole number")
            return
        if not 1 <= workers <= 16:
            print("--workers must be 1-16")
            return
        del rest[i:i + 2]
    if not (DECKS / f"{enemy}.ydk").exists():
        print(f"no such enemy deck: {enemy}")
        return
    if rest and rest[0] == "status":
        _print_games(game.summary(conn, trainer.deck_hash(path), enemy,
                                  first=first, device=device or "server"), first)
        return
    try:
        runs = int(rest[0]) if rest else game.RUNS
        ehands = int(rest[1]) if len(rest) > 1 else game.ENEMY_HANDS
    except ValueError:
        print("runs and enemy hands are whole numbers: games <deck> <enemy> [runs] [enemy hands]")
        return
    if runs < 1 or ehands < 1:
        print("runs and enemy hands must be at least 1")
        return
    if not device or device == "server":
        _warn_if_busy("these games")
    try:
        res = game.run(conn, path, path.stem, enemy, runs=runs, enemy_hands=ehands,
                       workers=workers, from_end=from_end, first=first, device=device)
    except remote.DeviceDown as e:
        # SystemExit, not an exception: `run` below prints and swallows
        # those, and the job would end "done" with its games unplayed.
        print(f"device went away: {e}; the finished games are kept")
        raise SystemExit(remote.EXIT_DOWN)
    _print_games(res, first)


COMMANDS = {
    "cards": cmd_cards,
    "images": cmd_images,
    "pilot": cmd_pilot,
    "meta": cmd_meta,
    "archetypes": cmd_archetypes,
    "flex": cmd_flex,
    "hand": cmd_hand,
    "check": cmd_check,
    "banlist": cmd_banlist,
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
    "draws": cmd_draws,
    "floodgates": cmd_floodgates,
    "train": cmd_train,
    "games": cmd_games,
    "impact": cmd_impact,
    "stops": cmd_stops,
    "starters": cmd_starters,
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
