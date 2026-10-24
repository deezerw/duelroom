# ISSUES

Found by a comprehensive pass on 2026-10-24. Ordered by **impact on getting a deck
optimised for a tournament**, not by how interesting they are.

Every guard currently passes — 171 tests, 7 invariants, field audit clean, doc drift
clean, 31/31 mutations killed. So nothing here is a test failure. These are gaps
between what the program reports and what it can actually support.

---

## P0 — blocks deck optimisation outright

### 1. The optimiser's ranking can be empty and still prints an order

Measured on the stored kewl-tune run (`results/optimize_kewl-tune.jsonl`, 10 trials,
12 hands): `brick_rate` has **one** distinct value across every trial (0.1667), so
does `survival` (0.0) and so does `solved` (10). Three of the four objective terms
were constant, so the entire score spread came from the combo-length tiebreak at
weight **-0.05** — a term never meant to decide anything.

`optimizer.degenerate_terms` exists precisely to catch this and the limitation is in
CLAUDE.md, but a run in that state still emits a ranked table. For a tournament list
that is worse than no answer, because it looks like one.

- **Fix:** raise `hands` until the heavy terms vary, and refuse to print an ordering
  when `degenerate_terms` says fewer than two terms moved (same shape as
  `gauntlet`'s `beats_floor` refusal, which works).
- Not a deck property: `hand_trap_survival` is 0.1667 on snake_eye and on
  blue_eyes_oct_2026, so the metric is live — kewl-tune really did score 0.0 twelve
  times, which is a sample-size problem.

### 2. The optimiser scores a consistency proxy, not winrate

`Objective` weights brick rate (-2.0), solve rate (+1.0), handtrap survival (+1.5)
and combo length (-0.05). None of those is a win. The program measures how reliably
a deck reaches its own board and how well it plays through handtraps; it does not
measure games won. That is the honest ceiling of the solitaire design (CLAUDE.md,
"Why No Duel AI"), and it should be stated wherever a "best list" is printed rather
than implied away.

### 3. The optimiser's inputs come from the single-draw solver

`optimizer.evaluate` → `gate.run_funnel` → funnel_eval, which takes **one** draw per
hand. The solver is a randomized search (CLAUDE.md), so a single draw reports bricks
that a re-draw solves — meaning `brick_rate`, the second-heaviest term at -2.0, is
measured by the noisiest available method. `skills/sweep.py` already does multi-draw
properly for the board library.

- **MEASURED 2026-10-24, same deck, same 20 hands:**
  - multi-draw sweep: **20/20 solved, 0 bricks**, mean 4.1 interruptions
  - single-draw funnel (what the optimiser scores on): **brick 5/20 = 0.25**
    [0.1119, 0.4687]

  So the optimiser's second-heaviest term is wrong by 25 points, in the pessimistic
  direction, on the user's own deck. Those five "bricks" are false bricks — one draw
  of a randomized search. Base score came out -0.193, dominated by an artifact.
- Its heaviest term is dead too: `survival 0.0` on this deck, as on kewl-tune. So
  brick rate is wrong and survival cannot discriminate, which leaves combo length.
- The same run logged `solver 857.09 s (budget 120 s: OVER)` — the budget guard is
  being exceeded by 7x and only warns.
- **Fix:** route `evaluate` through the sweep, or at minimum re-draw the hands it
  calls bricks before believing them.

---

## P1 — a whole feature reports numbers that mean nothing

### 4. `gauntlet` board ranking: the floor is not a floor

A board holding **one vanilla Alexandrite Dragon** "stopped" 18/18 meta going-second
hands (they searched on 6/6, so this is not the repair-mode no-op). A single vanilla
monster cannot stop a meta deck, so the metric is not measuring board strength, and
the four real boards that each stopped 18/18 measured nothing.

`rank` reports `beats_floor` and refuses to print an ordering, so no false answer
escapes — but the feature is unusable until this is found.

- **Retracted theory:** `capture_target()` at a hardcoded `r.turns == 2` is NOT the
  cause. Any `--target` sets `target_scratch`, which builds the goal from scratch and
  discards the captured reference (`main.cpp` ~3229, ~7123), so the goal is just
  "their boss in a monster zone" and is reachable.
- **Next step, now cheap:** `_attack` keeps a `solver.log` per search and returns its
  `target board POSTED/EDITED` line. Run ONE going-second search and read what the
  solver says about its goal, its starting position, and why it wrote nothing.
- **Candidates left:** `--turns 2` not giving player 1 a full turn; the start
  replay's player-1 deck not lining up with `--deck`.

---

## P2 — the data everything else reads is understated or unrepresentative

### 5. The board library was built single-draw and pair-capped

`results/boards.jsonl` (280 records) predates both the climb and the multi-draw
sweep, so every stored board is a floor. `targets`, `threats`, `matrix`, `second` and
the optimiser's payoff target all read it. One elfnote hand: library says 4
interruptions, 32 draws at a quarter of the budget found 10 distinct boards topping
out at 5 int / 4 neg.

- **Fix:** re-run with `skills/sweep.py` and promote.

### 6. `export` keeps whichever list was scraped first

`daemon.py cmd_export` selects with `ORDER BY d.id LIMIT 1` — not best-placing, not
most representative. That is how `branded.ydk` became a Branded/Blitzclique hybrid
with one Fallen of Albaz and **no Branded Fusion**, averaging 1.0 interruptions over
40 hands. It is one of the three opponents `gauntlet` weights by default, and a
gauntlet of weak lists flatters every board.

### 7. `draws` has no per-deck calibration

Four draws bought +0.10 mean interruptions on despia for 2.6x the solver time (one
hand of ten improved); the same machinery beat the stored library outright on an
elfnote hand. Two decks, opposite answers, so `sweep.DRAWS` cannot have one right
value. The budget already has `ladder`; draws needs the same.

### 8. Coverage gaps the reports do not refuse over

- `locals.toml` covers **58%** of the stated meta; no deck exists for azamina, doomz,
  invoked, mitsurugi or rogue. Every weighted ranking inherits that.
- The handtrap matrix is **partial**: 7 of ~10 decks. `doctor` says so; the matrix
  output does not.
- The active deck is `blue-eyes-branded-test`, an untracked file with "test" in its
  name. If that is the tournament list it should be tracked and named as such.

---

## P2b — found on the 2026-10-24 02:00 pass

### 12. Every solver draw leaked a scratch directory — FIXED

`solve_hand` removes the `job` subdirectory it is handed, never the mkdtemp ROOT the
caller made, so `sweep._solve_one` and `gauntlet._one_draw` each leaked one directory
per solve. 914 of them under /tmp over two days, 126 MB, growing by one per
(hand, goal, draw). Fixed in af67334 with a `finally` in both paths plus a test; 856
stale dirs cleared (126M -> 16M).

### 13. Long background jobs do not survive a turn boundary

The 20-hand sweep launched with `nohup … &` at the end of the previous pass had
written **zero** records and its parent process was 90 seconds old at the next pass —
i.e. it died and something restarted it. For a loop that is supposed to run hour-long
measurements across iterations, that silently loses all of them.

- **Workaround in use:** launch with `setsid` and a closed stdin
  (`nohup setsid … < /dev/null &`), then verify the process age on the next pass
  rather than assuming it survived.
- **Watch for:** a job that looks like it is progressing may be a restart from zero.
  Check `ps -o etime` against the elapsed time in its own log.

### 14. The going-second hand was never dealt — FIXED (6714c1e)

`--hand` does not reach player 1. The solver's "starting position (player 1)" block
listed the last five entries of `kewl-tune.ydk` reversed — `shuffle_decks=False` plus
`DUEL_PSEUDO_SHUFFLE` means nothing is shuffled and the hand is drawn off the END of
the decklist. Two different template seeds dealt the same five cards.

So every going-second trial tested ONE hand, and `hands=6` was that hand six times.
For kewl-tune it is four handtraps and a Synchro spell, which cannot assemble its
payoff — hence 0 of 72 searches and a vanilla monster "stopping" the meta. Fixed with
`breaking.deck_with_hand`, wired into `gauntlet._attack` and `breaking.going_second`
(so `second` was affected too). Retracted en route: `--turns 2`; `--turns 3` changes
nothing.

### 15. The optimiser explores one slot, not the flex space

`cmd_optimize` cuts `removable(...)[0]` — a single fixed card — in every trial, so it
answers "what is the best replacement for *this one card*", not "what change most
improves the deck". On the active deck that is 26 candidate adds against 1 fixed cut,
out of a real space of 22 removable slots x 26 candidates = 572 swaps.

`removable` is NOT broken: it orders by the deck's own copy count, so it picks a 1-of
(Blue-Eyes Jet Dragon) rather than a core card. The limitation is the search, not the
choice of cut. It is also stated honestly in the command's own output — but "optimise
my deck" reads as more than it does.

## P3 — correctness and housekeeping

### 9. The optimiser ledger is truncated, so it is not a checkpoint

`hill_climb` calls `ledger.write_text("")` at the start of every run. Within a run it
logs each finished trial, but a restart **erases** the previous attempt's work —
which is the opposite of what a checkpoint is for, and BACKLOG describes it as
resumption insurance.

Also, the ledger stores `brick_rate`, `survival`, `solved`, `hands`, `score` but NOT
`best_line_rate` or `combo_actions.mean`, so `degenerate_terms` cannot be computed
from a stored run — only live, in memory.

### 10. Dead code: `breaking.ScriptedThenYield`

Nothing references it. `convert_line(close_turn=True)` superseded it. It documents a
real trap, so the comment is worth keeping somewhere, but the class is not live code.

### 11. Known blind spots, already documented, still open

- **Handtrap pairs** (Ash + Imperm together) are not expressible: `--fire` takes one
  card per invocation. Locals players hold more than one, so every "stoppable"
  verdict understates.
- **Maxx "C"**: 94% of its windows convert, so lines continue through it, but what it
  *costs* — the cards handed over — is invisible to the metric.
- **`special_summons`** is never populated in `trials`; real counts need each line
  replayed through ygorl.
