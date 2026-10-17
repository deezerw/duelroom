# Stage 3 — Solitaire Solver + Validation Gate

## Goal

Given a deck and an opening hand, find the best line available. This single component is
the pilot, the combo discoverer, and the meta end-board generator — all three.

## Build

### `skills/scoring.py`
End-board evaluation. Start simple and hand-weighted; resist the urge to learn it:
- Number and quality of negates / disruptions (the dominant term by far)
- Card advantage (hand + field vs cards spent)
- GY and banish resources for the following turn
- Total ATK, and whether lethal is on board
- Penalty for cards left dead in hand

Keep the weights in one dict and make them config. You will tune these by eye against
lines you already know are good.

### `skills/solver.py`
Search the action tree over **your own** legal moves, no opponent:
- DFS/BFS with memoization on canonicalized game state
- Beam search or MCTS once raw search blows up — it will
- `MAX_DEPTH`, `MAX_NODES`, `BEAM_WIDTH` as config knobs
- Prune aggressively: skip actions that do not change the resource vector
- Return the **full action sequence**, not just the score — the line is the product

### Metrics layer
Per trial, record into `trials`: opening hand, whether the line completed, board score,
negate count, special summons used, and the line itself.

Aggregate per deck:
- **Brick rate** — no starter, or no line above a floor score
- **Combo completion %**
- **End-board distribution** — not just a mean; the spread is the interesting part
- **Opening hand classification** — god hand / normal / needs a specific draw / brick.
  More useful than a single brick percentage.
- **Per-card liveliness** — how often each card was used vs dead. This is your garnet
  detector and it falls out for free.

### Free counters
The solver already tracks special summons per line, so emit without extra work:
- **Nibiru count** — special summons before the Nibiru window; tells you if the line is
  Nibiru-proof
- **Maxx "C" cost** — how many cards you hand over if you push through it

### Line export
Dump each discovered line as a readable step sequence (EDOPro replay format if
practical). **A combo you cannot execute under pressure is worth nothing** — you need to
drill these, so they have to be human-legible.

## The Validation Gate

**Do not proceed past this stage until it passes.**

Point the solver at a deck with a well-documented combo line. Assert it reproduces the
published line in ≥ 90% of hands that contain the required starters.

If the solver cannot find a line that every player of that deck knows by heart, every
downstream number in this project is noise. This is the go/no-go for the whole thing,
and it is cheap to check now and expensive to discover later.

## Generating the meta end-board library

Once validated, point the solver at each scraped meta deck and goldfish it going first,
1000× each. Store the resulting boards in `boards` with frequencies.

This is what replaces bot-vs-bot duelling. You get:
- A realistic distribution of boards you will face, derived from actual top lists rather
  than guesswork
- Which of the top variants of an archetype is genuinely most consistent — i.e. which
  one your locals will actually be on
- Their brick rate: how often you simply win because they opened badly

## Gotchas

- **Memoize on `(deck_hash, opening_hand)`.** Deterministic, and hands repeat constantly
  across trials. On 8 cores this cache is the difference between minutes and hours.
- Canonicalize state before hashing — identical boards reached by different orderings
  must collide, or the cache does nothing.
- Searching to true optimum is intractable for modern decks. Beam search that reliably
  finds the known line is worth more than an exhaustive search that times out.
- Scoring weights are where bias enters. Sanity-check them against lines you already
  judge good before trusting any ranking built on top.

---
Delete this file when the stage is complete and verified.
