# Stage 13 — Side cards: which ones actually help, against what

Goal (user, 2026-11-17): know which side cards to bring and what to take out,
from games played as well as the solver can play them, to use in real matches.
Same rules as Stage 11: engine-played numbers, paired A/B, 95% intervals.
Delete this file when every step is done and verified.

## Why a new harness (the old one is broken)

`optimizer.side_plan` / `propose_side` exist but are deliberately unreachable:
they score through `breaking.going_second`, which is UNSUPPORTED (CLAUDE.md
Known limitations; 0 of 4 against one vanilla Alexandrite Dragon). The games
harness (`game.py`, Stage 9) is the trustworthy one: real turn-1 boards, the
smart defender, turn 2 / turn 3. Side testing is built on it, and the old
side plan is deleted once this works.

`impact.py` already says how games went when a card WAS used vs was not, but
that is observational (good hands use more cards). A side decision needs the
causal question: same hands, same enemy hands, card X instead of card Y.

## Design: a paired swap

- **A side config** = the main deck with swaps (in X, out Y), e.g. +2 Evenly
  Matched -2 Ash going second.
- **Same hands, card swapped in place.** Each trained hand that holds Y gets X
  in Y's slot; hands without Y are unchanged and their games are reused, not
  replayed (they are identical by construction). Same enemy hands, same seeds.
  So the comparison is exact for the hands it touches, and the cost is only
  the touched hands.
- **Changed hands need new lines**: the trainer re-solves only those (X may
  open a line, or just be a hand trap the defender fires: `game.respond_codes`
  reads it from the deck already).
- **Both sides of the coin**, going first and going second separately: a
  board breaker can be gold going second and a dead card going first.
- **The result per config**: change in win rate (and killed on turn 2 going
  first) vs the main deck, paired by pairing, with a crossed-bootstrap 95%
  interval on the DIFFERENCE, plus how often X was drawn and how often it was
  used. Verdict: helps / hurts / can't tell yet (interval crosses 0).

## Steps

1. **Harness + gate.** *Harness built 2026-11-17* (`skills/side.py`, 7fb8a59;
   test pins the in-place swap and the paired diff). Gate running on the server:
   `tools/experiments/side_gate.py` (blank -3 Ash +3 Alexandrite going first;
   Lightning Storm vs the blank going second, 10 enemy hands x 2 runs). `side.py`: build the swapped hands, play only the
   touched pairings, store results keyed by config (like device). Gate before
   anyone reads a number: (a) a blank card (vanilla) swapped for a real hand
   trap must measure as no better or worse; (b) a known strong breaker going
   second (Evenly Matched / Lightning Storm vs a built Angelechy board) must
   measure as a clear gain. If either fails, the harness is wrong: stop.
2. **Candidates.** Side cards from the meta lists of the enemy archetype
   (`deck_cards.section='side'`), the user's own side, and the common hand
   traps / breakers. "Out" candidates: the lowest-impact cards from
   `impact.py` for that matchup, and dead-going-first / dead-going-second
   cards from the games' "used" data.
3. **Screen, then confirm.** A cheap pass (fewer enemy hands) on every
   in/out pair drops the clear losers; the survivors get the full run on
   duelpc away days. Report only confirmed results as advice.
4. **Their side too.** Games 2 and 3: the enemy sides in against you. Run the
   enemy's side configs from its meta list against your main deck, so you see
   which of their side cards hurt you most (what to play around).
5. **`/side` page**: per matchup, going first / going second: the configs
   that help with intervals, what to take out, how often the card is drawn
   and used, and Watch links for games where it made the difference.

**2026-11-18, gate (a) first read: FAILED, and the failure was the games, not the swap.**
-3 Ash +3 Alexandrite read +10.0% wins (95% +1.9 to +19.8). Untouched hands 37% vs 38%
(same turn-1 pieces on 168/170); the 7 Ash hands 1% vs 34%. Cause: turn 3 going first was
steered by `goal[0]`, and "14558127@hand" posts an empty target (fixed, `game.steer`, see
CLAUDE.md "Going second"). Bad games deleted; `side_gate.py` now also re-plays the main
deck's runs 0..1. Re-run it after the current process ends, then read (a) again.

**2026-11-18, second read (after the steer fix): (a) PASSES, (b) is underpowered.**
(a) blank vs main going first: -1.2% (95% -8.5 to +5.4), as a blank should. Lightning
Storm read -2.1% vs the blank, but it was the wrong "must help" control: it resolved in all
15 games it was drawn and cleared their backrow, yet Angelechy stops the break from the
hand (Dominus Impulse, Ghost Ogre). (b) is now -3 Engraver +3 Alexandrite going second,
which must read "hurts": -7.3% (95% -15.4 to +1.0), 46% -> 39% on 240 pairings. The right
direction, the interval just touches zero. Next: the main deck is being re-trained (job
448, Trivikarma fix), so its games go stale; once they are re-played, run (b) again with 4
runs instead of 2 (about 0.7x the interval width, if run noise dominates) before
calling the gate.

**2026-11-19:** the gate is re-run under the honest settings (16x kill check, 8x break, trained
enemy turns) in duelpc's day queue right after the all-hands going-first baseline
(tools/duelpc/day.sh -> results/experiments/side_gate_honest.log). Read (a) and (b) before any
side card. First candidates once it passes, going first (where the deck loses: 84% killed on
turn 2): +1 Nibiru -1 Trivikarma, +1 Ghost Ogre -1 Trivikarma (both already in the side deck;
Trivikarma is dead without Visas).
