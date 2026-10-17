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

Interactive commands at the prompt:
- `meta <format>` — scrape/refresh meta decklists and recompute inclusion rates
- `goldfish <deck.ydk>` — consistency run for one deck
- `scenarios <deck.ydk>` — handtrap matrix, board breaking, floodgate checks
- `optimize <deck.ydk>` — flex-slot and side deck search
- `quit` / `q` — exit

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
`negates`, `special_summons`, `line` (JSON action sequence)

**`boards`** — end-board library: `archetype`, `board_state` (JSON), `frequency`,
`source_run`

## Business Logic Notes

- **Flex slots:** cards in 20–60% of top lists. The optimizer searches only these, which
  cuts the space from 13,000 cards to roughly 30 realistic candidates.
- **Paired seeds:** deck variants are always compared on identical shuffle seeds (common
  random numbers). This is the single biggest variance reduction available and it is
  mandatory, not optional.
- **Hand-level memoization:** `(deck_hash, opening_hand) → best_line` is deterministic.
  Opening hands repeat constantly across trials; this cache is the difference between
  minutes and hours on 8 cores.
- **Play/draw split:** every metric is reported split by going first vs going second.
  The die roll dominates modern Yu-Gi-Oh and an averaged number hides the answer.
- **Locals weighting:** the gauntlet is weighted by a hand-maintained config, not by
  global meta share. Top-5 YGOPRODeck lists are the optimized, funded field; locals run
  budget builds, last format's deck, and rogue piles.
- **Don't simulate what math solves:** pure "do I open card X" questions are
  hypergeometric — exact and instant. Reserve the engine for questions about lines.
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
2. `STAGE2.md` — Engine harness (`engine.py`)
3. `STAGE3.md` — Solitaire solver + validation gate (`solver.py`, `scoring.py`)
4. `STAGE4.md` — Scenario suite (`scenarios.py`)
5. `STAGE5.md` — Optimizer (`optimizer.py`)

Stage 1 deliberately came first: it needs no C++ build, produces real output on day one,
and de-risks the project while Stage 2 is being fought. **Stage 2 is next, and is the
whole risk of the project** -- time-box each engine option before trying the next.

**Deferred:** a Stage 6 bot-vs-bot arena, once Stages 1–5 are trusted. Not planned in
detail yet — see "Why No Duel AI (Yet)" above for the trade.
