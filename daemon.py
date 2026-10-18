#!/usr/bin/env python3
"""duelroom — Yu-Gi-Oh! deck laboratory. Interactive CLI.

Stage 1 commands only (meta ingestion and exact hand math). The engine-backed
commands arrive with Stage 2+.
"""

import json
import shlex
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills import (breaking, card_db, carddb, config, db, engine, gate,
                    importer, meta_scrape as ms, optimizer, scenarios,
                    stats)
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
  import <src> [name]       add a deck: .ydk path, ydke:// URI, or ygoprodeck URL
  mydeck [name]             show or set the deck commands default to
  ydke <deck>               export a deck as a ydke:// URI to paste elsewhere
  export <archetype> [name] write a scraped list out as a .ydk
  setup                     build the merged engine card database
  engine                    engine setup status and pinned versions
  solve <deck> [hands]      turn-1 line search: solve rate + board quality
  ladder <deck> [hands]     calibrate the solver budget (run this first)
  gate <deck> [hands] [ms]  validation gate: solve rate + every brick hand
  boards [load]             meta end-board library (what you must break)
  threats                   interruption counts + common pieces per archetype
  targets                   derive solver targets from the board library
  matrix                    handtrap matrix: which handtraps to main deck
  locals [auto [rogue%]]    your locals.toml weights; `auto` fills from the scrape
  optimize <deck> <arch>    hill-climb the flex slots (slow: hours)
  second <theirs> <ours>    going second into their real board
  floodgates <deck>         does the deck function under common floodgates
  help / quit
"""


def _since(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


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
    path = _resolve_deck(args[0] if args else None, conn)
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


def cmd_ladder(conn, args):
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: ladder <deck.ydk> [hands]")
        return
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
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    hands = int(args[1]) if len(args) > 1 else 12
    ms = int(args[2]) if len(args) > 2 else engine.SOLVE_MS
    print(f"gate: {path.stem}, {hands} hands, {ms/1000:.0f}s budget\n")
    rec = gate.run_funnel(path, hands=hands, solve_ms=ms)
    rep = gate.gate_report(rec, conn)

    lo, hi = rep["solve_rate_ci"]
    print(f"  solved      {rep['solved']}/{rep['hands']} "
          f"({rep['solve_rate']*100:.0f}%, 95% CI {lo*100:.0f}-{hi*100:.0f}%)")
    print(f"  solver time {rep['solver_s']:.0f}s")
    ca = rep["combo_actions"]
    if ca:
        print(f"  line length min {ca.get('min')}, median {ca.get('median')}, "
              f"max {ca.get('max')}")
    for code, surv in (rep["survival_given_line"] or {}).items():
        row = card_db.resolve(conn, int(code))
        print(f"  survives {row['name'] if row else code}: {surv*100:.0f}% of solved hands")

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
        print(f"\n  {archetype}  (mean {mean:.1f} interruptions)")
        for b in boards:
            print(f"    {b['frequency']*100:4.0f}%  {b['negates']} interrupts")
            for i in b["interruptions"]:
                print(f"            {i}")


def cmd_threats(conn, args):
    prof = gate.threat_profile(conn)
    if not prof:
        print("board library empty — run `boards load`")
        return
    counts = sorted({n for d in prof.values() for n in d})
    print("  share of end boards by interruption count\n")
    print("  " + "archetype".ljust(28) + "".join(f"{n:>7}" for n in counts) + "    mean")
    for arch, dist in sorted(prof.items(),
                             key=lambda kv: -sum(k*v for k, v in kv[1].items())):
        mean = sum(k * v for k, v in dist.items())
        cells = "".join(f"{dist.get(n,0)*100:6.0f}%" for n in counts)
        print(f"  {arch.ljust(28)}{cells}  {mean:6.1f}")

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
    path = ROOT / "results" / "handtrap_matrix.jsonl"
    if not path.exists():
        print(f"no {path} — run scenarios.run_matrix first")
        return
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
    print("  " + "deck".ljust(26) + "".join(f"{short[c]:>10}" for c in codes))
    for deck, data in sorted(m.items()):
        cells = ""
        for c in codes:
            cell = data["cells"].get(c)
            v = cell["survival"] if cell else None
            cells += "         -" if v is None else f"{v*100:9.0f}%"
        print(f"  {deck.ljust(26)}{cells}")

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
    if len(args) < 2:
        print("usage: optimize <deck.ydk> <archetype> [hands] [rounds]")
        return
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    archetype = args[1]
    hands = int(args[2]) if len(args) > 2 else 8
    rounds = int(args[3]) if len(args) > 3 else 2

    deck = Deck.from_ydk(path)
    deck.canonicalise(conn)
    pool = optimizer.candidate_pool(conn, archetype, deck)
    outs = optimizer.removable(conn, deck, archetype)
    if not pool:
        print(f"no flex/fringe pool for {archetype!r} — run `meta` and check `archetypes`")
        return
    print(f"{deck.name}: {len(outs)} non-core slots, {len(pool)} candidates")
    print(f"each trial is one funnel_eval run — {hands} hands at "
          f"{engine.SOLVE_MS/1000:.0f}s. This takes hours, not minutes.\n")

    tgt = json.loads((ROOT / "assets" / "derived_targets.json").read_text())
    if path.stem not in tgt:
        print(f"no derived target for {path.stem} — run `targets` first")
        return
    target = tgt[path.stem]["targets"][0]
    print(f"holding the target fixed at {target} across all trials\n")
    best, history = optimizer.hill_climb(conn, deck, archetype, target,
                                         hands=hands, rounds=rounds)
    print(f"\nbest: {best.label}  score {best.score:+.3f}")
    print(f"\n  {'score':>7}  {'solve':>16}  {'brick':>6}  {'surv':>6}  change")
    for r in rows[:12]:
        lo, hi = r["ci"]
        print(f"  {r['score']:+7.3f}  {r['solve_rate']*100:4.0f}% "
              f"[{lo*100:3.0f}-{hi*100:3.0f}]  "
              f"{(r['brick_rate'] or 0)*100:5.0f}%  "
              f"{(r['survival'] or 0)*100:5.0f}%  {r['label']}")
    rows = optimizer.report(history, hands)
    distinct_solve = {round(r["solve_rate"], 4) for r in rows}
    distinct_brick = {round(r["brick_rate"] or 0, 4) for r in rows}
    print("\n  A gain inside its confidence interval is noise. With 8-12 hands")
    print("  most of these intervals overlap — treat the table as a shortlist")
    print("  to re-run at higher hand counts, not as a ranking.")
    if len(distinct_solve) == 1 and len(distinct_brick) == 1:
        print(f"\n  ! At {hands} hands every trial scored the SAME solve and brick")
        print("    rate, so the ranking above rests entirely on the handtrap")
        print("    survival term. The swapped card is simply not in the hands")
        print("    that brick. Raise hands before reading anything into this.")
    out = ROOT / "results" / f"optimize_{deck.name}.ydk"
    best.deck.write_ydk(out)
    print(f"\n  best list written to {out}")


def cmd_second(conn, args):
    if len(args) < 2:
        print("usage: second <their_deck> <our_deck> [hands]")
        return
    theirs, ours = _resolve_deck(args[0], conn), _resolve_deck(args[1], conn)
    if not theirs or not ours:
        print("deck not found")
        return
    hands = int(args[2]) if len(args) > 2 else 4
    tgt = json.loads((ROOT / "assets" / "derived_targets.json").read_text())
    tk, ok_ = theirs.stem, ours.stem
    if tk not in tgt or ok_ not in tgt:
        print(f"need derived targets for both decks — run `targets` (have: "
              f"{', '.join(k for k in tgt if not k.startswith('_'))})")
        return
    their_target = tgt[tk]["targets"][0]
    our_target = tgt[ok_]["targets"][0]
    print(f"{ok_} going second into {tk}")
    print(f"  their board target: {their_target}")
    print(f"  our payoff target : {our_target}\n")
    rows = breaking.going_second(theirs, ours, their_target, our_target,
                                 hands=hands)
    built = sum(r["their_board"] for r in rows)
    searched = sum(r["searched"] for r in rows)
    through = sum(r["broke"] for r in rows)
    for r in rows:
        state = ("they bricked" if not r["their_board"]
                 else "NO SEARCH (misconfigured)" if not r["searched"]
                 else "got through" if r["broke"] else "stopped")
        print(f"  hand {r['seed']}: {state}")
    print(f"\n  their board built {built}/{len(rows)}, "
          f"we searched {searched}, got through {through}")
    if searched < built:
        print("  ! some hands never entered a search — see breaking.searched()")


def cmd_floodgates(conn, args):
    if not args and not db.get_meta(conn, "active_deck"):
        print("usage: floodgates <deck.ydk> [hands]")
        return
    path = _resolve_deck(args[0] if args else None, conn)
    if not path:
        print(f"no such deck: {args[0]}")
        return
    hands = int(args[1]) if len(args) > 1 else 2
    tgt = json.loads((ROOT / "assets" / "derived_targets.json").read_text())
    if path.stem not in tgt:
        print(f"no derived target for {path.stem} — run `targets`")
        return
    our_target = tgt[path.stem]["targets"][0]
    print(f"{path.stem} under floodgates, target {our_target}")
    print("locals run these far more than top tables do\n")
    rows = breaking.floodgate_check(path, our_target, hands=hands)
    print(f"  {'functioned':>12}  floodgate")
    for r in rows:
        if not r["tested"]:
            print(f"  {'untested':>12}  {r['name']}  (could not build the board)")
        else:
            print(f"  {r['functioned']}/{r['tested']:<10}  {r['name']}")
    print("\n  'functioned' = still assembled its payoff with that card set")
    print("  across from it. A set Continuous Trap cannot activate the turn")
    print("  it is set, so this models it flipping during your turn.")


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

    db.set_meta(conn, "active_deck", deck.name)
    conn.commit()
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
    "import": cmd_import,
    "mydeck": cmd_mydeck,
    "ydke": cmd_ydke,
    "export": cmd_export,
    "setup": cmd_setup,
    "engine": cmd_engine,
    "solve": cmd_solve,
    "ladder": cmd_ladder,
    "gate": cmd_gate,
    "boards": cmd_boards,
    "threats": cmd_threats,
    "targets": cmd_targets,
    "matrix": cmd_matrix,
    "locals": cmd_locals,
    "optimize": cmd_optimize,
    "second": cmd_second,
    "floodgates": cmd_floodgates,
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
