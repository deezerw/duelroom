# Stage 4 — Scenario Suite

**Rewritten after Stage 2.** The handtrap machinery exists upstream. Board breaking is
the open question and is the real work of this stage.

## 1. Handtrap matrix — mostly built

`tools/funnel_eval.py` takes `--fire PASSWORD` (repeatable) and re-runs every solved
hand with each handtrap fired, reporting survival. That is the handtrap matrix.

Run it for your deck and for every meta deck in the Stage 3 board library, over at
minimum: Ash Blossom, Infinite Impermanence, Maxx "C", Nibiru, Droll & Lock Bird,
Ghost Ogre, Effect Veiler.

| Deck | Ash | Imperm | Droll | Nibiru | Veiler |
|---|---|---|---|---|---|
| … | 60% stopped | 25% | 80% | 15% | 30% |

Weight the columns by `locals.toml` and you have a direct answer to **which handtraps to
main deck**. This is the most actionable output in the project and nobody does it
systematically.

Two things duelroom must add:
- `funnel_eval` wants a per-deck `--targets` JSON. `solve_blocking` plans targets
  automatically from card scripts instead. Either generate a targets file from a
  `solve_blocking` pass, or add `--fire` handling to the blocking path.
- Test the common **pairs** (Ash + Imperm), not only singles. Handtraps compound and
  locals players hold more than one.

Nibiru counts and Maxx "C" cost fall out of the solver's special-summon tracking for
free — emit them alongside.

## 2. Board breaking — VERIFIED available (2026-10-17)

The open item is closed. `ygorl.engine.puzzle` is built for exactly this: a `Puzzle`
takes `Placement(password, location, sequence, team, position)` tuples and creates a
duel whose cards are added with `OCG_DuelNewCard` instead of shuffled decks. No draw,
starts in player 0's Main Phase 1, deterministic like any other duel.

Confirmed by construction: Apollousa in the opponent's monster zone, a set Infinite
Impermanence in their spell/trap zone, Ash in our hand -- queried back as team 1
monsters 1, team 1 spell/trap 1, team 0 hand 1. That is the exact shape every
going-second puzzle needs.

Two notes for the implementation:
- `Puzzle.__init__` takes `cards, seed, lp, rule_flags` but no `snapshots` flag, so
  `duel.snapshot()` raises "this duel was created without snapshots". Branch-and-return
  over a puzzle therefore means re-running from the seed (deterministic, so correct,
  just slower) unless the puzzle path is extended to pass the flag.
- Bounds are checked before entering the core (player 0/1, exactly one `LOCATION_*`,
  monster seq < 7, spell/trap seq < 8) and raise `ValueError`. The core itself does not
  check and out-of-range writes corrupt memory, so do not bypass that layer.

For each board in the Stage 3 library: construct it, deal a going-second hand, and
search for a break.

Report per archetype:
- Break rate with Wilson CI (`stats.wilson`)
- Which of your cards did the breaking — this feeds Stage 5's side deck search
- Which boards you simply cannot beat; equally worth knowing

Weight break rates by board *frequency*. The library is a distribution, and optimizing
against a board they rarely assemble is wasted effort.

If arbitrary board construction turns out to be unavailable, the fallback is to start
from a replay of the opponent's solved turn-1 line (`ygorl branch` forks a replay at a
decision point) and take over from there. Slower, but it uses only verified machinery.

## 3. Floodgate check

Cheap static test using the same board-construction path: Skill Drain, Dimensional
Barrier, Rivalry of Warlords, There Can Be Only One, Summon Limit. Does the deck still
function?

**Locals run floodgates far more than top tables do.** Costs almost nothing and catches
a real way to lose.

## Deliverables

1. Per-handtrap survival for your deck, going first, singles and common pairs.
2. The full handtrap matrix across the meta, weighted by `locals.toml`.
3. Board-break rates per archetype with CIs — or a documented finding that arbitrary
   board construction is unavailable and the replay-fork fallback in its place.
4. Floodgate vulnerability report.
5. Everything split going first vs going second.

## Gotchas

- Worst-case handtrap timing is the honest default, but report best-case too: the gap
  tells you how much the matchup rides on opponent skill.
- Going-second hands must assume the opponent's turn happened — hand size and draws
  differ from going first.
- A side card that fixes a matchup but bricks the deck is a trap. Always re-run Stage 3
  consistency on a sided configuration.

---
Delete this file when the stage is complete and verified.
