# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**duelroom** is a Yu-Gi-Oh! deck laboratory. It scrapes the current meta from YGOPRODeck,
runs a solitaire search over the real rules engine to find the best line from any given
hand, and uses that to answer the questions that actually win locals: is my deck
consistent, can it play through handtraps, can it break the boards it will face, and
which flex slots and side deck cards should change.

**It is not a bot-vs-bot arena.** See "Why No Duel AI" below — that decision shapes
everything.

## Running It

```bash
source /home/deezer/duelroom/venv/bin/activate
python daemon.py
```

Commands (checked against the CLI by `tools/check_docs.py`):

```
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
  report [deck] [archetype] everything known about a deck, in one view
  drill [deck] [n]          export best lines as EDOPro replays to practise
  export <archetype> [name] write a scraped list out as a .ydk
  setup                     build the merged engine card database
  engine                    engine setup status and pinned versions
  solve <deck> [hands]      turn-1 line search: solve rate + board quality
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
  gauntlet <deck> <hand>    rank this hand's end boards by what they STOP
  floodgates <deck>         does the deck function under common floodgates
  help / quit
```

## The Web UI

`web/` is a Flask app over the same `skills.*` and the same SQLite — not a second
copy of either, because two implementations of a number is how this project's
worst bugs got in. It is being built to replace the CLI for everyday use; the CLI
stays as the debugging tool. Stages, settled decisions and the security history
that shaped the login are in **`web/PLAN.md`**.

```bash
# development
export DUELROOM_PASSWORD_HASH='pbkdf2:sha256:...'
venv/bin/python web/run.py        # 127.0.0.1:8446
venv/bin/python web/worker.py     # exactly one, runs the queued solver jobs

# deployed: gunicorn TLS :8446 + worker in tmux session 'duelroom'
venv/bin/python web/manage.py set-password
sudo systemctl enable --now duelroom.service duelroom-watchdog.timer
```

**Deploy web changes with SIGHUP, not a restart.** `systemctl restart` drops
the tmux session and kills whatever solver job the worker is holding — two
pilot runs were lost that way. The web code is served by gunicorn, and its
master reloads workers gracefully on SIGHUP without touching the job worker:

```bash
venv/bin/python -c "from skills import db; from web import jobs; \
    print(jobs.active(db.connect()))"      # check first
kill -HUP $(ps -u deezer -o pid,ppid,args --no-headers | grep 'web\.wsgi:app' \
    | grep -v grep | awk '$2 != 1 {print $1}' | head -1)
```

A full restart is still needed for changes to `web/worker.py` or `skills/`
used by the worker. `tmux-stop.sh` now SIGTERMs the worker first and waits
`DUELROOM_STOP_GRACE` (180s) for it to finish its job, but an `optimize` run
outlasts that.

**Guard `__main__` in any script that calls `handlab.analyse`.** It fires the
hand traps through a `spawn` pool, and a spawned child re-imports `__main__`,
so a script with top-level code re-runs itself in every worker. The first run
of that code solved the hand seven times over; the waste looks exactly like
the work.

**Never `pkill -f` the gunicorn pattern.** `pgrep -f` matches whole command
lines, so it also matches the shell that just typed it — that is the exit-144
footgun, and it fired again while deploying this. `tmux-stop.sh` excludes the
caller, its parent and its process group; use it.

Card art lives in `data/cardimg` (gitignored, ~450 MB) and is served only from
there — `daemon.py images small-all` fills it, resumable.

The deckbuilder's two load-bearing rules: card search ranks on the **Wilson lower
bound** of the inclusion rate, never the raw rate, and **legality is only ever
`Deck.validate`**, asked over HTTP on every edit — never reimplemented in the
browser.

## Why No Duel AI (Yet)

Yu-Gi-Oh's open AI (WindBot) is rule-based with a hand-written executor per archetype.
It cannot pilot a deck nobody wrote an executor for, and it plays combo decks badly even
when it can. A naive bot-vs-bot arena therefore ranks decks by "how easy is this for a
dumb bot to pilot," not by how good they are. Training an RL agent is also off the table:
the published agents took 32 GPUs for days, and this box is CPU-only.

Two observations remove the need for an opponent AI entirely:

1. **Searching your own turn to its best board is optimal piloting.** A solver on your
   side of the field beats any learned policy at the solitaire problem, and it is the
   same machinery that discovers combo lines.
2. **Side decking is a static board-breaking benchmark, not a duel simulation.** Point
   the same solver at a meta deck to generate its realistic turn-1 end boards, then deal
   yourself a going-second hand and search for a break. The decks "duel" in the only
   sense that matters, with no opponent AI anywhere.

**Bot-vs-bot is deferred, not rejected.** It is the natural Stage 6 once the solver is
trusted, and the design deliberately leaves room for it: `engine.py` already drives both
seats, and a solver good enough to pilot your deck is most of a solver good enough to
pilot the opponent's. What it would add is interaction the solitaire model cannot see —
chain fights, grind games, decisions made with incomplete information. What it costs is
thousands of games per comparison for a couple of points of win rate, on 8 cores. Build
it when the cheap questions are answered, not before.

## Architecture & Data Flow

```
daemon.py (CLI loop + report formatting)
  │
  ├─→ skills/card_db.py      YGOPRODeck cardinfo.php cache + banlist
  ├─→ skills/meta_scrape.py  Decklist scraping, inclusion-rate analysis
  ├─→ skills/deck.py         .ydk parse/write, deck model, legality
  ├─→ skills/engine.py       ygopro-core binding (duel create/step/query)
  ├─→ skills/solver.py       Action-tree search over own legal moves
  ├─→ skills/scoring.py      End-board evaluation
  ├─→ skills/scenarios.py    Handtrap injection, board breaking, floodgates
  ├─→ skills/gauntlet.py     Candidate end boards, ranked by what they stop
  ├─→ skills/optimizer.py    Flex-slot hill climb with paired seeds
  └─→ skills/stats.py        Hypergeometric, Wilson CIs, seed pairing
```

**Key flow:** scrape meta → inclusion rates identify flex slots → solver goldfishes each
meta deck to build an end-board library → scenarios test your deck against that library
→ optimizer searches flex slots → report.

## Key Configuration (hardcoded at module level)

| File | Constants |
|------|-----------|
| `card_db.py` | `YGOPRODECK_API`, `RATE_LIMIT_RPS` (20), `CACHE_DAYS` |
| `meta_scrape.py` | `LISTS_PER_ARCHETYPE` (50), `FORMAT`, `LOCALS_WEIGHTS` |
| `engine.py` | `CORE_PATH`, `SCRIPTS_PATH`, `CDB_PATH`, pinned versions |
| `solver.py` | `MAX_DEPTH`, `MAX_NODES`, `BEAM_WIDTH` |
| `scoring.py` | Board weights: negates, card advantage, ATK, GY resources |
| `scenarios.py` | `HANDTRAPS`, `FLOODGATES`, `TRIALS_PER_SCENARIO` |

## SQLite Schema (`data/duelroom.db`)

**`cards`** — `id`, `name`, `type`, `attribute`, `race`, `level`, `atk`, `def`,
`archetype`, `text`, `banlist_tcg`, `banlist_ocg`, `last_fetched`

**`decks`** — scraped lists: `id`, `archetype`, `source_url`, `format`, `placement`,
`list_hash`, `scraped_at`

**`deck_cards`** — `deck_id`, `card_id`, `section` (main/extra/side), `copies`

**`inclusion`** — `archetype`, `format`, `card_id`, `section`, `pct_lists`,
`avg_copies`, `n_lists`, `computed_at` — the flex-slot table

**`runs`** — `id`, `deck_hash`, `scenario`, `seed_base`, `n_trials`, `engine_version`,
`scripts_version`, `started`, `finished`

**`trials`** — `run_id`, `seed`, `opening_hand`, `completed`, `board_score`,
`negates`, `special_summons`, `line` (JSON action sequence). **`special_summons` is
never populated** -- the solver prints summon counts to stdout but none reach the
JSONL, and the Maxx "C"-window shortcut for deriving them was wrong (those are
response windows). Real counts need each line replayed through ygorl.

**`boards`** — end-board library: `archetype`, `board_state` (JSON), `frequency`,
`source_run`

## Business Logic Notes

- **Flex slots:** cards in 20–60% of top lists. The optimizer searches only these, which
  cuts the space from 13,000 cards to roughly 30 realistic candidates.
- **Paired seeds:** deck variants are always compared on identical shuffle seeds (common
  random numbers). This is the single biggest variance reduction available and it is
  mandatory, not optional.
- **Hand-level memoization: measured, and NOT worth building.** The original plan
  called this non-negotiable on the assumption that opening hands repeat across
  trials. They do not. A correct cache key must include the solver budget and the
  target (10 s reports a brick where 60 s solves the same hand) and, for correctness,
  the ygorl/CardScripts/BabelCDB revisions too. Measured over every run in `results/`:
  18.8% apparent hits with budget and target keyed, and all of them were re-running
  the same gate at identical settings -- which is re-validation after a component
  bump, precisely the case where a cached answer would hide the change being checked
  for. With revisions in the key the rate is about zero.

  The optimizer gets no benefit either, but NOT for the reason first written here.
  Measured over 80 (candidate, seed) pairs, a one-card swap leaves the dealt hand
  **unchanged 55% of the time** -- the claim that every hand differs was wrong. The
  hands coincide; the solves still do not, because the two decks differ by a card and
  the solver searches the deck, so a line available to one may not be available to
  the other. A cache keyed on the hand alone would return answers from a deck that is
  not the one being evaluated.

  The same measurement is good news elsewhere: because most trial hands are shared
  with the base deck, the optimizer's paired comparison is tighter than a shuffle-luck
  argument would suggest.
- **Play/draw split:** every metric is reported split by going first vs going second.
  The die roll dominates modern Yu-Gi-Oh and an averaged number hides the answer.
- **Locals weighting:** the gauntlet is weighted by a hand-maintained config, not by
  global meta share. Top-5 YGOPRODeck lists are the optimized, funded field; locals run
  budget builds, last format's deck, and rogue piles.
- **Don't simulate what math solves:** pure "do I open card X" questions are
  hypergeometric — exact and instant. Reserve the engine for questions about lines.
- **The solver is a RANDOMIZED search, and one attempt is one draw.** Measured
  2026-10-20, same hand, same target set, same 60 s budget, only the solver seed
  differing: an elfnote hand returned end boards of 3, 3, 3, 3, 5 and 6 interruptions
  over six draws, and a despia pair solved on 3 of 6 draws while reporting "no line"
  on the other 3. No `--seed` is passed anywhere, so all 280 records in
  `results/boards.jsonl` carry a different one. Consequences, in order of how much
  they cost: a single-draw "this deck cannot make that board" is a coin flip, not a
  finding; the best board a hand can reach is usually NOT the one a single draw
  returns, because the solver stops at the first line that satisfies the targets and
  anything else on the board is incidental; and no run is reproducible. `gauntlet`
  draws each goal `DRAWS` times for this reason, and the opponent gets
  `DEFENCE_DRAWS`. Anything that takes one draw and believes it is understating
  the deck.
- **Version pinning:** `ygopro-core`, `CardScripts`, and `cards.cdb` must match and are
  pinned. Treat an update as a migration; otherwise results silently change meaning.

## Tech Stack

- **Python 3.12**, venv at `venv/`
- **ygopro-core** (C++ rules engine) via pybind11/ctypes, with Lua `CardScripts`
- **httpx** for the YGOPRODeck API; plain GET + `__NEXT_DATA__` parsing for decklists
- **SQLite** (stdlib `sqlite3`) for all caching and results
- **numpy / scipy** for statistics
- No GPU. CPU-only, 8 cores.

---

## Implementation Stages

Work through these in order. Each stage is in its own file — delete the file once that
stage is complete and verified.

1. ~~Meta ingestion & card database~~ — **done** (card_db, meta_scrape, deck, stats, daemon)
2. ~~Engine harness~~ — **done** (`engine.py` drives ygorl + ygo-combo-solver)
3. ~~Validation gate, scale, storage~~ — **done** (`gate.py`; gate passed, board library built)
4. ~~Scenario suite~~ — **done** (`scenarios.py`, `breaking.py`)
5. ~~Optimizer~~ — **done** (`optimizer.py`; flex-slot hill climb + side plan)

Stage 2 was expected to be the whole risk and instead resolved in an afternoon: ygorl
builds clean on Python 3.12 and already provides the engine, determinism patches,
snapshot/branch, paired seeds, replay export and a wrapped combo solver. Stages 3-5 were
rewritten against what actually exists rather than what was planned.

All five stages are implemented. Standing cautions, in the order they will bite:

- **Run `daemon.py setup` after any submodule bump.** BabelCDB ships recent sets as
  separate `release-*.cdb` that ygorl does not load, and a deck holding one of those
  cards segfaults the solver after five misleading diagnostics.
- **Bump BabelCDB and CardScripts together.** Cards without their Lua load fine and
  then do nothing, which looks exactly like a deck that cannot combo.
- **Re-run `daemon.py gate snake_eye` after any component bump.** CardScripts changes
  rules behaviour for every deck.
- **Measure the solver budget per deck with `daemon.py ladder`.** Never assume one; a
  cheap run is a wrong run, not a fast one.

## Known limitations

Honest gaps, so nobody mistakes a limit for a result:

- **Sample sizes are small.** Everything measured so far is 4-12 hands per deck. At six
  hands an optimizer run scored an identical brick and solve rate for every trial, so
  the whole ranking rested on the handtrap-survival term. The code says so when it
  happens; raise `hands` before believing an ordering.
- **Handtrap pairs are not supported.** `--fire` takes one card per invocation and
  funnel_eval loops them separately, so Ash + Imperm together cannot be tested. Locals
  players hold more than one, so the matrix understates how stoppable a deck is.
- **Maxx "C" is a measurement blind spot.** The metric asks only whether a line
  reached its target, and 94% of Maxx "C" windows convert, so lines do continue through
  it -- what it actually costs you, the cards it hands over, is invisible here. Droll
  was listed alongside it until the `survives` fix; it is not a blind spot (45/85
  windows convert, 19 hands died to it) and the earlier 0% was a reading bug.
- **`locals.toml` holds placeholder weights** covering 55% of a stated meta that is not
  yours. Every weighted ranking inherits whatever is in that file.
- **The side plan and floodgate check are implemented but not run at scale.** Each side
  candidate is `hands` full going-second runs, roughly ten minutes apiece.
- **`gauntlet` is only as strong as the decks it points at you.** "Stopped" means the
  opposing deck did not assemble ITS ONE PAYOFF TARGET through your board -- the same
  limitation the handtrap matrix has, generalised from a card to a board. It is not a
  life-point check and there is no OTK test; breaking a board is not expressible as a
  solver goal (`--board-remove` relaxes the goal, see `breaking.going_second_args`).
  The opponents come from `assets/decks/`, and `export` writes whichever scraped list
  happened to be stored first (`ORDER BY d.id LIMIT 1`), not a representative one:
  `branded.ydk` is a Branded/Blitzclique hybrid with one Fallen of Albaz and no
  Branded Fusion, and it averages 1.0 interruptions over 40 hands. A gauntlet of weak
  lists flatters every board.

**Deferred:** a Stage 6 bot-vs-bot arena, once Stages 1–5 are trusted. Not planned in
detail yet — see "Why No Duel AI (Yet)" above for the trade.
