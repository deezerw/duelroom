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

**2026-11-18, step 1 read** (60 positions, 738 distinct boards, kill check at 4x OTK_MS,
held out by hand): lost on turn 2 with the board picked by full 94%, pieces 90%, free 90%,
random 93%, model (GBM) 95%, oracle 70%. pieces - full -4.2% (95% -11.0 to +1.8); on the 18
positions where the board decides, -13.9% (-33.3 to +8.3). No pick clears zero: step 2's
condition is NOT met. The GBM is no better than random. Only 18/60 positions have any board
that survives, so the pick can rescue at most ~24 points even when perfect.

**2026-11-19, step 4 started: the enemy's real goals.** `train Angelechy-Meta --vs imported`
trains the 10 hands the games deal it (trainer.ensure_enemy_pool, all 10 checked against the
stored games). Its first generation only set Dominus traps: the scripts count no Angelechy
piece, so the new card value zone "szone" (a monster face-up as a Continuous Spell) values
its 6 Synchros there 1 each and Endgame Problem 2 (values chosen by Claude, not the user).
24 generations, 8 h on the server, last 6 flat: mean 4.40 interruptions a hand. The games'
enemy turn 1 (one target, Endgame Problem) scored the same way: mean 2.07, and its best of 24
games per hand is below the trained board on all 10 hands (the games' boards face our hand
traps, the trainer's do not, so the gap is an upper bound). NOT wired into game.enemy_turn1
yet (trainer.enemy_goal is the lookup): asking the user, since it changes every game going
second and old games stop being comparable.
**User chose: switch it (2026-11-19).** game.run going second passes each enemy hand's trained
goal (trainer.enemy_goal, refused if the dealt hand differs) to enemy_turn1. The old imported vs
Angelechy-Meta going-second games (240 turn 1s, 2400 games) are relabelled device
`endgame-t1` with their files under `Angelechy-Meta@endgame-t1/` (readable on /games, never
pooled); DB backup data/duelroom.db.bak-20261119. The three side variants still play the old
enemy turn 1 (no enemy pool for their decks yet). Next: re-play going second on duelpc.

**2026-11-19, richer kill labels (user: "start working 1").** board_data records per kill
check (`kill`): first win's clock, wins, states searched, LP taken when not killed, enemy cards
played, ours that fired, our stops, and the line (kept). Boards from 1203 on have it; earlier
ones are lost/not only (their scratch is gone). The model step reads it once there is enough:
the user's PC runs Mon-Wed and short daytime windows only (loud), the server nights.
**2026-11-19:** side variants are dealt the same 10 enemy hands (checked), so
trainer.enemy_goal(hand=...) serves them the goals trained against imported; their old
going-second games are relabelled `endgame-t1` too (backup duelroom.db.bak-20261119b).
Stored games get the rich kill label nightly (cron 05:40, tools/ml/game_kills.py).
**2026-11-19, user chose: break at 8x.** game.BREAK_X = 8: going second, the break searches
8x SOLVE_MS (the plateau's converged level). The 233 games job 502 had played with the trained
enemy turn 1 but the 1x break are relabelled `brk1x` (backup duelroom.db.bak-20261119c); they
compare with `endgame-t1` on the enemy change alone. Re-play: 8 hands x 10 enemy hands x 2 runs.

**2026-11-19, survivor re-check read:** all 63 boards that survived the 4x kill check were
checked again at 16x: 59 were killed (94%). Of 1063 distinct boards, 4 survive a 16x kill
check. With a converged-ish kill check, vs Angelechy-Meta going first nearly every board this
deck makes dies on turn 2 -- the games' going-first numbers (1x kill check: 690 of 1520 lost on
turn 2) are far too kind. Board picking cannot fix that (nothing to pick); the levers are the
deck (side/main) and the kill-check budget in the games (Monday: the 32x plateau).
/games has "What you must stop, by their hand" (stops.by_enemy_hand).
**2026-11-19:** going first, the enemy's non-kill turn 2 builds toward its trained goal
(game.turn2 `targets`), not Endgame Problem alone. game.run refuses to add going-first games
beside ones older than game.FIRST_METHOD_SINCE: relabel the old going-first games (with the
kill-budget change, after Monday's 32x plateau -- use_timing and Monday's queue read their
kill lines in place until then).

**2026-11-19, best time to use, going second (use_timing second, 37 windows, 32 paired,
their trained turn 1, our break at 8x):** Ash on Shatranga lose 33% fired / 67% held (3),
Ash on Fuwalos 25/38% (8, +12 +-24), Ash on Purulia 50/62% (8, +12 +-24); our Fuwalos at an
open window 67/67% (9). Every Ash row favours firing; none is clear of zero yet. More needs
going-second games for the other Ash hands (11, 13, 23: the 8-hand re-play skipped them).
Going first (use_timing, 101 windows at 8x kill): fire = hold everywhere, the kill comes anyway.

**2026-11-19, the honest going-first baseline (8 hands x 10 enemy hands x 2 runs, duelpc,
16x kill check, trained enemy turn 2):** won by turn 3 11/159 = 7%, killed on turn 2 84%. The
old settings on the same hands (kill1x: 1x kill, one-target enemy turn 2) read 43% / 45%.
Going second (server, 8x break, trained enemy turn 1): 66% won on turn 2 (was 31% at 1x).
Against a full-strength Angelechy this deck wins going second and almost never survives going
first -- the side-card search (the user's end goal) should look at going first.

**2026-11-19, why going first is weak (user asked):** in the honest going-first games the enemy
fires 2.2 interruptions on our turn 1 on average (168 turn 1s; 2 untouched), opening ~3 cards
usable on our turn -- 22 of its 43 main cards act on our turn (11 hand traps, 11 Dominus from
hand) plus 3 Opening to e4 (only when WE go first): a going-second list. Our turn 1 then reaches
its full goal 4/80, ends with 0 pieces 22/80 and 1 piece 44/80 (the trainer's untouched boards
average ~4.8), and Mulcharmy draws leave them ~6 cards (up to 13) for turn 2. Levers on our
side: turn 1 that adapts to disruption (this stage), turn-1 budget against the CONVERGED kill
check (`tools/experiments/t1_budget.py`, queued on duelpc), the omniscient attacker (it sees our
hand and set cards), and a second enemy deck to tell "weak going first" from "weak vs Angelechy".
