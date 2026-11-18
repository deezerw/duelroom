# Stage 15 — Turn 1 that adapts to disruption (user chose this 2026-11-18)

Why (240 stored going-first turn 1s, imported vs Angelechy-Meta): the games' turn 1
reached the trainer's full goal in 14 (6%) -- the enemy's hand traps break the rest --
and then kept the line with the most GOAL OVERLAP, which knows nothing else: boards
average 1.29 live interruptions, and 31% ended with a spell/trap still in hand that was
free value (Perlereino 18 not activated, Tearlaments Scream 15 not placed, Sulliek 6 not
set; Super Polymerization 31 is correctly held). Game h16/e1009 run 8 shows all of it:
Scream kept, Lacrima's mill skipped, S:P's optional banish spent on our own Engraver,
Reinoheart left bare instead of Kaleido-Heart.

The search is the attacker's own, so the fix is in what it is ASKED for and how its
answer is PICKED, not in the search itself.

## Steps

1. **Measure the pick** (tools/ml/board_data.py + board_model.py, Stage 11's data): per
   (hand, enemy hand), turn 1 asked for several goals -- full, interruptions only, the
   goal minus each card, and (new) the goal + the hand's FREE cards (Field/Continuous
   Spells face up, Traps set: `board_data.free_targets`) -- each board through the real
   kill check at 4x OTK_MS. board_model then scores pick rules on hands held out:
   `full` (today) vs `pieces` (most live interruptions) vs the oracle.
2. **If `pieces` (or full+free) beats `full` with an interval clear of zero**: game.turn1
   tries the variants when the full goal is not reached and keeps the board with the
   most live pieces. Ask the user before it becomes the games' turn 1 (every number
   rests on it), then re-play.
3. **Self-harm**: optional effects whose only legal targets are our own cards (S:P's
   banish on an empty enemy field). Count how often lines do it first; fix in the solver
   only if it is common.
   *Counted 2026-11-18:* S:P's effect was used in 26 of the 240 stored turn 1s and banished
   one of OUR cards in 16 (7%; e.g. h0/e1001, h14/e1005, h16/e1009). Real but second to the
   pick; fix after step 2.
   **Done 2026-11-18 (user asked): patch 0007 `--no-self-target`**, `game.NO_SELF_TARGET`
   on our turn 1 and lethal turns. S:P's summon banish must take an opponent's card (none ->
   not activated); its quick effect (until the End Phase) stays legal. Stored games keep the
   old behaviour until re-played.
4. Then the enemy's real goals (train its 10 hands), and re-play everything.
