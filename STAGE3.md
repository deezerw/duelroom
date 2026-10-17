# Stage 3 — Validation Gate, Scale, and Storage

**Rewritten after Stage 2.** The original plan was to build a solver, a board
scorer, and a metrics layer. ygorl already has all three. What is left is smaller and
more important: prove the thing is trustworthy, run it at a useful scale, and get the
results into our database where Stages 4 and 5 can use them.

## What already exists (do not rebuild)

| Need | Where |
|---|---|
| Action-tree search | `ygo-combo-solver` (NRPA + Levin + Iterated-Width, ~30k lines C++), wrapped as a subprocess |
| Target planning | `ygorl.solver.blocking` reads the deck's own scripts and plans targets automatically — works on any deck, no hand-written target file |
| Board scoring | end-board interruption counting, `blocking_summary.json` lists them by card name |
| Line verification | every line is re-run in the rules core by fresh replay before it is kept |
| Line export | `.yrpX` replays, readable in EDOPro |
| Paired seeds | `funnel_eval` uses the same shuffle permutations across every deck, by construction |
| Resumability | records append as they finish; a rerun skips what is done |

Driven through `skills/engine.py` — `solve_blocking()` and `duel()`.

## 1. The validation gate — do this first

**Nothing downstream means anything until this passes.**

Point the solver at a deck with a well-documented combo line and assert it reproduces
that line in ≥90% of hands containing the required starters. Export the line as `.yrpX`
and actually watch it back in EDOPro — the verification is automated, but a human
confirming the line is the real check that targets and scoring mean what we think.

Early signal is good: Kewl Tune solved 8/8 hands at a mean of 3.0 interruptions, Branded
4/8 at 1.25. Plausible, but 8 hands proves nothing. The gate is a named deck, a named
line, and a pass rate.

## 2. Budgets — reproducibility over speed

Use `--max-rollouts` (a solver *count* budget), not `--solve-ms` alone. A wall-clock
budget makes results depend on machine load, so the same deck scores differently on a
busy box. A rollout budget is reproducible; set `--solve-ms` well above it as a backstop.

`SOLVE_MS` is 10s and is **not a free knob**. Upstream measured 5s/hand producing
systematic false bricks on long-combo decks — +17 to +19 percentage points of hands
wrongly reported unsolvable. A cheaper run is a wrong run.

Observed cost here: 18–31 s/hand on 6 workers. 1000 hands per deck is 1.5–3 hours of
wall clock. Plan runs accordingly, and prefer more decks at 100 hands over one deck at
1000 until the pipeline is trusted.

## 3. Store results in `data/duelroom.db`

Right now output lands in `results/*.jsonl` and `results/*.json`. Load it into the
schema that already exists:

- `runs` — one row per solve sweep. **Record `engine.versions()` into it.** Those
  components move independently and a mismatch changes results silently.
- `trials` — one row per hand: seed, opening hand, solved, interruptions, the line.
- `boards` — end boards keyed by archetype, with frequency.

## 4. Metrics duelroom adds on top

The solver gives per-hand outcomes. These aggregate them, and none need the engine:

- **Real brick rate**, next to Stage 1's hypergeometric floor. The gap between
  "opened no starter" and "opened a starter and still had no line" is the interesting
  number and nothing else measures it.
- **Opening hand classification** — god hand / normal / needs a specific draw / brick.
  More useful than one percentage.
- **Per-card liveliness** — how often each card appeared in a hand that solved versus
  one that bricked. This is the garnet detector, and it feeds Stage 5's candidate pool.
- **Interruption distribution**, not just the mean. A deck averaging 3 that is bimodal
  between 1 and 5 plays nothing like one that always makes 3.

## 5. Meta end-board library

Export the top archetypes from Stage 1 (`export <archetype>`), solve each going first,
and store the end boards in `boards`. This is what replaces bot-vs-bot duelling: a
distribution of the boards you will actually face, derived from scraped tournament
lists rather than guesswork. It also yields each meta deck's brick rate — how often you
win because they opened badly.

## Deliverables

1. A passing validation gate, with the deck and line named in the commit.
2. `runs` / `trials` / `boards` populated, with version pins recorded.
3. Brick rate, hand classification, per-card liveliness, interruption distribution.
4. End-board library for the top 8 archetypes by `locals.toml` weight.

---
Delete this file when the stage is complete and verified.
