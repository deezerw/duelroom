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
  threats [library]         interruption counts + common pieces per archetype
  snapshot [label]          freeze today's inclusion rates for later comparison
  drift <archetype> [a] [b] what's rising or falling between two snapshots
  targets [library]         derive solver targets from the board library
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

**Bot-vs-bot is deferred, not rejected.** It comes after Stages 6-7 (the playmat viewer and the deck
trainer) once the solver is trusted, and the design deliberately leaves room for it: `engine.py` already drives both
seats, and a solver good enough to pilot your deck is most of a solver good enough to
pilot the opponent's. What it would add is interaction the solitaire model cannot see —
chain fights, grind games, decisions made with incomplete information. What it costs is
thousands of games per comparison for a couple of points of win rate, on 8 cores. Build
it when the cheap questions are answered, not before.

## Architecture & Data Flow

```
daemon.py (CLI loop + report formatting)      web/ (Flask, same skills + same SQLite)
  │
  ├─ data in ───────────────────────────────────────────────────────────────
  │  skills/card_db.py      YGOPRODeck card mirror + banlist (`cards`)
  │  skills/meta_scrape.py  Decklist scraping, inclusion rates, placements
  │  skills/importer.py     Bring a deck in: .ydk, ydke://, ygoprodeck URL
  │  skills/images.py       Local card-art cache (data/cardimg)
  │  skills/db.py           SQLite schema + connections
  │  skills/config.py       locals.toml -- the weights every ranking inherits
  │
  ├─ the deck ──────────────────────────────────────────────────────────────
  │  skills/deck.py         .ydk parse/write, deck model, `Deck.validate`
  │  skills/builder.py      Deckbuilder card search and list editing
  │  skills/carddb.py       Merged engine card DB  (NOT card_db.py -- see below)
  │
  ├─ running the engine ────────────────────────────────────────────────────
  │  skills/engine.py       ygorl + the C++ combo solver; workers, budgets
  │  skills/gate.py         Validation gate, budget ladder, result loading
  │  skills/handlab.py      Solve ONE chosen hand, in full detail
  │
  ├─ asking questions ──────────────────────────────────────────────────────
  │  skills/sweep.py        The board library: multi-draw ladder, triaged budget
  │  skills/scenarios.py    Handtrap injection, targets, floodgates, the matrix
  │  skills/breaking.py     Going-second board breaking
  │  skills/gauntlet.py     Candidate end boards ranked by what they STOP
  │  skills/optimizer.py    Flex-slot hill climb with paired seeds
  │  skills/stats.py        Hypergeometric, Wilson CIs, seed pairing
  │
  └─ output ────────────────────────────────────────────────────────────────
     skills/report.py       One view of everything known about a deck
     skills/drill.py        Best lines out as EDOPro .yrpX replays
```

**There is no `skills/solver.py` and no `skills/scoring.py`.** Both were in this
diagram until 2026-10-25 and neither has ever existed. The search is the **C++
`combosolver`** (`engine.SOLVER_BIN`), driven by wall-clock budget, so there is no
Python `MAX_DEPTH`/`MAX_NODES`/`BEAM_WIDTH` to tune — the lever is `engine.SOLVE_MS`,
calibrated per deck with `ladder`. Board quality likewise is **not** a weighted score:
it is (a) whether the line satisfied the `--target` conjunction the sweep planned and
(b) `solver.blocking.interruptions` counted by ygorl. Nothing multiplies negates by a
weight. Anyone sent looking for board weights by the old table found nothing, which is
the honest outcome; the misleading part was being sent.

**`card_db.py` and `carddb.py` are different modules one underscore apart.**
`card_db` is the YGOPRODeck mirror (names, banlist, art metadata — what the reports
read); `carddb` is the merged BabelCDB the rules engine loads. Importing the wrong one
is silent, because both answer about cards.

**Key flow:** scrape meta → inclusion rates identify flex slots → solver goldfishes each
meta deck to build an end-board library → scenarios test your deck against that library
→ optimizer searches flex slots → report.

## Key Configuration (hardcoded at module level)

Checked against the source by `tools/check_docs.py`; every name below exists.

| File | Constants |
|------|-----------|
| `card_db.py` | `API_BASE`, `CARDINFO`, `CHECK_VER`, `TIMEOUT`, `USER_AGENT` |
| `meta_scrape.py` | `LISTS_PER_ARCHETYPE` (50), `PAGE_SIZE`, `MIN_INTERVAL` (0.4 s), `DEFAULT_FORMAT`, `CORE_THRESHOLD`, `STAPLE_LOW`, `MIN_LISTS_FOR_DRIFT`, `PLACEMENT_RANK` |
| `engine.py` | `SOLVER_BIN`, `VENDOR`, `THIRD_PARTY`, `SOLVE_MS` (60 s), `DEFAULT_WORKERS` |
| `scenarios.py` | `HANDTRAPS`, `FLOODGATES`, `BLIND_SPOTS`, `MAXX_C`, `ZONE_RANK`, `TRIVIAL_ACTIONS` |
| `sweep.py` | `TRIAGE_MS` (15 s), `DRAWS` |
| `gauntlet.py` | `DRAWS` (4), `DEFENCE_DRAWS` (3), `MEASURABLE` |
| `config.py` | `LOCALS` — the path to `locals.toml` |

Three of the old table's rows named constants that do not exist, and two named
**files** that do not exist (see the architecture note above). In particular the card
mirror has **no `CACHE_DAYS`**: `card_db.needs_refresh` compares the stored
`checkDBVer` against the remote one, so the cache turns over when YGOPRODeck's
database actually changes rather than on a timer. The rate limit is
`meta_scrape.MIN_INTERVAL`, not a `card_db.RATE_LIMIT_RPS`, and the locals weights
live in `locals.toml` read by `config.py`, not in a `meta_scrape.LOCALS_WEIGHTS`.

## SQLite Schema (`data/duelroom.db`)

**`cards`** — `id`, `name`, `type`, **`frame`**, `race`, `attribute`, `level`, `atk`,
`def`, `linkval`, `scale`, `archetype`, `text`, `banlist_tcg`, `banlist_ocg`,
`last_fetched`

`frame` drives every main-vs-extra routing decision (`deck.EXTRA_FRAMES`) and was
missing from this list. `banlist_tcg`/`banlist_ocg` hold YGOPRODeck's wording —
`Forbidden`, `Limited`, `Semi-Limited`, or NULL — read only through
`card_db.banlist_limit`, which raises on a status it does not recognise rather than
assuming three copies are fine. There is no row for an unlimited card; NULL means
unlimited.

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
- **Extra draws are worth measuring per deck, not assuming.** Four arms on the same
  ten despia hands (identical hand seeds): a single draw at 60 s gives mean 2.50
  interruptions for 14.6 solver-minutes; four draws give 2.60 for 38.4. One hand of
  ten improved. The same machinery on one elfnote hand went from the library's 4 to
  5 int / 4 neg, so the value of draws is **deck-dependent** — ladder it like the
  budget, do not assume `DRAWS`. The free part is the wave restructure: 61.9 → 38.4
  solver-minutes at identical 2.60. Early stopping (`patience`) takes it to 25.3 and
  gives the gain back, so it is opt-in.
- **The multi-draw library, measured per deck against the single-draw one**
  (2026-10-25, 40 identical hand seeds per deck, paired, sign test). Five of the
  eight decks were complete at the time of writing:

  | deck | better | worse | sign p | verdict |
  |---|---|---|---|---|
  | elfnote | 33 | 1 | <0.0001 | **better** |
  | kewl-tune | 10 | 2 | 0.039 | **better** |
  | despia | 7 | 0 | 0.016 | **better** |
  | branded | 7 | 2 | 0.18 | no difference |
  | fiendsmith | 8 | 12 | 0.50 | no difference |

  So extra draws pay for three of five decks and buy nothing measurable for two,
  which is the per-deck `draws` ladder this file already prescribes, now with
  numbers. Mean interruptions per hand: elfnote 3.73 → 4.80, kewl-tune 3.88 →
  4.08, despia 2.38 → 2.55, branded 1.23 → 1.38, fiendsmith 3.17 → 3.05.

  **Extra draws did NOT change the solve rate for any deck** — 31/31 for branded
  and 40/40 for the other four, identical in both arms. Multi-draw improves the
  BOARD on hands that already solved; it does not rescue hands that do not. Any
  claim that it lowers the brick rate needs its own evidence.

  **Retracted:** I previously called fiendsmith's single-draw comparison "9
  regressions ... wants re-running to confirm they are gone". They are not gone
  (now 12 worse, 8 better) and they were never an effect: p=0.50 then and now.
  The honest statement is that fiendsmith shows no measurable difference between
  arms while costing four times the solver time, not that it regressed.

- **Where solver time goes** (all 952 attempts of the stored sweep, 2026-10-22):
  attempts that SOLVED (632) take a median 12.5 s and a mean 16.8 s, and 64% of them
  finish within 15 s; attempts that FAIL (320) burn the WHOLE budget by construction
  (median 60.2 s). 64% of the sweep's 8.2 solver-hours went to failures. Two things
  follow. The breadth pass does not need the full budget — `sweep.TRIAGE_MS` is 15 s,
  and goals that solve there are kept outright. And the full budget still belongs on
  the depth pass, because the borderline goals are exactly the slow ones: one despia
  pair hit at 17.7 s, 32.8 s and 45.4 s across seeds, each of which 15 s calls a brick.
- **Re-drawing a brick does NOT rescue it — measured, so the optimiser keeps its
  cheap single-draw brick rate.** Two independent experiments. (1) The
  single-draw and four-draw library arms, 40 identical hand seeds on each of five
  decks: the set of unsolved hands is **identical in both arms for every deck**,
  0 rescued and 0 lost across all 200 paired hands; branded's same 9 hands failed
  at one draw and at four. (2) `optimizer.paired_check` re-solved the active
  deck's 5 funnel bricks three ways each and found 0 false bricks. Combined:
  **0 of 14 brick-hands rescued, 95% CI 0-22%** — a large effect is ruled out, a
  small one is not. Scope: re-drawing the SAME goal does not rescue it; those
  hands might still solve toward an easier target. So routing `evaluate` through
  the sweep would cost 4x the solver time for an effect measured at zero.
  `confirm_bricks` stays as the free guard.
- **Measured and NOT worth doing:** keeping 4 verified lines per solve instead of 1
  costs 0.1 s (4.1 s vs 4.2 s, same seed, same search), and re-ranking over 32 written
  lines instead of 4 did not improve the board in 4 of 4 cases. The line ceiling is not
  a speed lever and not a quality lever. `default_cards()` is cached per worker (0.16 s
  once) and `Workdir.create` is 2 ms, so per-solve setup is not a lever either.
- **A target set is a CONJUNCTION, so it is monotone.** If the search cannot place one
  card of {base, A} it cannot place {base, A, B}. The sweep therefore asks about pieces
  first and builds stacks only from the pieces that solved — which is also the only way
  the non-prefix stacks (A+C, B+D) ever get requested at all.
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
6. `STAGE6.md` — Watch a combo line on a real playmat (`playback.py`, `web/`)
7. `STAGE7.md` — The deck trainer: play over and over, learn from its own lines
   (`trainer.py`, the solver's `--adapt` corpus)

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
- **Handtrap pairs are not supported, and this was checked rather than assumed
  (2026-10-24).** `--fire` is singular — the solver's help does not mark it
  "Repeatable" as it does `--target` and `--summon` — so a both-fired verdict cannot
  be requested. `--opp-hand` *is* repeatable and reaches the solver through
  `SolveRequest.extra_args` (verified: the flag appears on the command line), so the
  opponent can HOLD a second handtrap while the first is fired. Measured on one
  solved line of the active deck, Ash fired with and without Imperm held gave
  identical verdicts — 2 windows, 1 converted, both ways — because the `--fire`
  verdict counts windows of the fired card only. One hand and 2 windows rules out a
  large effect, not a small one. The matrix still understates how stoppable a deck is,
  and fixing it needs a repeatable `--fire`, not a flag combination.
- **Maxx "C" is a blind spot of the SURVIVAL metric only — the cost is measured.**
  Corrected 2026-10-25; this used to say the cost "is invisible here", which invited
  the opposite error of ignoring a table that exists. Survival asks only whether a
  line reached its target, and 94% of Maxx "C" windows convert, so it reads "fine".
  But Maxx "C" draws on every special summon, so the REPLAYED summon count is the
  cost, and `threats` prints it as "cards surrendered to Maxx 'C' if they combo
  through": fiendsmith **8.4** cards (worst 15), elfnote **6.1** (13),
  light-and-darkness-ritual 4.2 (14), despia 3.0 (7), kewl-tune 2.5 (6),
  sky-striker 2.0 (5), branded 0.6 (6). Read the two together: a deck that combos
  through while handing over eight cards has not beaten it. Droll was listed
  alongside it until the `survives` fix; it is not a blind spot (45/85 windows
  convert, 19 hands died to it) and the earlier 0% was a reading bug.
- **`locals.toml` holds placeholder weights** covering **58.2%** of a stated meta
  that is not yours (measured 2026-10-25 via `config.coverage`; this file said 55%).
  The uncovered 41.8% is rogue 25.0%, mitsurugi 5.8%, azamina 4.4%, invoked 3.6% and
  doomz 3.1% — archetypes with no measured deck in `assets/decks/`. Every weighted
  ranking inherits whatever is in that file, and `matrix` now prints the covered
  share beside its ranking rather than leaving it implicit.
- **The side plan has NO WAY IN, and its metric is the broken one.** Corrected
  2026-10-25; this entry used to read "implemented but not run at scale", which
  invited exactly the reliance it should have warned against. Two things are true:
  `optimizer.side_plan` and its companion `optimizer.propose_side` have **no CLI
  command and no web route** — nothing in `daemon.py`, `web/` or `tools/` calls
  either, so the feature cannot be run at all, at any scale. And it rates every
  candidate through `breaking.going_second`, the *same* function behind the `second`
  command, which the next entry marks UNSUPPORTED on a decisive measurement. So
  wiring up a command would hand over a side-deck planner whose objective returns
  0 of 4 against one vanilla Alexandrite Dragon. **It is deliberately left
  unreachable until that is fixed** — a side deck is 15 cards and games two and
  three, and a confident wrong answer there is worse than no answer. The cost note
  stands for whenever it becomes trustworthy: each candidate is `hands` full
  going-second runs, roughly ten minutes apiece.
- **The floodgate check is implemented but not run at scale**, and two of its five
  floodgates were the wrong card until 2026-10-25 (see ISSUES.md #43).
- **`second` and `gauntlet.rank` are UNSUPPORTED, and the reason changed on
  2026-10-26.** The harness was never putting a board on the field: the start replay
  went in as the solver's POSITIONAL argument, which is a template, and with
  `--deck`/`--hand` beside it the solver builds a FRESH duel (`=== synthetic start
  ===`). Queried at the root, both players' zones were empty and both held five cards
  — so one vanilla Alexandrite Dragon "stopped" 18/18 hands because nothing was in
  front of them. `--deck`/`--hand` also reach side a only, so `--player 1` searched a
  seat nobody controlled. That is why four fixes each changed the hand and none
  changed the result. Full account in ISSUES.md #4.
  The harness is now correct: `breaking.second_position` builds the position with
  `--start` plus `--approach`, and `breaking.check_root` replays the prefix and asks
  the core — verified, the builder has a board and the attacker holds six cards.
  What is still broken is the SEARCH from it, and that is a two-line repro: same deck,
  target, budget and binary, empty field throughout, **turn 1 as player 0 SOLVES while
  turn 2 as player 1 and turn 3 as player 0 reach nothing**. Not the seat and not the
  turn — a resumed root gets only the finisher's unguided walk, with no plan, probe or
  repertoire. Both attacker hands solve in 30 s going first.
  Also measured: the hardcoded `turns >= 2` cuts are real (the approach root was
  EXHAUSTED after 0 expansions; `patches/0001-going-second-turn-cuts.patch` takes the
  cuts from 100% of rollouts to 4% and the root to 591 expansions) and the patch is
  still NOT applied, because it does not make the feature work.
  Both commands print the refusal on every call and the record carries it.
  **The replacement is `gauntlet.rank_boards`** — the same question asked of `--fire`,
  which is already measured over 40 hands per cell. See ISSUES.md #69.
- **`gauntlet` is only as strong as the decks it points at you.** "Stopped" means the
  opposing deck did not assemble ITS ONE PAYOFF TARGET through your board -- the same
  limitation the handtrap matrix has, generalised from a card to a board. It is not a
  life-point check and there is no OTK test; breaking a board is not expressible as a
  solver goal (`--board-remove` relaxes the goal rather than demanding destruction).
  The opponents come from `assets/decks/`, and `export` writes whichever scraped list
  happened to be stored first (`ORDER BY d.id LIMIT 1`), not a representative one:
  `branded.ydk` is a Branded/Blitzclique hybrid with one Fallen of Albaz and no
  Branded Fusion, and it averages 1.6 interruptions over its 31 solved hands of 40
  (1.2 if the 9 with no line count as zero). A gauntlet of weak
  lists flatters every board.

**Deferred:** a bot-vs-bot arena (now after Stage 7), blocked on the turn-2 resume
problem above. See "Why No Duel AI (Yet)" for the trade.
