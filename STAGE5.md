# Stage 5 — Optimizer

## Goal

Turn the measurements into decisions: which flex slots to change in the main deck, and
what the side deck should be for each matchup.

## Why this is tractable

Stage 1's inclusion analysis already cut the problem down. You are not searching 40
cards against a 13,000-card pool — you are searching roughly 8 flex slots against about
30 cards that real players actually run. That is three orders of magnitude off the
search space, and it is the only reason this stage is cheap.

## Build — `skills/optimizer.py`

### Candidate pool
- Flex + fringe cards from `inclusion` for your archetype
- Plus cards matching your deck's mechanical requirements (type / attribute / level /
  archetype overlap with your existing extenders)
- Filter by banlist legality

A plain text and attribute filter gets ~90% of useful pruning. Only reach for Ollama
card tagging if the pool is genuinely still too wide — it is a nice-to-have, not a
dependency.

### Main deck search
Hill climb over 1–3 card swaps:

1. Propose a swap within the flex slots
2. Re-run Stage 3 consistency and Stage 4 scenarios
3. Accept if the weighted objective improves
4. Repeat until no single swap improves

**Paired seeds are mandatory.** Both variants must see identical shuffle seeds (common
random numbers). This is the single biggest variance reduction available — without it
you need thousands of trials to see a real 2-point difference; with it, a fraction of
that.

Objective function, weighted and configurable:
- Brick rate (minimize)
- Combo completion (maximize)
- Handtrap survival, weighted by `LOCALS_WEIGHTS`
- Board-break rate, weighted by board frequency and `LOCALS_WEIGHTS`
- Floodgate resilience

### Side deck search
Same loop, different objective. For each archetype in the gauntlet:
- Which candidate cards most raise the **going-second break rate** against that
  archetype's board distribution?
- What is the minimum swap set that achieves it, and does the deck still function after
  the swap? A side card that fixes a matchup but bricks the deck is a trap.
- Output a per-matchup side plan: cards in, cards out.

15 slots against 8-ish matchups is a small combinatorial problem — a greedy pass
followed by a local search is plenty. Do not over-engineer this.

### Optional: MAP-Elites
[ygorl](https://github.com/n0tevi1/ygorl) ships MAP-Elites deck evolution via `pyribs`.
If Stage 2 landed on ygorl, this is close to free and gives you a diverse archive of
viable builds rather than one local optimum. Worth trying after plain hill climbing
works — not before.

## Deliverables

1. Ranked list of main deck swaps with measured deltas and confidence intervals.
2. A per-matchup side deck plan: cards in, cards out, measured break-rate improvement.
3. A diff report against the scraped consensus list — where your build departs from the
   meta, and whether the numbers justify it.

## Gotchas

- **Report confidence intervals on every delta.** A 1-point improvement inside a 4-point
  CI is not an improvement, and hill climbing on noise will happily converge to garbage.
- Hill climbing finds local optima. Run from several random restarts and compare.
- Guard against overfitting to the board library. Hold out a few archetypes, optimize on
  the rest, then check the result still holds on the held-out set.
- The optimizer's objective encodes your assumptions about your locals. When results
  look strange, suspect `LOCALS_WEIGHTS` and the scoring weights before the search.
- Re-run after every banlist. The flex-slot analysis shifts, and so does everything
  built on it.

---
Delete this file when the stage is complete and verified.
