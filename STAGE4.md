# Stage 4 — Scenario Suite

## Goal

Stop asking "is my deck good" and start asking the three questions that decide locals:
can it play through interruption, can it break what it will face, and does it fold to a
floodgate.

All of this is the Stage 3 solver run from different starting conditions. No new engine
work, no opponent AI.

## Build — `skills/scenarios.py`

### 1. Handtrap injection (going first)
Replay your line with a handtrap fired at **every** decision point, and take the worst
case — assume a competent opponent.

Cover at minimum: Ash Blossom, Infinite Impermanence, Maxx "C", Nibiru, Droll & Lock
Bird, Ghost Ogre, Effect Veiler.

Output per handtrap: does the line survive, and what does the end board degrade to.

### 2. The handtrap matrix
Run the above against every **meta** deck's line as well, using the Stage 3 board
library. The result is a table:

| Deck | Ash | Imperm | Droll | Nibiru | Veiler |
|---|---|---|---|---|---|
| … | 60% stopped | 25% | 80% | 15% | 30% |

Weight the columns by `LOCALS_WEIGHTS` and you have a direct answer to **which handtraps
to main deck** — arguably the most actionable output in the whole system, and nobody
does it systematically.

### 3. Board breaking (going second)
For each board in the `boards` library: construct it via `engine.set_board()`, deal
yourself a going-second opening hand, and search for a win or a meaningful break.

Report per archetype:
- Break rate, with Wilson CI
- Which of your cards did the breaking (this feeds Stage 5's side deck search)
- Which boards you simply cannot beat — equally valuable to know

### 4. Floodgate check
Cheap static test: set up Skill Drain, Dimensional Barrier, Rivalry of Warlords,
There Can Be Only One, Summon Limit, and check whether your deck still functions.

**Locals run floodgates far more than top tables do.** This test costs almost nothing
and catches a real way to lose.

## Deliverables

1. Per-handtrap survival rates for your deck, going first.
2. The full handtrap matrix across the meta, weighted by your locals config.
3. Board-break rates against every archetype in the library, with CIs.
4. Floodgate vulnerability report.
5. Everything split **going first vs going second**. The die roll dominates modern
   Yu-Gi-Oh; an averaged number hides the answer.

## Gotchas

- Worst-case handtrap timing is the honest assumption, but also report best-case — the
  gap between them tells you how much the matchup rides on opponent skill.
- Multiple handtraps compound. Test the common pairs (Ash + Imperm), not just singles.
- The board library is a distribution, not one board. Weight break rates by board
  frequency or you will optimize against a board they rarely assemble.
- Going-second hands should assume the opponent's turn happened — adjust hand size and
  available draws accordingly.

---
Delete this file when the stage is complete and verified.
