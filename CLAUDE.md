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
  train <deck> [hours] [n]  play n hands over and over, keep each best line
  games <deck> <enemy> [runs] [enemy hands] [--second]  finish the game: win rate by turn 3 (going 2nd: turn 2)
  impact <deck> [enemy] [--first|--second]  card impact scores from the games played
  stops <deck> [enemy] [--first|--second]  which of their cards you stopped, with what, and what it won
  starters <deck> <1-3> [hours]  what every 1/2/3-card hand makes on its own
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
  │  skills/playback.py     A solved line as playmat frames (core snapshots)
  │
  ├─ asking questions ──────────────────────────────────────────────────────
  │  skills/sweep.py        The board library: multi-draw ladder, triaged budget
  │  skills/scenarios.py    Handtrap injection, targets, floodgates, the matrix
  │  skills/breaking.py     Going-second board breaking
  │  skills/gauntlet.py     Candidate end boards ranked by what they STOP
  │  skills/optimizer.py    Flex-slot hill climb with paired seeds
  │  skills/trainer.py      The deck trainer: replay a hand pool, keep best lines
  │  skills/game.py         Finish the game: turns 1-3 vs a meta deck, win rate
  │  skills/impact.py       Card impact: win rate when a card was used vs not
  │  skills/stops.py        What you stopped of theirs, with what, and its worth
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
| `trainer.py` | `HANDS` (24, the pool), `PER_HAND` (3 solves per hand per generation), `BIG_STACK` (3), `BIG_STACK_X` (2), `TRAIN_MS` (180 s per attempt; the page's "seconds per try"), `PLATEAU` (6) |
| `game.py` | `ROOTED_BIN`, `RUNS` (10), `ENEMY_HANDS` (3), `OTK_MS` (20 s), `NEAR_DUD_RATE` (0.10), `GAMES`, `STOP_ON_WIN` (4), `ARENA_ENV`, `DEFENDER` ("smart"), `DEFENCES` ((1, 2)) |

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

**`game_turn1`** / **`game_results`** — Stage 9 games (`skills/game.py`): turn 1 once
per (deck, enemy, hand, board, enemy seed) with the enemy's opening hand for the
fairness check; one row per game with `outcome` (`win` / `no_win` / `lost_t2` /
`no_t1`), turn-3 `damage`, and the deciding line's replay path. Game files live in
`results/games/<deck>/<enemy>/`.

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
6. ~~Playmat line viewer~~ — **done** (`playback.py`, `/watch/...`; Watch buttons on pilot + drill)
7. ~~Deck trainer~~ — **done** (`trainer.py`, `/train`; best-line trainer, see below)
8. `STAGE8.md` — Matchup testing: top-3 boards per hand, impact score per card, shared enemy hands, 10 runs, locals weighting
9. `STAGE9.md` — Finish the game: win rate by turn 3, duds — **gates passed, core built** (`game.py`, `/games`), impact score built; going-second left
10. ~~Tests on another machine~~ — **done** (`remote.py`; duelpc runs games jobs, results keyed by device, Wake-on-LAN; see "Tests on another machine")
11. `STAGE11.md` — Pick the turn-1 board that survives: away-day runs on duelpc, board data, a learned board choice, paired A/B (prerequisites done)
12. `STAGE12.md` — A move prior for the search (GPU), only if Stage 11 pays off
13. `STAGE13.md` — Side cards: paired in/out swaps on the games harness, going first and second, with intervals (replaces the broken `optimizer.side_plan`)
14. `STAGE14.md` — A searched defender: best replies on the attacker's line, then the attacker re-plans (scripts proved readable)
15. `STAGE15.md` — Turn 1 that adapts to disruption: several goals asked, the board with the most live interruptions kept, free cards played
16. `STAGE16.md` — An AlphaZero-style player: imitation from our solver lines, the net as a search prior, a value net, then self-play (on ygorl's nets)

**Direction (user, 2026-11-19):** make the AI as close to perfect as compute allows on BOTH
sides -- the enemy must play like the user's real opponents ("extremely good and smart") --
THEN search side cards / deck changes and produce "best time to use" ratings. Converged
budgets and searched defences come before any side-card comparison.

**Added 2026-11-19** (details in STAGE15.md):
- `train <enemy> --vs <deck>`: the enemy's own game hands (`trainer.ensure_enemy_pool`);
  going second each enemy hand plays its trained goal (`trainer.enemy_goal`, any pool
  holding the same hand). Card value zone `szone` = a monster face-up as a Continuous Spell.
- `game.BREAK_X = 8`: the going-second break searches 8x. Old going-second games live on as
  devices `endgame-t1` (one-target enemy turn 1) and `brk1x` (trained enemy, 1x break).
- Rich kill labels: `game.solve_stats(log)`; `tools/ml/board_data.py` (`kill`, `recheck`
  at 16x) and `tools/ml/game_kills.py` (stored games, cron 05:40).
- `/games`: "What you must stop, by their hand" (`stops.by_enemy_hand`) and "Best time to
  use (measured)" (`timing.measured`, from `tools/experiments/use_timing.py`: fire vs hold
  at the same window, both re-searched from the window). `skills/timing.scores` is the
  confounded correlational read -- never shown.
- `tools/experiments/searched_defender.py gbrk`: the enemy defends our break with a searched
  defence on the games' positions.
- duelpc: never at night (loud) unless the user says so (trial night 2026-11-20 03:00-12:00);
  Mon-Wed usually free; Linux keeps its monitors off (OLED) and its RGB off (OpenRGB).
- Site (user: "a lot of useless pages", mobile + PC): the menu is decks / train / games / jobs;
  report, handtraps, threats, drill are off it (code kept). /games folds its analysis blocks
  and puts older-settings sections (OLD_SECTIONS in web/views.py) in labelled folds.
- Tests run on a SNAPSHOT of the database (`DUELROOM_DB`, tests/conftest.py) as they run on
  pinned deck copies: the web tests press job buttons.
- Patch 0008 (`patches/0008-card-reader-off-arena.patch`): the solver's card-reader callback
  noted unknown cards into the CardDB's SHARED set with the worker's arena live, so a node was
  wiped by that worker's next restore -> SIGSEGV in the other worker (8 of 631 NRPA-level-3
  searches, deterministic per position; found with an ASan build, 2026-11-19). Fixed with
  ArenaPause. `game._solve` still re-runs a SIGSEGV'd search once (CRASH_RETRIES) as a guard.

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

## The deck trainer (Stage 7) — what "learning" means here

`daemon.py train <deck> [hours] [hands]` or the **train** page. A fixed pool of hands
(the same hand seeds `sweep` uses) is played generation after generation; each attempt
is aimed where a better board might be (breadth pieces → unasked stacks from the pieces
that solved → near misses biggest-first → the best goal at a fresh seed), and each
hand's **best line ever found** is kept and watchable on the playmat. A line's score
is `[on field, interruptions, negates, -actions]`, where a card in hand counts ONLY if
the combo put it there -- hand traps held from the opening hand are excluded
(`trainer.line_key`; stored bests are re-scored at every session start).
Every interrupter piece is asked for (not sweep's first four), goals of `BIG_STACK`+
pieces get `BIG_STACK_X` times the budget, and every line a solve writes is ranked,
not just the solver's pick.

**The player can steer it** (`trainer_prefs`, by deck NAME; train page + the playmat's
⊘ buttons). *Counts as an interruption*: upstream reads interruptions from scripts and
misses real ones (on `imported`: Kaleido-Heart, Dragostapelia), and an uncounted card is
neither scored nor asked for. *Avoid a move*: an activation becomes `--no-activate
card@zone` + `--no-chain card` (the chain filter has no zone, so it is broader); summons
have no solver flag, so an avoided summon is only never asked for as a goal and ranked
down. The key is `[-avoided, on_field, interruptions, negates, -actions]`, so any line
avoiding them beats any that does not, and the old best stays until one is found. The learning curve
is the pool's mean best on-field interruptions per generation, so it can only rise or
stay flat. Bests are seeded from this exact deck's stored results (matched by card list,
not file name; `trainer_hands.deck_hash`). Stop = a request honoured between
generations; every attempt is committed as it lands, so a killed run resumes with no
repeated (hand, goal, draw).

**Starter test** (`/starters/<deck>`, `starters <deck> <1-3> [hours]`, job kind `starters`):
the trainer over every k-card hand the main deck can hold (copies respected, hand traps left
out by default, `--skip` more), pool key `trainer.starter_key(dh, k)` so it never mixes with the
training hands; no 5-card bootstrap; 60 s tries by default; ends at a plateau. A hand that makes
nothing in generation 1 gets no more tries (`plan` has nothing to retry). Sizes for imported:
24 / 286 / 2262 hands (~0.5 / 5.5 / 44 h first generation at 60 s on 6 workers).

**Card values** (`card_values`, keyed by zone field/grave/hand, train page "Card values", `trainer.set_value`): what one
interruption of a card is worth, 0-5 in halves, default 1. `line_key`'s `on_field` and
`interruptions` are SUMS of those values (`negates` stays a count), so a Desirae set to 2
outranks an Evolzar on an equal board. A value in a zone the scripts do not read (a Tearlament
in the GY) also counts the card there; hand copies held since the opening never count.
Setting one re-scores stored bests at once. It
steers which line is kept and which board is aimed at next, not the solver's search itself.

**Extenders** (`trainer_prefs` kind `extender`, train page): the player's bridge
cards, the ones a line should go THROUGH (the material that makes the rank 6 and
mills). Three effects: every attempt gets `--hint <code>` for each (an NRPA sampling
bonus, a nudge); once a hand has a best line, `plan` step 1b asks for that best board
through each extender as its own goal `[*board, "ext:<code>"]`, asked again whenever
the best board changes, where the `ext:` requirement becomes `--summon-min` (monster)
or `--resolve` (spell/trap) -- checked at the goal, so a line skipping it does not
solve -- and counts as a piece for `BIG_STACK`; and the key gains an `extenders` term
(distinct extenders summoned/activated, via `playback.line_moves`) as a tiebreak below
the board: `[-avoided, on_field, interruptions, negates, extenders, -actions]`.
`split_goal` strips `ext:` before anything that wants board targets (the solver's
`--target`, `game.py`'s turns 1 and 3). Verified on a real solve 2026-11-02: imported
hand 0's best board through Necroquip Princess solved in 68 s and the line summons it.

**Budget: 180 s per attempt, not sweep's 60 s** (`TRAIN_MS`, changed 2026-11-02 at
the player's request: "smart, however long it takes"). 60 s is where sweep's BRICK
count settles, a cheaper question than a hand's best line; the misses the player
reported are long lines where an early material choice pays off ten decisions later.
`PLATEAU` went 3 -> 6: 3 stopped the first deck after 9 attempts a hand.

**The solver does not get smarter; the deck's known lines do.** Measured before
building, 2026-10-31: the combo solver's own policy warm-start from a corpus of past
lines (`--adapt`) is live (90% corpus agreement) but at EQUAL wall time no better than
plain extra budget (10 s: 22/24 base, 23/24 adapt, 24/24 base at 20 s; hard goals
10/24 vs 11/24). Harness: `tools/experiments/adapt_ab*.py`. Untried: `--options`
macros, a per-starter corpus, `--ctx-shrink`. ygorl's PPO/BC on CPU learned nothing
usable for combo decks (upstream's own measurement).

## Card impact (`skills/impact.py`)

Games page "Card impact" per enemy deck, or `daemon.py impact <deck> [enemy]`. A card
is USED in a game when you summoned or activated it (any turn, incl. hand traps on
theirs), read once per game from its replay (~0.05 s) into `game_used`, keyed on the
`game_results.line` it was read from (re-recorded game = read again). `game.run`
records it as each game lands; the page reads at most `IMPACT_READ_S` (3 s) of backlog
per load; the CLI reads everything. Only your own decklist's cards count (Angelechy
Enlisted moves itself onto your field and its next trigger is yours).

Impact = Mantel-Haenszel risk difference of win rate, used vs not, over strata
**(your hand, their hand)**, with an Agresti-Caffo 95% interval. Stratifying by hand
alone was measured wrong on imported vs angelechy: Ash Blossom scored -67 (you hold it
for their strong hands) and +1 by (hand, enemy hand). Consequences: turn 1 is solved
once per pairing and reused, so turn-1-only cards are "every game" (no comparison);
most cards score > 0 (more of your deck came out = the game went well), so read
them against each other; "few games" under `MIN_SIDE` (5) compared games a side.

## What you stopped (`skills/stops.py`)

Games page "What you stopped", or `daemon.py stops <deck> [enemy]`. Asked 2026-11-05:
"which of my opponent's cards getting stopped won me the game the most, and which of
my cards are doing that". Read from the same replays as impact into `game_stops`.
A stop is a link of theirs **negated** (MSG_CHAIN_NEGATED/DISABLED) or a card of
theirs **removed** off the field/hand by effect while a link of YOURS resolves. Per
enemy card: stopped vs went-through among games they played it; per your card: games
it stopped something vs not -- both through `impact.compare` (same strata, same MH).

**Negation attribution is the trap.** The engine reports a negation either while the
negator resolves (Ash) or only when the victim resolves (Evolzar Lars) -- both seen in
real games. "The link resolving" credits the victim to itself in the second case, and
"any link above" credited their own Dominus Impulse negating their own Dominus Spark
(30 games) to "a field effect". Rule: resolving link if above the victim, else your
nearest link above it; only theirs above = not a stop; nothing above = field (0).

**First read (imported vs angelechy, 1200 games):** on their turn 2 Evolzar Lars is
almost the only thing that stops anything, and the greedy defender spends it on The
Fallen & The Virtuous in 336/382 games (+7 ± 28) when stopping Pot of Desires (+23)
or Primite Lordly Lode (+17) is worth more. Ash had no turn-2 window in any game.

## Finishing the game (Stage 9) — the rooted solver

`daemon.py games <deck> <enemy>` or the **games** page (also: `train <deck> ... <enemy>`
trains until 3 generations in a row improve nothing, then plays them). Going first:
your turn 1 toward the trainer's best goal for the hand, the enemy's turn 2 into your
board (first `--goal-win` for `OTK_MS` -- a kill is a loss -- then its payoff
target), your turn 3 as a `--goal-win` search. **Win = their LP 0 by the end of turn
3; dud = no win in any game; near-dud = under `NEAR_DUD_RATE`.** Every game is
replayable on the playmat, all three turns.

**It runs on a SECOND solver binary**, `vendor/ygorl/build/combo-solver-rooted`, built
by `tools/build_rooted_solver.sh` from `patches/0002-rooted-search.patch` (byte-identical
rebuild verified). The pinned `engine.SOLVER_BIN` is untouched, so no existing
measurement changed. What the patch adds, and the two real bugs it fixed:

- `--root <prefix>`: every phase (probe, NRPA, finisher) roots at the position the
  prefix reaches. This is what ISSUES.md #4 needed -- only the finisher used to see a
  resumed root. Gate A, empty field, 8 hands, 60 s: kewl-tune going first 8/8, old
  `--approach` 0/8, `--root` 8/8; snake_eye 7/8, 0/8, 7/8, the same hand failing both.
- **BUG: `SELECT_PLACE` answered the zone owner relative to the chooser**; the core
  wants the absolute player. Harmless as player 0, a MSG_RETRY on EVERY summon as
  player 1 -- the "second blocker" patch 0001 could not name.
- `--goal-win`: a solution is a duel the target player wins. **BUG/TRAP: ocgcore does
  not stop at MSG_WIN** -- it keeps processing until its queue is empty -- so a won
  duel was never seen until the step was cut there. Gate B (vanilla positions with
  known answers): lethal and clear-then-lethal found, both one-short positions empty.
  Rollouts score damage dealt and blockers removed; without that the first real game
  found no lethal, with it lethal was found (verified by replay).
- `--opp-greedy [--opp-respond codes]`: the non-searching player DEFENDS -- first
  eligible chain effect per window, never onto its own link -- instead of passing.
  Superseded as the default by the smart defender of patch 0004 (below).

**Rebuild ALL solver objects after touching `search.h`.** Recompiling only
`search.cpp` after adding a member left `main.o` on the old layout and the binary died
silently in the probe; `build_rooted_solver.sh` compiles everything.

Fairness, checked rather than assumed: the enemy's OPENING hand per seed is asserted
identical across your hands (`check_shared_enemy_hands`). Their turn-2 draw is not, by
design -- Maxx "C" during your turn draws them cards off the top. Turn 1 is solved once
per (hand, board, enemy hand) and reused by every run.

### Search speed: patch 0003 (2026-11-06)

Asked: faster searches, quality first. Two changes, each gated on a measurement that
could have rejected it; both on the ROOTED binary only (the pinned one is untouched).

- **`--stop-on-win 4`** (`game.STOP_ON_WIN`) on every `--goal-win` search (your lethal
  turn, their turn-2 kill check). A worker used to stop only after 16 solutions of its
  own, and winning rollouts repeat the same lines, so a WON search still burned the
  whole budget. Win/lose is a yes/no; the rest only picked a cheaper line. Never set
  on the probe (its lines are not written: a probe win stopping the rollouts would end
  with NO line). A/B on 36 stored turn-3 positions (`tools/experiments/stop_on_win_ab.py`):
  full budget 24/36, at 4 25/36, at 1 26/36, no win lost; won searches 46 s -> 25 s.
  Non-wins still use the full budget, by design.
- **Kernel page tracker, `R2V_ARENA_UFFD=1`** (`game.ARENA_ENV`). The arena snapshots by
  write-protecting itself; the POSIX port did it with SIGSEGV + one mprotect per page per
  generation -- measured ~100k signals and ~100k mprotect per 8 s per solver, MORE kernel
  CPU than search CPU, plus a cross-CPU TLB flush per mprotect with 2 threads. Linux 6.7+
  has GetWriteWatch for real: userfaultfd async write-protect (`UFFD_USER_MODE_ONLY`, so no
  root and `unprivileged_userfaultfd=0` is fine) + the `PAGEMAP_SCAN` ioctl, which reads
  and re-arms the marks in one call. Proven the SAME search: deterministic mode gives
  identical counters and byte-identical solutions (`uffd_identity.py`; the later finisher
  phase is wall-clock, so it differs -- it did 2.1x the expansions per root). At the
  games' settings: **1.88x the states in the same 60 s**, wins 8/12 -> 9/12, none lost
  (`uffd_throughput.py`); a batch went user 3m44/sys 5m09 -> user 11m05/sys 2m09. Falls
  back to the old tracker by itself if userfaultfd is unavailable.
- `R2V_ARENA_VERIFY=1` reports "barrier INCOMPLETE: page 1154" with BOTH trackers, same
  page and offset: pre-existing, not from 0003. Unexplained; worth a look.
- Not done: the trainer, sweep and every turn-1 solve use the PINNED binary, which still
  has the old tracker. Porting the arena half of 0003 there is the next big saving.

### The smart defender: patch 0004 (2026-11-11)

Asked: make the AI play smarter. The 4800 games of 2026-11-09 (imported vs
Angelechy-Meta, 2400 a side) said where the play was dumb: going first we won 19% and
LOST ON THEIR TURN 2 in 60%, at the same rate whether our board held 1 or 4
interruption pieces, and in 1866 of 2370 games our board stopped nothing at all. One
traced game (h0/e1000): the enemy opened its turn with a hand effect onto an EMPTY
field, the greedy defender fired S:P Little Knight at that first window, and its "first
legal choice" targeting banished our own Lars and S:P with it ("2 face-up monsters on
the field, including a monster you control"). Whole board gone before they had summoned.

Patch 0004, rooted binary only (`tools/build_rooted_solver.sh` applies it; `game.require`
refuses an older binary when `game.DEFENDER == "smart"`):

- `--opp-smart`: the defender answers a window with the eligible piece listed FIRST in
  `--opp-respond` (`game.respond_order`: hand traps, then negates, then the rest) rather
  than the first option the core lists, and answers follow-up card selections by value
  (its own lowest-ATK card given up, the attacker's highest taken) rather than "first".
- `--opp-hold <codes>` (+ `--opp-hold-until n`, default 1): those pieces wait until the
  attacker controls n monsters. `defender_args` holds everything but the hand traps.
- **Enumerator bug found on the way:** `MSG_SELECT_CARD` deduplicates candidates by code
  BEFORE forming subsets, so "select 2" among two copies of one card left a pool of one,
  nothing was emitted, and the branch was KILLED as a prompt with no default answer --
  688 times in a 5 s search, every one of them the DEFENDER's prompt (Skull Archfiend of
  Chaos x2), so the attacker's rollouts died at our hands for no game reason, a fake
  survival. Below the minimum the copies now stay as distinct choices. Dead ends 135 -> 0.
- **A turn that never touches its target now writes a best approach** (most own monsters
  at equal overlap; under `--goal-win` the win progress, as before). Going second, 19%
  of games were `no_t1` because the enemy's turn 1 had no line at all: the position
  h0/e1006 is a hand of two Ash, Fuwalos and two bricks -- it now summons Ash and passes.

**Measured, `tools/experiments/defender_bench.py`:** the enemy's turn-2 kill search (20 s,
the games' seed) on all 237 stored going-first turn-1 positions, four arms, paired:

| arm | defender | lost | vs greedy |
|---|---|---|---|
| A | greedy, live binary | 142/237 (60%) | -- (the stored games: 60%) |
| B | greedy, new binary | 142/237 (60%) | 1 saved, 1 newly lost (the noise floor) |
| C | smart, no hold | 128/237 (54%) | 27 saved, 13 newly lost |
| D | smart + hold | **90/237 (38%)** | **65 saved, 13 newly lost** |

Then the hold-until family on the same positions: E (hold until 2 monsters) lost
106/237, F (until 3) 145/237 -- worse alone, but lost under BOTH D and E: 67 (28%),
under D, E and F: 63 (27%), under all four with greedy: 57 (24%). Two scripts buy most
of it, so `DEFENCES = (1, 2)`; adding a no-hold script (`(0, 1, 2)`) reaches 25% for one
more search per game. The other side of the coin, `defender_bench_t3.py` (60 stored
turn-3 positions, 40 won + 20 not, 60 s): your lethal against their greedy defence 40/60,
against smart + hold 39/60 (2 lost, 1 gained -- noise). By turn 3 their board is what it
is; the smarter script matters on THEIR turn into YOUR board. Caveat: each extra script is one more 20 s randomized
search for the attacker to miss a real kill in (the A-vs-B noise floor is 1 flip in
237 per arm), so a family's survival is slightly flattered. **The choice is built:
`game.DEFENCES`**, the `--opp-hold-until` scripts a turn
tries. Their turn: the kill check runs once per script, a loss needs every one beaten,
and the script that kept the most LP plays the rest of the turn. Your lethal turn: a win
must beat every script (the enemy picks the one that stops you). One kill search per
script; one lethal search per script on won turns only. `(1,)` = a single script.

**The limit, seen in a trace (h0/e1009, arm D):** any fixed defender script is visible
to the attacker's rollouts. With Lars held until a monster was out, the enemy played ONE
activation to draw it, then effect-less Synchro summons and attacks for lethal. A
smarter script moves the bar; only a family of scripts (or a search on both sides) moves
it further. Trivikarma, counted as a piece on 40 of 237 boards, needs Visas Starfrost
and is dead without it. **Fixed 2026-11-18 at the user's request** (`drill.NEEDS_ON_FIELD`,
`drill.never_live`): imported runs no Visas, so the trainer no longer counts or asks for it,
and `game._pieces` counts only live pieces (a bare Desirae neither). Bests re-scored: hands
0, 1, 2, 5, 6 lost one interruption each; re-trained 2026-11-18.

**Daily banlist check (2026-11-18, user asked):** cron 06:15 runs `daemon.py banlist`
(log results/banlist.log): Project Ignis's upstream 0TCG.lflist.conf over HTTPS vs the engine's
LFLists checkout, plus the card mirror's own sync. A list not seen before shows a banner on every
page (what changed, which decks break it); `daemon.py banlist ack` or the banner's dismiss clears
it. Moving the ENGINE to the new list is manual (LFLists + CardScripts + BabelCDB together, then
setup and the gate). First run found 2026.10 TCG (45 changes) over the engine's 2026.09.
**Decks live in `engine.DECKS`** (env DUELROOM_DECKS overrides); the tests use pinned copies.

**Patch 0007 (2026-11-18), `--no-self-target`:** at the searcher's single-pick target prompt
of a listed card's effect (its link is already on the chain there), picks of only its own
cards are dropped. `game.NO_SELF_TARGET = (S:P Little Knight,)` on our turn 1 and lethal turns:
its summon banish took our own Engraver from the GY in 16 of 240 turn 1s. `game._solve` now
raises on any non-zero solver exit (a crash is never a "no win"), and `remote.run` tells a
remote crash (ssh 255, device answering) from a lost device.

### Turn 1 in the trainer's duel (2026-11-13)

Asked to keep making the AI play smarter. Our turn 1 reached its trained goal in 39 of 237
going-first positions, and `tools/experiments/t1_fired.py` showed the enemy fired nothing
in 148 of them -- so the search was missing goals the trainer had reached. Two fixes
(ISSUES.md #72):

- **`trainer.hand_start` / `game.ordered_deck`**: turn 1 (and your deck going second) is
  played on the trainer's deck order and core seed for that hand, not the .ydk file order
  and the enemy seed's randomness. On the old duel 23 of 24 best lines could not even be
  replayed. The enemy's opening hand per seed is unchanged (its shuffle is host-side).
- **`game.respond_codes`**: each side's defender answers with every interruption its main
  deck runs, read from the scripts, not only `scenarios.HANDTRAPS`. Angelechy-Meta's six
  Mulcharmies had never fired. `is_hand_trap` decides what the smart defender never holds.

**Stored turn 1s are cached** (`game_turn1`), so a matchup already played keeps its old
turn 1s, old defender list and old greedy defender until its rows are cleared.

Measured and NOT adopted: with the real interruptions now firing, warm-starting from the
best line (`--adapt`) or replaying it first (`patches/0005-approach-line.patch`, kept
UNAPPLIED like 0001) reached 4/40 and 5/40 against 2/40 plain, pieces even -- noise
(`tools/experiments/t1_bench.py`). The gotcha that cost a day there: the arena's `Pop()`
RESTORES as well as pops; drop a snapshot with `Discard()`, and never Discard a nested
level the caller will Restore through (segfault).

### Going second (2026-11-06)

`games <deck> <enemy> --second`, or "you go: second" on the games page (and on the
train page for the games at a plateau). **Their turn 1, your turn 2; a win = their LP 0
on your turn 2**, i.e. breaking their board and killing through it. Every game table
carries `first` in its key (1 = you went first; `db._migrate_game_first` marked every
older game 1), so both sides of a matchup sit side by side and never mix in summary,
impact or stops. Files: `results/games/<deck>/<enemy>/second/`.

- **The enemy faces the SAME opening hands as going first.** `game.enemy_hand` deals the
  going-first template and reads player 1's hand -- checked equal to the stored going-first
  hands for seeds 1000/1001/1005. The enemy is side a (`--deck`/`--hand`).
- **Your hand is seated on side b** by `breaking.deck_with_hand` on your deck shuffled by
  the enemy seed (`--hand` never reaches side b), so your turn-2 draw is fixed per
  (hand, enemy hand). `enemy_turn1` asserts BOTH dealt hands from the position itself.
- Your hand traps answer their turn 1 (`--opp-greedy` with HANDTRAPS + your interrupters);
  their board's pieces + their hand traps answer your turn 2. No line at all on their
  turn 1 = `no_t1` (the bare template would deal them the bottom of their list).
- **Your turn 2 is steered by the first goal piece NOT held in hand.** Hand 4's goal opens
  `42141493@hand` (keep Fuwalos): as a target it posts 0 cards, all 9 lines the search
  reached were rejected, no approach was written, and the game read "0 damage".
  **Going first had the same bug on turn 3 until 2026-11-18** (`game.steer` now serves
  both): the 11 of 24 imported hands whose goal opens "@hand" won 6 of the 564 turn 3s
  they reached, the other hands 623 of 728. Found by Stage 13's blank gate (-3 Ash +3
  vanilla read +10% wins, all on Ash@hand hands). Those 1220 going-first games were
  deleted (turn 1s kept); the old going-first 26% understated the deck.
- Playmat: you are engine player 1, so `playback.frames(me=1)` swaps the sides and you
  sit at the bottom with your hand shown (`/watch/game/...?second=1`).
- First run (4 hands x 1 enemy hand, 60 s): no lethal, 5700 / 4800 / 5900 / 2500 damage;
  a game is ~1 search (their turn 1 is shared by the runs).

**Why going second fell 63% -> 45% (traced 2026-11-17, `tools/experiments/t2_enemy_fired.py`):**
not a regression. Since 8463177 the enemy answers with every card its deck can legally
play from the hand, and Angelechy-Meta runs 9 Dominus traps that activate FROM THE HAND
(no trap in its GY) -- the old HANDTRAPS-only list let it use 6 interaction cards. Dominus
Impulse fires in 41% of our losses vs 20% of our wins, Purge 24% vs 11%, Spark 24% vs 15%.
Same enemy opening hands as the old run, no more pieces on their boards; the old 63% also
dropped 19% no_t1 games. The 45% is the honest number.

## Tests on another machine (Stage 10, `skills/remote.py`)

The app, database and every decision stay on the server; another device only runs
`combosolver`. **Path mirroring**: the device has the same user and the same absolute
paths, so a solver command line built here runs there unchanged.

- Devices: `remote.DEVICES` — `server` (4 games at once) and `duelpc` (the gaming PC's
  Ubuntu SSD, `duelpc.local` over mDNS, 8 at once; installer `tools/duelpc/`).
- Before a job: `remote.prepare` — SSH check, else Wake-on-LAN and wait up to 4 min;
  rsync of the solver build + merged card DB + CardScripts (`STATIC`) when the stamp
  differs (`~/.duelroom-solver-stamp` both sides).
- Per solve (`game._solve` -> `_solve_remote`): push every file argument + the workdir
  (`rsync -R` to `/`), run under `timeout -s KILL` THERE (a killed ssh client would
  leave the solver running), pull the outdir. One multiplexed SSH connection
  (`~/.ssh/cm-duelroom-*`); duelpc's sshd allows 64 sessions. Exit 255 -> wake + one
  retry; lost again -> `DeviceDown` ends the run (games are resumable).
- A job's device: `params.device` (`/games` "run on" picker, `games ... --device duelpc`).
  The worker runs ONE LANE PER DEVICE, so the server and duelpc each run one job at once.
- `device` is in the key of all four game tables and every read (old rows = `server`);
  scratch folders are `<enemy>@<device>`. Different search speed = different measurement:
  never pool devices. Measured 2026-11-17 (`tools/experiments/device_bench.py`, paired
  turn-1 solves): duelpc at 8 at once searches ~1.08x the server's states/s per solve
  at 4 at once — about the same strength, twice the games per hour.
- Gotchas: duelpc's DHCP address can change (mDNS covers it); a reinstall changes its
  host key -> `ssh-keygen -R duelpc.local`. It must be booted into Linux (GRUB default).

## A learned board value? (measured 2026-11-17)

`tools/experiments/board_value.py`: on the solver's real choice (boards of ONE hand, enemy
hand's difficulty removed, 240 going-first boards / 2400 games) "target cards placed"
correlates +0.14 with winning, interruption pieces +0.18, a learned ridge board value +0.07
(intervals overlap). No case for a neural board evaluator from the games we have; the
question needs many boards per hand against a fixed enemy hand (a duelpc-sized run).

## Turn 1 through hand traps: aim for interruptions only? (measured 2026-11-17, no)

Against Angelechy-Meta the enemy fires something in 236 of 240 of our turn 1s and the full
goal is reached in 14. `tools/experiments/t1_backup_bench.py` (on duelpc) re-solved every
position with the goal cut to its interruption cards and ran both boards through the turn-2
kill check 3x on paired seeds. The cut goal is reached 122/240 (vs 14) and leaves more
interruption pieces (1.68 vs 1.29), yet LOSES MORE: 73% vs 69% of 720 checks, worse in 64
positions and better in 37 (sign test p = 0.009); still worse where it reached its goal
(79% vs 72%). The full goal's other cards (bodies, follow-up, resources kept) are worth
more than an extra interruption. Keep the full goal; do not re-run this as a backup goal.
Mulcharmy Fuwalos resolving looks bad (lost on T2 0.59 vs 0.38) but within one enemy hand
their T2 hand size does not predict losses (rho -0.01): it marks strong enemy hands.

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
- **Games use a SCRIPTED DEFENDER** (`game.DEFENDER`, patch 0004 above): ordered
  responses, board pieces held until the attacker has a monster, follow-up targets by
  value. Games before 2026-11-11 used the greedy script. Both sides get the same
  model, and either script is deterministic, so the searching side can play around it.
- **A game's turn 1 is re-solved against the enemy deck**, toward the trainer's best
  goal, with their hand traps firing. It can land a smaller board than the trainer's
  (stored lines are bound to the passive opponent they were solved against, see
  `breaking.py`), and `game_turn1.reached` says whether the full goal was made.
- **Damage on a non-winning turn 3 is read from the solver's best approach**: the
  most target overlap, then (under `--goal-win`) the lowest opponent LP. It is the
  best the search happened to visit, not a damage maximum, so it can understate.
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

**The turn-2 resume problem is solved in the rooted binary** (Stage 9 above); `second`,
`gauntlet.rank` and the side plan still call the PINNED binary and stay unsupported
until they are moved onto `--root`. A full bot-vs-bot arena (beyond turn 3, with a
searched rather than scripted defender) is still deferred; see "Why No Duel AI (Yet)".
