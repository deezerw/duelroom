# Stage 9 — Finish the game: win rate by turn 3

Requested 2026-11-02. Going first: you build your board on turn 1, the enemy plays
turn 2 into it, and on turn 3 (your second turn) you try to win. **A hand wins if
the enemy's LP reaches 0 by the end of turn 3. A hand that wins in no run against
any enemy hand is a dud.** Boards are ranked by win rate.

Delete this file once the gates pass and the win-rate run is built and verified.

## Status (2026-11-02)

**Both gates PASSED and the game is built.** See CLAUDE.md "Finishing the game".

- Gate A: `--root` (patches/0002) -- kewl-tune 8/8 vs going first 8/8, snake_eye 7/8
  vs 7/8, the same hand failing both; old `--approach` 0/8 on both. Done via option 1
  (C++), plus a real bug fix (SELECT_PLACE absolute player).
- Gate B: `--goal-win` -- known-answer positions all correct, wins verified by replay.
  Needed a second fix: ocgcore keeps processing past MSG_WIN.
- Built: `skills/game.py` (T1 / T2 with lethal check / T3), greedy defender
  (`--opp-greedy`), damage-guided lethal search, `game_turn1`/`game_results`,
  fairness check on enemy OPENING hands, `games` CLI, `/games` page, whole-game
  playmat replays, `train ... <enemy>` plateau trigger, near-dud tier.
- First real game (imported hand 0 vs snake_eye): enemy Maxx C + Veiler + Nibiru on
  T1, payoff on T2, lethal found on T3 (3400 + 3000 + 2700), replay-verified.

**Left before this file can be deleted:**
- ~~Impact score per card from game results~~ -- done 2026-11-02 (`skills/impact.py`,
  games page "Card impact", `daemon.py impact`); stratified by (your hand, their hand),
  not by hand alone -- see CLAUDE.md.
- Rank top-3 boards per hand (needs STAGE8 item 1).
- Measured cost of a full run (the 2.5 min/game estimate is unmeasured at 60 s).
- Going-second variant (their T1 board, your T2 lethal) -- the user liked it; same pieces.

## Why this is a separate stage: it is blocked today

ISSUES.md #4: the solver cannot search from a position a duel has already reached.
Same deck, target and budget, empty field: turn 1 SOLVES, a resumed turn 2 or turn 3
finds nothing. A resumed root gets only the finisher's unguided walk, with no plan,
probe or repertoire. Both turn 2 (enemy) and turn 3 (you) are resumed roots, so
this whole stage rests on fixing that. **Do not ship a win rate from the broken
search** — it would print "dud" for every hand, which looks like a result.

## Gate A — resumed-root search (spike first, before any other code)

Pass = on paired hands, a resumed root with an empty field solves at least 80% as
often as the same hand going first. Try in order, stop at the first that passes:

1. Apply `patches/0001-going-second-turn-cuts.patch` (needed regardless) and run the
   plan/probe/repertoire construction at the resumed root instead of only at the
   duel start (C++, `solver-patched/`).
2. Rebuild the position as a fresh duel via ygorl's puzzle mode
   (`ygorl.engine.puzzle`, cards placed in zones) so the guidance runs from a
   "start". Loses per-duel state (once-per-duel flags, counters); list what is lost.
3. `--rounds` + larger budget at the resumed root.

If none passes: stop, write the measurements into ISSUES.md #4, report. No stand-in.

## Gate B — a lethal check

The solver's goals are boards, not life points. Two options, cheapest first:

1. Solver builds a turn-3 board (targets = attackers / removal); then a Python
   battle-phase search over ygorl snapshot/restore enumerates attack orders and
   reads the enemy's LP. Battle phase is small enough to brute-force.
2. A lethal goal in the C++ solver (opponent LP <= 0).

Pass = correct on hand-built puzzles: a known lethal, a known one-short, and a
lethal that needs the board cleared first.

## The game (once both gates pass)

- **T1:** each of your hands' top 3 boards (Stage 8).
- **T2:** the enemy, with one of 3 fixed enemy hands (shared across all your hands),
  searches for its best turn. Your board responds; your use of each interruption is
  chosen by trying its legal windows and keeping the one worst for the enemy.
  If the enemy OTKs you, that run is a loss.
- **T3:** you draw (the draw is fixed per run and SHARED by all 3 boards of a hand,
  so boards differ only by the board), search MP1, then the battle-phase check.
  The enemy's held hand traps and board responses use the same worst-for-you rule.
- **10 runs per (board, enemy hand).** Win rate per hand = wins / (3 boards' best ×
  3 enemy hands × 10). Dud = 0 wins. **Near-dud** = 1-2 wins of 30 (under 10%):
  listed beside the duds, since in practice it is as bad as one.
- **Impact score becomes real:** win rate of games where the card was summoned vs
  games where the same hand reached a board without it.
- **Report:** win rate, damage dealt when not lethal (tiebreak: 7500 of 8000 beats
  a brick), losses to a turn-2 OTK, duds listed with their hands.

## Cost estimate (measure before believing)

~1 min per side per turn at the 60 s budget → ~2.5 min per game. 4 hands × 3 boards
× 3 enemy hands × 10 runs = 360 games ≈ 2.5 h on 6 workers, per enemy deck.

## Verify

- Gate A and B numbers recorded in ISSUES.md #4.
- A hand known to OTK through an empty board wins 10/10 against a passive enemy.
- Paired draws: the turn-3 draw codes are identical across a hand's 3 boards.
