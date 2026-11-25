"""The AI's effectiveness over time, logged for graphing (user, 2026-11-21: "for
future graphing purposes i want you to start logging the effectiveness of the ai
as weve been developing it, if you can fill in older data too do that").

Two tables in the main database:

  ai_metrics  one measurement per row: when (`at`, the day it describes), the
              series ("Angelechy-Meta | first | won"), value, n, a 95% interval,
              the era of settings (`era`: a game device label / a game.METHOD),
              the machine, where it came from (`source`), a note.
              Sources: games (per day), games-era (an era's whole pool, crossed-
              bootstrap interval), audit (misplays per game, play_audit.py),
              experiment (an A/B or bench result), doc (a number recorded in
              CLAUDE.md / STAGE*.md before its games were relabelled or deleted).
  ai_events   the AI changes, dated, to mark on a graph.

Reading the series: higher "won" / lower "killed_t2" mean OUR side plays better
or the enemy worse -- both sides are the AI, so win rates mix the two; the audit
series (misplays per game, both sides) and the A/B results are the clean reads
of "smarter". Never compare across eras without its label: each era changed what
a game is (budgets, enemy goals, defender), which is why the old games carry them.

    venv/bin/python tools/ai_metrics.py backfill      # everything stored + recorded history
    venv/bin/python tools/ai_metrics.py snapshot      # recompute the current eras (cron, daily)
    venv/bin/python tools/ai_metrics.py event YYYY-MM-DD "title" ["detail"]
    venv/bin/python tools/ai_metrics.py export [path] # CSV of both tables (default results/ai_metrics.csv)
    venv/bin/python tools/ai_metrics.py show [series-substring]
"""

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "experiments"))

from skills import db, engine, stats, trainer  # noqa: E402

DECK = "imported"
CURRENT = ("server", "duelpc")         # game devices still collecting (not relabelled eras)

# What each relabelled era of games was (web/views.OLD_SECTIONS says it on /games)
ERAS = {
    "kill1x": "their kill check at 1x (now 16x), one-target enemy turn 2",
    "endgame-t1": "the enemy's turn 1 aimed at one card (Endgame Problem)",
    "brk1x": "our going-second break searched 1x (now 8x)",
    "gift": "before 2026-11-20: own-turn hand traps spent, enemy gifts allowed",
    "m21b": "2026-11-21b: before patches 0011-0013 and the end-of-turn sets",
    "m21c": "2026-11-21c: the defender picked between two scripts with hindsight",
}

# Numbers recorded in the project notes for games no longer stored as they were.
# (at, series, value, n, lo, hi, era, note)
DOC_HISTORY = [
    ("2026-11-09", "Angelechy-Meta | first | won", 0.19, 2400, None, None, "greedy-defender",
     "4800 games of 2026-11-09, greedy defender (CLAUDE.md, patch 0004 section)"),
    ("2026-11-09", "Angelechy-Meta | first | killed_t2", 0.60, 2400, None, None, "greedy-defender",
     "lost on their turn 2 at the same rate whatever our board held"),
    ("2026-11-09", "Angelechy-Meta | first | board stopped nothing", 1866 / 2370, 2370, None, None,
     "greedy-defender", "games where our board stopped nothing at all"),
    ("2026-11-17", "Angelechy-Meta | first | won", 0.26, 2400, 0.15, 0.39, "kill1x",
     "2400 games, crossed-bootstrap 95% (STAGE11 rules); later deleted (turn-3 steer bug)"),
    ("2026-11-17", "Angelechy-Meta | second | won", 0.45, None, None, None, "endgame-t1",
     "the enemy answering with every hand card it can play (was 63% with hand traps only)"),
    ("2026-11-19", "Angelechy-Meta | first | won", 11 / 159, 159, None, None, "honest-16x",
     "first honest going-first baseline: 16x kill check, trained enemy turn 2 (8 hands)"),
    ("2026-11-19", "Angelechy-Meta | first | killed_t2", 0.84, 159, None, None, "honest-16x",
     "same baseline"),
    ("2026-11-19", "Angelechy-Meta | second | won", 0.66, None, None, None, "honest-8x",
     "8x break, trained enemy turn 1 (was 31% at 1x)"),
    ("2026-11-20", "Angelechy-Meta | first | own-turn waste (Fuwalos, our side)", 94 / 480, 480, None,
     None, "gift", "we fired Mulcharmy Fuwalos on our OWN turn 1 in 94 of 480 games (before patch 0009)"),
    ("2026-11-20", "Angelechy-Meta | first | own-turn waste (enemy Mulcharmies)", 148 / 480, 480, None,
     None, "gift", "Angelechy fired Fuwalos / Purulia on its own turn 1 in 74 + 74 of 480"),
]

# Benches and A/Bs: (at, series, value, n, lo, hi, era, note) -- value is the
# measured rate of the arm named in the series
EXPERIMENTS = [
    ("2026-11-11", "defender bench | kill check lost | greedy", 142 / 237, 237, None, None, "bench-20s",
     "defender_bench.py arm A, 20 s kill check on 237 stored turn 1s"),
    ("2026-11-11", "defender bench | kill check lost | smart+hold", 90 / 237, 237, None, None, "bench-20s",
     "arm D: patch 0004 smart defender holding board pieces; 65 saved, 13 newly lost"),
    ("2026-11-17", "t1 backup goal | lost | full goal", 0.69, 720, None, None, "bench-4x",
     "t1_backup_bench.py: the full goal vs interruptions only (73%), p=0.009"),
    ("2026-11-18", "plateau | kill check lost | 16x", 0.93, 72, None, None, "plateau",
     "budget_plateau: converged at 16x (32x the same); OTK_X = 16"),
    ("2026-11-18", "plateau | our break won | 8x", 0.81, 72, None, None, "plateau",
     "budget_plateau: converged at 8x; BREAK_X = 8"),
    ("2026-11-18", "defender family | kill check lost | best of 8 scripts", 0.90, 72, None, None,
     "plateau", "today's 1+2 = 92%: no scripted defence stops a converged attacker"),
    ("2026-11-18", "searched defender | kill check lost | searched", 0.74, 72, None, None, "bench-4x",
     "STAGE14: the searched defender stops nothing real (scripted 65%)"),
    ("2026-11-21", "t1 enemy timing | our board killed | fire at once", 0.80, 79, None, None, "bench-16x",
     "t1_enemy_script.py FINAL: all1 82%, all2 76%; per-position pick by kill check 90%"),
    ("2026-11-21", "net prior | held-out kills found 30 out | uniform 6x sims", 14 / 40, 40, None, None,
     "mcts-800", "prior_ab.py at about equal wall time (7.0 s)"),
    ("2026-11-21", "net prior | held-out kills found 30 out | BC net", 28 / 40, 40, None, None,
     "mcts-800", "prior_ab.py: the learned prior finds 2x the far kills (5.1 s)"),
    ("2026-11-21", "patch 0011 | our board killed | old defender", 41 / 49, 49, None, None, "bench-16x",
     "negate_live_ab.py: 49 positions where a negate waited"),
    ("2026-11-21", "patch 0011 | our board killed | negate waits for a live target", 38 / 49, 49, None,
     None, "bench-16x", "3 saved, 0 newly lost (sign p 0.25)"),
    ("2026-11-21", "patch 0012 | our board killed | ATK target pick", 52 / 60, 60, None, None, "bench-16x",
     "kill_key_ab.py: 60 positions holding S:P / Dragostapelia / Lars"),
    ("2026-11-21", "patch 0012 | our board killed | kill keys first", 52 / 60, 60, None, None, "bench-16x",
     "0 saved, 0 newly lost: no measurable effect"),
    ("2026-11-21", "Dominus from hand on our turn 1 | our board killed | fires all", 0.95, 40, None, None,
     "bench-16x", "t1_dominus.py: without Impulse 92%, Purge 98%, Spark 92% -- firing them is no misplay"),
]

# The AI changes, dated (from the commits and the notes)
EVENTS = [
    ("2026-11-02", "rooted solver (patch 0002)", "turn 2/3 searchable: the games exist"),
    ("2026-11-06", "patch 0003: stop-on-win + kernel page tracker", "1.88x states in the same time"),
    ("2026-11-11", "patch 0004: smart defender", "ordered answers, board pieces held, by-value targets"),
    ("2026-11-13", "turn 1 in the trainer's duel; every interruption of a deck fires", "ISSUES #72"),
    ("2026-11-17", "the enemy answers with every hand-playable card (Dominus)", "going second 63% -> 45%"),
    ("2026-11-18", "patch 0007 --no-self-target; Trivikarma dead without Visas", ""),
    ("2026-11-19", "converged budgets: kill check 16x, break 8x", "OTK_X / BREAK_X"),
    ("2026-11-19", "the enemy plays its trained goals", "turn 1 going second, turn 2 going first"),
    ("2026-11-20", "patch 0009: no own-turn hand traps; fewest usable gifts", "METHOD 2026-11-20"),
    ("2026-11-20", "duel mode (it knows your list, never your hand)", "patch 0010"),
    ("2026-11-21", "Seneschal gift allowed (usable gifts only); METHOD 2026-11-21b", ""),
    ("2026-11-21", "patches 0011 (negates wait for a live target) + 0012 (kill keys first) built",
     "measured on the PC; not yet in the games"),
]


def ensure(conn):
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_metrics (
            at TEXT NOT NULL, series TEXT NOT NULL, value REAL, n INTEGER, lo REAL, hi REAL,
            era TEXT NOT NULL DEFAULT '', device TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
            note TEXT, recorded TEXT NOT NULL,
            UNIQUE (at, series, era, device, source))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_events (
            at TEXT NOT NULL, title TEXT NOT NULL, detail TEXT, recorded TEXT NOT NULL,
            UNIQUE (at, title))""")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put(conn, at, series, value, n=None, lo=None, hi=None, era="", device="", source="games", note=None):
    with conn:
        conn.execute("INSERT OR REPLACE INTO ai_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (at, series, value, n, lo, hi, era or "", device or "", source, note, _now()))


def _era(device, methods):
    if device in ERAS:
        return device
    return "method " + ",".join(sorted(m for m in methods if m)) if any(methods) else device


def games(conn, only_current=False):
    """Per day and per era: won / killed on turn 2 (going first) / damage of the
    non-wins (going second), for the main deck's games of every enemy."""
    dh = trainer.deck_hash(engine.DECKS / f"{DECK}.ydk")
    secs = conn.execute("SELECT DISTINCT enemy, first, device FROM game_results WHERE deck_hash=?",
                        (dh,)).fetchall()
    done = 0
    for enemy, first, device in secs:
        if only_current and device not in CURRENT:
            continue
        rows = conn.execute("SELECT * FROM game_results WHERE deck_hash=? AND enemy=? AND first=? "
                            "AND device=? AND outcome != 'no_t1'", (dh, enemy, first, device)).fetchall()
        if not rows:
            continue
        era = _era(device, {r["method"] for r in rows})
        side = "first" if first else "second"
        byday = defaultdict(list)
        for r in rows:
            byday[(r["created"] or "")[:10]].append(r)
        for day, rs in sorted(byday.items()):
            n = len(rs)
            w = sum(r["outcome"] == "win" for r in rs)
            put(conn, day, f"{enemy} | {side} | won", w / n, n, *stats.wilson(w, n)[1:], era, device,
                "games", "Wilson on the day's games (pooled: narrow)")
            if first:
                k = sum(r["outcome"] == "lost_t2" for r in rs)
                put(conn, day, f"{enemy} | {side} | killed_t2", k / n, n, *stats.wilson(k, n)[1:], era,
                    device, "games", "Wilson on the day's games (pooled: narrow)")
            else:
                dmg = [r["damage"] or 0 for r in rs if r["outcome"] == "no_win"]
                if dmg:
                    put(conn, day, f"{enemy} | {side} | damage of non-wins", sum(dmg) / len(dmg), len(dmg),
                        era=era, device=device, source="games")
        # the era's whole pool, with the honest crossed interval (hands x their hands)
        last = max(byday)
        for metric, hit in (("won", "win"),) + ((("killed_t2", "lost_t2"),) if first else ()):
            cells = defaultdict(list)
            for r in rows:
                cells[(r["hand_index"], r["enemy_seed"])].append(int(r["outcome"] == hit))
            p, lo, hi = stats.crossed_bootstrap(cells)
            put(conn, last, f"{enemy} | {side} | {metric}", p, len(rows), lo, hi, era, device, "games-era",
                f"{ERAS.get(device, 'current settings')}; crossed bootstrap over hands x enemy hands")
        done += 1
    return done


def audit(conn, only_current=False):
    """Misplays per game, both sides (play_audit.py), per game section."""
    import play_audit
    dh = trainer.deck_hash(engine.DECKS / f"{DECK}.ydk")
    secs = conn.execute("SELECT DISTINCT enemy, first, device, MAX(created), GROUP_CONCAT(DISTINCT method) "
                        "FROM game_results WHERE deck_hash=? GROUP BY enemy, first, device", (dh,)).fetchall()
    for enemy, first, device, last, methods in secs:
        if only_current and device not in CURRENT:
            continue
        side = "first" if first else "second"
        try:
            r = play_audit.main(enemy, side, device, quiet=True)
        except Exception as exc:  # noqa: BLE001 -- one section's failure is noted, not fatal
            print(f"audit {enemy} {side} {device}: {type(exc).__name__}: {exc}")
            continue
        n = r["games"]
        if not n:
            continue
        era = _era(device, set((methods or "").split(",")))
        for k, label in (("own_turn_waste", "own-turn hand traps per game"),
                         ("self_negate", "self-negates per game"),
                         ("free_left", "free traps left in hand per game"),
                         ("hand_discards", "hand-size discards per game"),
                         ("avoidable_gifts", "avoidable usable gifts per game")):
            if r.get(k) is not None:
                put(conn, last[:10], f"{enemy} | {side} | misplay: {label}", r[k] / n, n, era=era,
                    device=device, source="audit", note="play_audit.py, both sides")
        if r["pieces_counted"]:
            put(conn, last[:10], f"{enemy} | {side} | pieces that fired", r["pieces_fired"] / r["pieces_counted"],
                r["pieces_counted"], era=era, device=device, source="audit",
                note="interruption pieces on the board at the start of the opponent's turn that fired there")
        print(f"audit {enemy} {side} {device}: {n} games")


def history(conn):
    for at, series, value, n, lo, hi, era, note in DOC_HISTORY:
        put(conn, at, series, value, n, lo, hi, era, "", "doc", note)
    for at, series, value, n, lo, hi, era, note in EXPERIMENTS:
        put(conn, at, series, value, n, lo, hi, era, "", "experiment", note)
    for at, title, detail in EVENTS:
        event(conn, at, title, detail)


def event(conn, at, title, detail=""):
    with conn:
        conn.execute("INSERT OR REPLACE INTO ai_events VALUES (?,?,?,?)", (at, title, detail, _now()))


def export(conn, path=None):
    path = Path(path or ROOT / "results" / "ai_metrics.csv")
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "at", "series", "value", "n", "lo", "hi", "era", "device", "source", "note"])
        for r in conn.execute("SELECT * FROM ai_metrics ORDER BY series, at"):
            w.writerow(["metric", r["at"], r["series"], r["value"], r["n"], r["lo"], r["hi"], r["era"],
                        r["device"], r["source"], r["note"]])
        for r in conn.execute("SELECT * FROM ai_events ORDER BY at"):
            w.writerow(["event", r["at"], r["title"], "", "", "", "", "", "", "", r["detail"]])
    print(f"wrote {path}")


def show(conn, sub=""):
    for r in conn.execute("SELECT * FROM ai_metrics WHERE series LIKE ? ORDER BY series, at, source",
                          (f"%{sub}%",)):
        ci = f" ({r['lo']:.0%}-{r['hi']:.0%})" if r["lo"] is not None and r["value"] is not None and r["value"] <= 1 else ""
        v = f"{r['value']:.1%}" if r["value"] is not None and r["value"] <= 1 else f"{r['value']:.0f}"
        print(f"{r['at']}  {r['series'][:58]:58} {v:>7}{ci:14} n={r['n'] or '':<5} {r['era']:22} "
              f"{r['device']:8} {r['source']}")


def main(cmd="show", *args):
    conn = db.connect()
    ensure(conn)
    if cmd == "backfill":
        history(conn)
        print(f"games: {games(conn)} sections")
        audit(conn)
        export(conn)
    elif cmd == "snapshot":
        print(f"games: {games(conn, only_current=True)} current sections")
        audit(conn, only_current=True)
        export(conn)
    elif cmd == "event":
        event(conn, args[0], args[1], args[2] if len(args) > 2 else "")
    elif cmd == "export":
        export(conn, *args)
    elif cmd == "show":
        show(conn, *args)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(*sys.argv[1:])
