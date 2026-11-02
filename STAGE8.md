# Stage 8 — Matchup testing for trained boards

Requested 2026-11-02. The trainer finds each hand's best line; this stage asks how
those boards do against real meta decks, and scores each card the deck summons by
how much it actually stops. Everything here runs on machinery that works today
(`--fire`, the trainer, `gauntlet.rank_boards`). Stage 9 replaces the fire-based
estimate with full games once the turn-2 search works.

Delete this file once every item below is built and verified.

## 1. Keep the top 3 distinct boards per hand (trainer)

The trainer already ranks every line a solve writes; it keeps only the best.
Keep the best **3 distinct boards** per hand (`gauntlet.Candidate.board_key`
identity: same pieces in the same places = same board), so the matchup has
something to rank. Stored beside `trainer_hands`, keyed by deck_hash + hand.
Each must stay watchable on the playmat (`/watch/train/<name>/<i>?board=k`).

## 2. Choose the enemy list properly

`export` writes `ORDER BY d.id LIMIT 1` — the first list stored, not a good one
(`branded.ydk` is a Branded/Blitzclique hybrid). Pick by placement rank
(`meta_scrape.PLACEMENT_RANK`), then recency, then closeness to the archetype's
core (most core cards from the `inclusion` table). Print which list was chosen
and why. The enemy deck needs its own payoff target (`scenarios.ensure_target`).

## 3. Weight across meta decks with `locals.toml`

Run the matchup against every enemy deck with a weight; headline = weighted mean
over the decks actually measured, `coverage` printed beside it
(`rank_boards` already does this — reuse, don't reimplement).

## 4. Maxx "C" cost per board

Replay each kept line through ygorl and count special summons = cards handed over
to Maxx "C" if it resolves. Shown beside every board; not folded into the score.

## 5. Watch the enemy play into your interruption

Fire runs with `--fire-bake` write replays that play from their own file. Store
them and add `/watch/matchup/...` so the playmat shows the enemy's turn with your
card fired — that is how you see WHY a board failed.

## 6. Matchup score can become the trainer's ranking (toggle)

A per-deck preference (`trainer_prefs`): rank bests by `[-avoided, weighted
stop-rate, on_field, interruptions, negates, -actions]` instead of the board-only
key. Off by default; the board-only key stays the learning curve.

## The run itself

- **Plateau = trigger.** After N generations (default 3) with no hand improving,
  the trainer stops training and starts the matchup automatically.
- **Enemy hands are shared.** 3 fixed enemy hand seeds per enemy deck; EVERY one
  of your hands faces the SAME 3 (one enemy hand lets their draw decide the whole
  ranking — a brick flatters every board, a god hand blanks every board).
- **10 runs per pairing** (the solver is randomized). A run "got through" if the
  enemy reaches its payoff target. Report `stopped k/10` per (board, enemy hand).
- **Impact score per card** = share of that card's fired windows where the enemy's
  line failed, pooled over enemy hands and runs, with a Wilson interval and the
  window count (a card with 3 windows shows "3 windows", not a percentage alone).

## Known limit, printed with every result

`--fire` puts the card in the opponent's hand, so only cards **held in hand** are
measured exactly. On-field interrupters (Kaleido-Heart, Dragostapelia, Fiendsmith
negates) get a **stand-in**: a handtrap with the same effect class (monster-effect
negate → Infinite Impermanence, etc.), mapping in one table, results labelled
"approximate". Stage 9 removes the stand-ins.

## Verify

- Top-3 boards: distinct by `board_key`, all three watchable.
- Shared enemy hands: identical enemy hand codes across every one of your hands
  (assert in code, show in the report).
- A known-useless board piece (vanilla monster) scores ~0 impact; Ash Blossom vs
  a deck whose starter it hits scores well above it.
- Kill + resume a matchup run with no repeated (board, enemy hand, run).
