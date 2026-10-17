# Stage 1 — Meta Ingestion & Card Database

**No engine required.** This stage runs entirely on scraped data and produces real,
usable output before any C++ is built. Do it first.

## Goal

Know what the format looks like: every card, every top list, and — the point of the
stage — which slots in a deck are actually contested.

## Build

### `skills/card_db.py`
- Pull the full card pool from `https://db.ygoprodeck.com/api/v7/cardinfo.php`.
- **Rate limit is 20 req/sec and exceeding it blocks your IP for an hour.** One bulk
  fetch of the whole database, cached locally, is the correct access pattern. Their ToS
  explicitly asks for local caching and no image hotlinking.
- Store into `cards`. Include banlist status and archetype.
- Refresh on `checkDBVer.php` change, not on a timer.

### `skills/meta_scrape.py`
- There is **no decklist endpoint** in the official API — cards only. Decklists come
  from the deck-search pages.
- Try a plain `httpx` GET and parse `__NEXT_DATA__` first. Per the OpenClaw notes this
  usually yields full structured results with no browser. Only reach for Playwright if
  that fails.
- Pull `LISTS_PER_ARCHETYPE = 50` per archetype, not 5. The extra lists cost nothing and
  are what make inclusion rates meaningful.
- Store into `decks` / `deck_cards`. Dedup on `list_hash` (sorted card multiset).

### `skills/deck.py`
- `.ydk` parse and write (`#main` / `#extra` / `!side`, one passcode per line).
- Deck model with legality checks against the banlist and the 40–60 / 15 / 15 limits.

### `skills/stats.py` (start it here)
- Hypergeometric opening-hand math: P(at least N copies of a card or card group in the
  opening 5 or 6). Exact and instant — no simulation.
- Wilson score intervals for later stages.

### Inclusion analysis
Compute into the `inclusion` table, per archetype and section:
- `pct_lists` — fraction of lists running the card at all
- `avg_copies` — mean copies among lists that run it

Classify:
- **Core**: ≥ 90% inclusion. Never touched by the optimizer.
- **Flex**: 20–60%. This is the search space.
- **Fringe**: < 20%. Candidate pool, surfaced but not auto-searched.

## Deliverables

1. `cards` populated and refreshing correctly.
2. ≥ 50 lists per tracked archetype in `decks`.
3. A printed flex-slot report for any archetype: core / flex / fringe with percentages.
4. Side-deck inclusion rates — i.e. a ranked list of what the meta is aiming at you.
5. Hypergeometric brick math for your own deck. **This alone is actionable** and is why
   this stage is first.

## Also set up here

- `LOCALS_WEIGHTS` config file: hand-maintained archetype shares for your actual room
  (e.g. `{"Snake-Eye": 0.30, "Yubel": 0.20, "rogue": 0.25, ...}`). Every later gauntlet
  weights by this, not by global meta share. Without it you tune for a field you never
  sit in.

## Gotchas

- Scrape weekly and keep history. Diffing inclusion rates over time surfaces tech cards
  before they are common knowledge, and it is nearly free once the scraper exists.
- Record the format with every list. Mixing formats silently corrupts inclusion rates.
- TCG and OCG banlists differ. Store both; pick one per run.

---
Delete this file when the stage is complete and verified.
