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

- **RETRACTED (2026-10-24). I claimed the brick rate was "25 points wrong" on the
  active deck, comparing a sweep that solved 20/20 against a funnel reporting
  5/20. That comparison was invalid and the claim is withdrawn.** The two runs
  shared **neither their hands nor their goals**:
  - 0 of 20 hand seeds overlap, and 0 of 20 hands match by card multiset — the two
    tools seed their deals differently, so they dealt 20 entirely different hands.
  - The sweep solves toward each hand's own planned ladder goals; the funnel solves
    toward one fixed payoff card (`59822133@atk`, Blue-Eyes Spirit Dragon). Only 14
    of the 20 sweep goals included that card at all.

  So "20/20 solved" and "5/20 bricked" were never measuring the same thing. This is
  the failure mode this project documents repeatedly, committed by me: reporting the
  difference between two numbers that mean different things.
- **CLOSED with positive evidence (2026-10-24).** `paired_check` re-solved all 20 of
  the funnel's own hands, 3 seeds each, against the funnel's own target:

  | | |
  |---|---|
  | agree solved | **15** |
  | agree brick | **5** |
  | funnel pessimistic (brick, re-draw reached it) | **0** |
  | funnel optimistic (solved, no re-draw reached it) | **0** |

  **20 of 20 agreement.** The single-draw brick rate is reliable for this deck and
  target — 0.25 is a real deck property. `confirm_bricks` stays as a guard that
  currently finds nothing, which is the right state for a guard.
- The safeguard is still worth having: it costs nothing when there are no false
  bricks, and the solver's randomness is measured and real elsewhere. But it is now
  a guard with a negative first result, not a fix for a known 25-point error.
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
**Status after three fixes (2026-10-24): STILL BLOCKED.** Two real bugs were found
and fixed on the way, neither of them the cause. What is now PROVEN:

1. `--hand` does not reach player 1. The report's "starting position (player 1)"
   listed cards nobody passed, and two template seeds dealt the same five.
2. The hand is not the tail of the file passed as `--deck` either: with `--deck`
   pointing at a rewritten copy, the solver dealt the last five of the ORIGINAL
   light-and-darkness-ritual.ydk.
3. Putting the dealt list into the START replay DOES change the hand — so that path
   reaches player 1 — but not to the hand that was dealt.
4. The start replay's side-b main deck is the **same multiset in a different order**
   than the .ydk written for it (verified by reading the .yrpX back). And the hand
   dealt is not that replay's tail either.
5. `--turns` is not involved: `--turns 3` changes nothing, 100% of rollouts still cut.

So the order is lost somewhere between `template_vs` writing the template and
`Replay.from_yrp` reading the solver's output replay, and the actual source of player
1's opening hand is still unidentified.

- **Next experiment, precise:** read the template `.yrpX` directly, BEFORE the solver
  runs, and compare its side-b main order against the dealt .ydk. That isolates
  whether `template_vs`/`to_yrpx` reorders, or the solver's output replay does.
  Then find what the core actually draws from.
- **Do not spend more loop iterations on this before the P0 optimiser items.** It is
  one feature; the optimiser is the thing being asked for.

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

### 15. The optimiser explored one cut slot — FIXED (configurable)

The effect was real and my first description of the mechanism was wrong. I wrote that
`cmd_optimize` cuts `removable(...)[0]`; in fact `hill_climb` loops over every
removable slot and then ends the inner loop with an unconditional

    break  # one removal candidate per round keeps this finite

so only the first slot was ever reached. Confirmed on the live run: 7 trials, 7
distinct adds, **one** distinct cut (Blue-Eyes Jet Dragon). The search was 26 of a
22 x 26 = 572 space, by design rather than by accident.

`removable` is NOT broken: it orders by the deck's own copy count, so it picks a 1-of
rather than a core card.

- Now `slots` (CLI: 5th argument), default **1** so the cost is unchanged and
  widening is opt-in — every extra slot multiplies trials, and each trial is `hands`
  full funnel runs. `optimize <deck> <arch> 20 1 3` is 78 trials, most of a day.
- `optimize` now prints how many of the possible swaps it will actually try.
- Fixed alongside: the trial name omitted the cut, so two trials adding the same card
  over different cuts shared a scratch path — `evaluate` overwrote one with the other
  and `rescore` only ever saw the last. Harmless at slots=1, immediate at slots>1.

### 16. Orphaned pool workers survive for days

Found 2026-10-24: six `multiprocessing.spawn` workers from a duelroom run, parent
PID 1, **age 6 days 01:31**, 41 MB resident between them. When a pool's parent dies
(a killed sweep, a crashed optimiser) its spawned children are reparented to init
and never exit — they sit blocked on a queue that will never be written to.

Harmless to correctness but it accumulates, and six stray Python processes is enough
to make a `doctor` CPU reading or a `_busy_solvers()` check misleading. Cleared by
PID.

- **PARTLY FIXED:** `doctor` now reports them, with the PIDs and a kill line, and
  fails its check when any exist. It proved the point immediately — run right after
  the detector was written it found **six more**, aged 4h20m, children of the one
  `funnel_eval` parent killed earlier in the session. So this recurs every time a
  long job is stopped, not just once.
- **Still open:** the pools themselves should terminate their children on exit (a
  signal handler in the sweep, or a hard timeout), so the leak stops happening rather
  than being reported after the fact.

### 17. `pkill -f` on a job pattern kills the caller — again

CLAUDE.md documents this as the exit-144 footgun for the gunicorn pattern. It applies
to ANY `-f` pattern typed into a shell whose own command line contains it: `pkill -f
"daemon.py optimize"` exited 144 having killed the invoking shell, and the target's
solver children were orphaned rather than stopped.

- **Rule:** never `pkill -f` from the loop. Kill by explicit PID, or by exact
  executable name (`pkill -x combosolver`), which cannot match a bash command line.
  Killing the parent does NOT stop its solver children; they need killing separately.

### 18. A structural test passed while the code was broken — FIXED (6de8e84)

`sweep.py` was missing `import shutil`, so every call to `_solve_one` died on
`shutil.rmtree` in its `finally` — breaking every sweep and the optimiser's brick
re-check, which routes through it. Cause: the edit adding the import was anchored on
`import json\nimport os`, and `import os` had already been removed when `_be_nice`
moved to `engine.py`, so the anchor never matched.

The test written for that very commit passed the whole time, because it only grepped
the source for `"rmtree(scratch"`. **A structural assertion cannot catch a missing
import**, and `_solve_one` is stubbed out of every other test, so nothing in the suite
ever called it.

- **Lesson for the remaining structural tests in this file** (there are several —
  `test_the_spare_reaches_the_child_process`, `test_breadth_does_not_ask_for_stacks`,
  the ledger tests): each one proves the source *says* something, not that it *works*.
  Where the path is cheap to exercise with a stub, it should be.
- Fixed by asserting the names resolve in the module AND calling `_solve_one` for real
  with the solver stubbed. Verified by deleting the import again: both tests fail,
  then pass when restored.

### 19. Nothing can compare the sweep and the funnel on the same hands

The two measurement paths seed their deals independently — `sweep` via
`hand_seed(seed, name, i)`, funnel_eval via its own scheme — so there is no way to
ask "does the multi-draw path solve the hand the single-draw path called a brick?"
except by hand-plumbing seeds. That absence is what let me compare 20 unrelated hands
and report the difference as a measurement error (see the retraction under P0 #3).

- **FIXED** by `optimizer.paired_check`: re-solves every hand of a funnel record with
  the multi-draw path, using the funnel's own hands and its own target, and classifies
  disagreement in BOTH directions — including `funnel_optimistic`, where the single
  draw found a line the re-draws missed. That direction bounds how much of a solve
  rate is luck rather than reach, and I would not have thought to look for it before
  getting the comparison wrong.

### 20. `survival 0%` reads as a broken metric and is not one — FIXED (output)

Chased this as a suspected bug and it is correct behaviour, worth recording because
the next reader will chase it too. `fire_survives` counts a hand as surviving only
when the solver recovered from **every** window the handtrap had, because the
opponent chooses when to drop it. So `hand_trap_survival` is 0.0 whenever any window
is unrecoverable — which on a long combo is almost guaranteed.

**Measured on the active deck (20 hands):**

| handtrap | windows per line (median / max) | recovered | all windows | window recovery |
|---|---|---|---|---|
| Infinite Impermanence | 11 / 17 | median 3 | **0 of 15** | 32.6% |
| Ash Blossom | 3 / 4 | median 1 | **0 of 15** | 31.0% |

This is the most tournament-relevant number found so far, and it is NOT a bug: the
deck is consistent (15/20 reach Blue-Eyes Spirit Dragon, mean 4.1 interruptions) but
its lines hand Imperm a median of eleven chances and only a third are survivable.
Since the opponent picks the window, they pick one you cannot recover from.

Mitigated by making the output say so: `optimize` now prints the all-windows standard
and the per-window recovery share alongside, and `optimizer.report` carries
`window_recovery` so 0% is never shown bare. The underlying exposure is a deckbuilding
question, not a code fix — shorter lines create fewer windows.

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
