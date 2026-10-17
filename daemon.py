#!/usr/bin/env python3
"""duelroom — Yu-Gi-Oh! deck laboratory. Interactive CLI.

Stage 1 commands only (meta ingestion and exact hand math). The engine-backed
commands arrive with Stage 2+.
"""

import shlex
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills import card_db, db, engine, meta_scrape as ms, stats
from skills.deck import Deck

ROOT = Path(__file__).resolve().parent
DECKS = ROOT / "assets" / "decks"

BANNER = """\
duelroom — Yu-Gi-Oh! deck laboratory

  cards                     refresh the card mirror from YGOPRODeck
  meta [days] [n]           scrape recent tournament lists (default 120 days, 500)
  archetypes [n]            what is being played, by tournament presence
  flex <archetype> [side]   core / flex / fringe breakdown
  hand <deck.ydk>           exact opening-hand math
  check <deck.ydk>          legality and banlist check
  decks                     list .ydk files in assets/decks/
  export <archetype> [name] write a scraped list out as a .ydk
  engine                    engine setup status and pinned versions
  solve <deck> [hands]      turn-1 line search: solve rate + board quality
  help / quit
"""


def _since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def _resolve_deck(arg):
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
        print("usage: flex <archetype> [main|side|extra]")
        return
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
    if not args:
        print("usage: hand <deck.ydk>")
        return
    path = _resolve_deck(args[0])
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
    if not args:
        print("usage: check <deck.ydk>")
        return
    path = _resolve_deck(args[0])
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
    if not args:
        print("usage: solve <deck.ydk> [hands]")
        return
    path = _resolve_deck(args[0])
    if not path:
        print(f"no such deck: {args[0]}")
        return
    hands = int(args[1]) if len(args) > 1 else 8
    print(f"solving {hands} opening hands for {path.stem} "
          f"({engine.DEFAULT_WORKERS} workers, {engine.SOLVE_MS/1000:.0f}s/hand budget)")
    print("this is CPU-bound and not fast — see STAGE3.md on budgets\n")
    summary = engine.solve_blocking(path, hands=hands)
    for row in engine.summarise(summary):
        print(f"\n  {row['deck']}")
        print(f"    solve rate        {row['solved']}/{row['hands']} "
              f"({row['solve_rate']*100:.0f}%)")
        print(f"    mean interrupts   {row['mean_interruptions']:.2f}")
        print(f"    per hand          {row['s_per_hand']:.1f}s")
        print(f"    distribution      {row['interruptions']}")


def cmd_export(conn, args):
    if not args:
        print("usage: export <archetype> [filename]")
        return
    archetype = args[0]
    row = conn.execute(
        """SELECT d.id, d.title, d.placement FROM decks d
             JOIN deck_tags t ON t.deck_id = d.id
            WHERE t.tag = ? ORDER BY d.id LIMIT 1""",
        (archetype,),
    ).fetchone()
    if not row:
        print(f"no stored lists tagged {archetype!r}")
        return
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
    "meta": cmd_meta,
    "archetypes": cmd_archetypes,
    "flex": cmd_flex,
    "hand": cmd_hand,
    "check": cmd_check,
    "decks": cmd_decks,
    "export": cmd_export,
    "engine": cmd_engine,
    "solve": cmd_solve,
}


def main():
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
