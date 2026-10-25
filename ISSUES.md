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
**Status after FIVE fixes (2026-10-24): BLOCKED, and the cause is now localised to
the solver's going-second mode itself.** Four real bugs were found and fixed on the
way and none of them was the cause. Stopping here deliberately.

The decisive measurement, which took five iterations to set up correctly:

| kewl-tune reaching `15665977@atk` | |
|---|---|
| going FIRST (stored optimiser ledger, same target) | **10 of 12 — 83%** |
| going SECOND into one vanilla Alexandrite Dragon, varied functional hands | **0 of 4** |

A board holding a single vanilla monster cannot turn 83% into 0%. Every input is now
verified correct, so what remains is the mode.

What was PROVEN and fixed along the way (each real, none the cause):

1. `--hand` does not reach player 1 — the report listed cards nobody passed, and two
   template seeds dealt the same five.
2. The hand is not the tail of the `--deck` file either.
3. The dealt list must go into the START replay, not `--deck` — that path does reach
   player 1 (fixed; it changed the hand).
4. `template_vs` hardcoded `shuffle_decks=False`, so player 1's hand was simply its
   decklist tail, identical for every seed — for kewl-tune, four handtraps and a
   Synchro spell that cannot assemble its payoff at any budget. Fixed: `shuffle=True`
   for the opposing seat, and `defend`/`floor` vary the seed per hand. **Verified in
   the run: four different functional hands, including Kewl Tune Synchro, Cue and
   Rotary.** Still 0 of 4 through.
5. `--turns` is not involved: `--turns 3` changes nothing, 100% of rollouts still cut.
6. The template preserves decklist order exactly — it is the solver's OUTPUT replay
   that reorders, and its responses are only valid against that order, so the order
   cannot be substituted back.

**What this is NOT:** hands, decks, decklist order, turn limits, target posting
(`target_scratch` builds the goal from `--target` and discards the captured
reference), or the repair-mode no-op (`searched` is true throughout).

**Localised to the vendored C++ (2026-10-24), and deliberately NOT changed.**
`turn_cuts` does not mean "budget exhausted" — the solver's own comment says it
separates "decision budget too short" from **"the line overflows turn 1"**. Three
sites in `solver-patched/search.cpp` cut on a **hardcoded** `turns >= 2`:

| line | comment |
|---|---|
| 2751 | "Past the end of turn 1 the board is frozen: continuing the rollout into the opponent's turn can no longer reach anything." |
| 3269 | same shape |
| 4726 | `if(turns >= 2) { ++stats.turn_cuts; return Adv::Dead; }` |

Every *other* cut site uses the configurable `total_turns >= cfg.max_turns + 1`.
So the search carries a baked-in assumption that it is a **first player's turn-1
combo**, which is exactly what a going-second search is not — and 100% of rollouts
dying by `turn_cut` is consistent with it.

**PROVEN by experiment, and it is a real bug — but it is not the whole blocker.**

Built a patched solver ALONGSIDE the pinned one (separate object, separate binary,
source restored, `bin/combosolver` and `obj/solver/search.o` verified byte-identical
by md5 afterwards), with those four cuts respecting `cfg.max_turns`. The cut profile
changes completely:

| solver | turn cuts |
|---|---|
| pinned | `turn 40334` — **100% of rollouts** |
| patched | `turn 16` — **0% of rollouts** |

So the hardcoded cuts WERE killing every going-second rollout, and respecting
`cfg.max_turns` fixes that. The patch is saved, reviewed and **not applied**, at
`patches/0001-going-second-turn-cuts.patch`.

**A second, independent blocker remains.** With the search now running freely it
still gets through 0 of 4, and the solver's own report says why it cannot:

    NRPA : 38871 rollouts  165189 states  38332 adaptations   best 0/1
    hindsight: 0 substitute goal(s) kept  <-- NONE: no extra-deck monster summoned

`best 0/1` means the goal card was never once placed across 38,871 rollouts, and no
extra-deck monster was summoned at all — in a Synchro deck. Also logged:
`!! REQUESTED but INERT here (dependency absent in this mode): reenter 0.50`, so at
least one mechanism is disabled in this mode. That is the next thread, and it is a
separate investigation from the turn cuts.

**Why the patch is not applied:** the solver is pinned (`combo_solver e0c7221802a2`),
CLAUDE.md says "treat an update as a migration; otherwise results silently change
meaning", and promoting it re-gates every measurement in this session. It also would
not make the feature work, because of the second blocker above. The feature stays
UNSUPPORTED and the patch waits for a session where the re-gate can be watched.

- **For a session with someone watching:** patch those three sites to respect
  `cfg.max_turns` (or the rollout's starting turn), rebuild via
  `tools/build_combo_solver.sh`, re-run `daemon.py gate snake_eye`, and re-measure.
  `_attack` keeps a `solver.log` per search as the evidence.
- **Until then treat `gauntlet`'s board ranking and `second` as UNSUPPORTED.** `rank`
  refuses to print an ordering when nothing beats the floor, so no false answer
  escapes — believe that refusal.

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
with one Fallen of Albaz and **no Branded Fusion**, averaging 1.6 interruptions over
its 31 solved hands of 40 (1.2 if the 9 with no line count as zero). It is one of
the three opponents `gauntlet` weights by default, and a gauntlet of weak lists
flatters every board.

- **My own measurement error, corrected 2026-10-24.** I reported this deck at
  "1.0 interruptions over 40 hands" all session, in ISSUES, CLAUDE.md and several
  commit messages. That figure came from my iteration-1 script scoring unsolved
  hands as **-1 interruptions**: (49 - 9)/40 = 1.00 exactly. Counting a brick as
  minus one interruption is meaningless. The real figures are 1.6 over solved
  hands and 1.2 counting bricks as zero. The qualitative point stands on the
  decklist itself — no Branded Fusion — not on the number I attached to it.
- Found because `boards` printed 1.6 for the same deck. Two views disagreeing is
  what surfaced it, which is the fifth time that has worked tonight. `boards` now
  states its denominator: "per SOLVED hand; hands with no line are not in this
  library" — a bare mean invited reading it as per-hand.
### 7. `draws` had no per-deck calibration — FIXED

Four draws bought +0.10 mean interruptions on despia for 2.6x the solver time (one
hand of ten improved); the same machinery beat the stored library outright on an
elfnote hand. Two decks, opposite answers, so `sweep.DRAWS` cannot have one right
value. The budget had `ladder`; draws had nothing.

`sweep.draws_curve` and the `draws [deck]` command now report it, **at no solver
cost** — every record stores each attempt with its draw index, so the running best
over the order the solves actually ran is exact after the fact.

First real answer, on the active deck (40 hands, 540 solves, mean best 3.83):

    hands already at their best board by end of draw
       draw  0   24/40      draw  6   31/40
       draw  2   27/40      draw  8   35/40
       draw  5   28/40      draw 10   40/40
    solves after the best was already found: 285 (53%)

So 60% of hands peak in the breadth round and the rest keep improving all the way to
draw 10 — **this deck earns its draw budget**, unlike despia where almost nothing
moved. That is the per-deck answer the single `DRAWS` constant could not express, and
the command says which case it is looking at.

### 8. Coverage gaps the reports do not refuse over

- `locals.toml` covers **58%** of the stated meta; no deck exists for azamina, doomz,
  invoked, mitsurugi or rogue. Every weighted ranking inherits that.
- The handtrap matrix is **partial**: 7 of ~10 decks. `doctor` said so; the matrix
  output did not, because its own threshold was `_n < 5` and 7 clears it. **FIXED:**
  `matrix` now compares against the decks that actually exist — the same standard
  `doctor` uses — and prints the share ("PARTIAL: 7 of ~10 decks, 70% of the field")
  plus the note that every ranking shown is a ranking over those 7. Two views judging
  the same file by different standards is how a caveat goes missing from the one that
  drives a decision, and this view decides which handtraps to main deck.
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
- **NOW FIXED at the source:** `sweep._children_die_with_us` installs SIGTERM and
  SIGINT handlers around every pool wave, terminates the workers, then chains to
  whatever handler was already there so a `kill` still kills. SIGKILL cannot be
  caught by anything in Python, so `kill -9` will still orphan workers — which is
  why the `doctor` check stays as the backstop rather than being replaced.

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

**It recurred.** The same mistake — an edit anchored on text that had already
changed — silently dropped `import contextlib` an hour later and broke importing
`sweep` at all. Now covered by a test that asserts every name the module uses
resolves (`test_every_name_the_sweep_uses_resolves`), and the habit changed: insert
by line number read from the file, never by a guessed text anchor.

- **FIXED SYSTEMATICALLY:** `tools/check_names.py` runs pyflakes and fails on
  **undefined names only**, wired in as `doctor`'s fifth guard and asserted by the
  test suite. Verified against both real bugs: deleting `import shutil` reports
  `skills/sweep.py:115:9: undefined name 'shutil'`, and deleting `import contextlib`
  reports `skills/sweep.py:154:2: undefined name 'contextlib'`. Unused imports and
  f-string notes are printed but do NOT fail, so the gate stays actionable instead of
  becoming noise that gets ignored — which is how a real finding would hide in it.
- It also surfaced three dead names left by my own refactors, now removed:
  `distinct_solve` / `distinct_brick` in `cmd_optimize` (orphaned by the
  degeneracy-refusal rewrite) and an unused `shutil` import in `optimizer.py`.
- **Partly addressed.** 48 assertions in the suite read source text. Converting them
  all is neither feasible nor wise, so the most load-bearing one was converted:
  `test_the_refusal_actually_suppresses_the_table` runs `cmd_optimize` against a
  degenerate history with stubs and asserts no ranking prints, no "best:" line
  appears and **no .ydk is written**. Verified by deleting the refusal's `return`:
  it fails, and passes when restored.
  - Honest accounting: for that particular break the structural test caught it too.
    The behavioural one earns its place on the assertions source-matching cannot
    make — that no file is written and no best line is announced.
  - The general weakness stands for the rest (`test_the_spare_reaches_the_child_process`
    needs a solver; several ledger and wave tests still only read source). The name
    check covers the specific failure that bit twice.
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

### 21. Solved-only means printed as if they were per-hand — FIXED

Audited after my own "1.0 interruptions" error, to see whether production code shared
the shape of it. The sentinel part did not — no `-1`-for-unsolved anywhere outside my
throwaway script. But the *labelling* did.

`summarize` appends to `row["interruptions"]` only after
`if r["status"] != "solved": continue`, so `mean_interruptions` is a mean over solved
hands. Three views printed it bare:

- `solve` (both branches) — "mean interrupts 4.10"
- `sweep`'s summary line — "mean 4.1 interruptions"
- `boards` — fixed in the previous commit

For a deck that bricks a quarter of its hands, "per solved hand" and "per hand" are
materially different claims: branded is 1.6 and 1.2 respectively. All three now name
their denominator, and a test asserts both that the field really is solved-only and
that every view saying it says so.

### 22. The deployed web service is 5 days stale — checked, LOW impact

`web/worker.py` and the gunicorn masters have been up **5 days 7 hours** (since
~2026-10-19 01:00). The first change in this work landed 2026-10-20 and tonight's
~30 commits are from 2026-10-24, so the deployed service runs code older than all of
it. CLAUDE.md is explicit that `skills/` changes used by the worker need a **full
restart**, not a SIGHUP.

So the question is which changed modules the web app actually imports. It imports
`carddb`, `db`, `engine`, `handlab` and `skills.deck`. Of the seven `skills/` files
changed since the worker started, only **two** are on that list:

| module | what changed | effect on the web UI |
|---|---|---|
| `handlab.py` | the board score was clobbering the plan's piece list (231f2b2) | **none on any reported number** — nothing read that field; it is diagnostic only |
| `engine.py` | added `be_nice` | none — it only niced the sweep/gauntlet pools, which web does not use |

`gauntlet`, `sweep`, `optimizer`, `breaking` and `meta_scrape` all changed tonight and
**none of them is imported by the web app**, so none of tonight's measurement fixes —
and none of the defects they fixed — reaches the web UI either way.

**The stale deployment was not serving wrong numbers**, which is why the restart was
held back while the user slept — an internet-facing TLS service, a failed restart
leaves it down with nobody to recover it, and the gain was a field nothing reads.

**RESTARTED 2026-10-24** once explicitly authorised. Worker confirmed idle first
(`jobs.active` was None), then `systemctl restart duelroom.service`: service active,
3 gunicorn procs, worker age reset, and `https://127.0.0.1:8446/login` serves 200. The
deployment now runs the current `skills/`.

### 23. `drill` told you to practise passing — FIXED

Found by running the command, which had never been exercised this session. `drill`
exports your best lines as EDOPro replays to rehearse, and it ranked them on the flat
interruption count — which scores a card HELD IN HAND the same as a negate on the
field. The top line it offered for practice on the active deck:

    5 interruption(s), 12 actions
    opening hand: Effect Veiler, Dominus Purge, Ash Blossom, Mulcharmy Fuwalos,
                  Dominus Purge
    ends on: Dominus Purge (set) x2 | hand: Mulcharmy, Ash, Veiler

An all-handtrap hand with no engine, where the "line" is *set two traps and hold
three*. It outranked two genuine boards — Blue-Eyes Spirit Dragon + Maiden of White,
and Spirit Dragon + Hieratic Seal — at four interruptions each.

Practising "I had no engine so I passed" is not practice, and this is the command
whose entire purpose is telling you which lines to rehearse before a tournament.

Fixed: `lines_from` ranks on `_on_field` first (interruptions the board holds on the
field, set or face-up), then the flat count as the tiebreak. Cards in hand are real
interaction but not a line you can rehearse — you hold them whatever you drew. Now
the two real boards rank first and the do-nothing line drops to third. Verified by
restoring the old sort key: the test fails, and passes again with the fix.

This is the same flat-count flaw that drove the end-board scoring at the start of the
session, surfacing in a second place that nobody had looked at.

### 24. `report` said "no funnel run yet" with a full record on disk — FIXED

Found by running the command. `latest_funnel` globbed `results/*.jsonl` — **top level
only** — while `optimizer.evaluate` writes its funnel records into
`results/optimizer/`. So any deck that had been through `optimize` reported no
measurement at all. The active deck had a complete 20-hand record — 15/20 solved,
brick 0.25, the very one `paired_check` validated — and the view whose job is
assembling what exists said there was nothing.

Fixed with `rglob`. Trial records cannot be picked up by mistake: their
`summary.deck` is the generated trial name, not the deck's.

**That fix broke a different test, which was right to break.** Ranking by recency
alone, `rglob` then preferred the optimiser's base record for kewl-tune — newer, but
it fires only the top two threats (Imperm, Ash) — over the gate run that fired
everything, silently dropping the Maxx "C" blind-spot note the report exists to
carry. A genuine loss of information, not a test artefact.

`latest_funnel` now ranks by **(handtraps fired, mtime)**: more of the question
answered leads, recency breaks ties. Both views are now correct — the active deck
reports its 20 hands, and kewl-tune keeps "Surviving it is not beating it".

Now visible in the report for the active deck, and worth reading before the event:

      PLAYS THROUGH (higher is better — this is YOUR deck)
         0%  Infinite Impermanence    folds to it
         0%  Ash Blossom              folds to it
      -> Infinite Impermanence is your worst matchup card

### 25. `drift` would have reported "the meta is stable" from one scrape — FIXED

Found by checking before running the command. `snapshot` freezes the `inclusion`
table; `inclusion.computed_at` moves every time anything recomputes it, so six-day-old
scraped lists look fresh. Nothing recorded which SCRAPE a snapshot came from, so two
snapshots of the same data differ only by when they were taken — and `drift` between
them reports about zero movement, which reads as "the meta did not move" when it means
"no new lists were gathered".

Already live: `baseline-2026-10-18` and `2026-10-18` are both **5599 rows from the
same scrape, 3.5 hours apart**. The newest scraped list is 2026-10-18; `inclusion` was
recomputed 2026-10-24.

- `scrape_fingerprint` is the newest scraped list plus the list count — what actually
  has to change for drift to mean anything. Stored with each snapshot (added by
  `ALTER TABLE` if the column is absent, so existing databases keep working).
- `same_scrape` compares two snapshots and returns **None** when either predates the
  column, which must not be read as "different".
- `drift` now refuses BEFORE computing anything when both share a scrape, and says
  what to do instead. Verified end to end: two fresh snapshots of one scrape are
  detected and refused; a new list changes the fingerprint; the two pre-existing
  snapshots correctly report "could not be checked" rather than guessing.

`ydke` was exercised in the same pass and is correct — the URI round-trips to an
identical 44/14/15 deck.

### 26. `optimize` announced more than twice the work it does — FIXED

`cmd_optimize` built its display pool with `candidate_pool(...)`, whose own default
limit is 30, while `hill_climb` passes `pool_limit=12`. So it printed "26 candidates
... 78 trials of 572 possible swaps" and then ran **36** (37 records including base).
The 3-slot run completing at 37 trials rather than 79 is what exposed it — and that
count was correct, the announcement was not.

Fixed: one `pool_limit` is computed, announced, and passed to `hill_climb`, so the
stated work is the work. Now reads "12 candidates ... 36 trials of 264 possible
swaps".

### 27. `rescore` rendered a 3-slot run as duplicate rows — FIXED

Trial names became `<deck>_r<round>_<cut>_<add>` when the cut was added to them, but
the label parser read only the last segment. So two trials adding the same card over
**different cuts** rendered identically — "Called by the Grave" twice — which looks
like a bug in the data rather than in the display. Now shows `-cut  +add`, and still
renders the older add-only names from the single-slot run.

### 28. `import` silently repoints every bare command — notice improved

Exercising `import` moved the active deck from the tournament list to the imported
one. That is deliberate and right for the common case, but `solve`, `report`,
`optimize`, `drill` and `gauntlet` all default to the active deck, so importing an
opponent's list or a meta deck for the gauntlet silently moves the target. The
one-line notice was accurate and conveyed none of that.

Behaviour unchanged — it is a reasonable default and changing it is the user's call.
The notice now names what it replaced and gives the exact command to undo it.

### 29. No test checked that the web routes answer — FIXED (and they do)

The web UI is what the user actually opens, and it imports `skills/` that changed
~30 times tonight. `test_web.py` asserted specific copy on a handful of pages and
never checked the rest answer at all, so a route raising 500 would have been
invisible to the suite.

Walked all of them: **15 routes return 200**, the two missing-deck routes correctly
302, no 5xx anywhere. So this is a clean result, not a defect — but the absence of
the check was real, and it is now two tests: every read-only route answers, and a
missing deck redirects rather than erroring or rendering a page about nothing.

### 30. My own drill fix made the display contradict the order — FIXED

Caused by the iteration-23 fix. `lines_from` now ranks on ON-FIELD interruptions
(correct: a card held in hand is not a line you can rehearse), but both the CLI and
the web page still printed the **flat** count, which includes cards in hand. On the
active deck's 80 lines that put a "4 interruptions" entry above a "5" **three times**
— at positions 6, 36 and 69.

To a reader that is a sorting bug, not a measure being wrong. The ranking was right
and the number shown did not explain it.

Both views now lead with the number they rank by: "4 on field, 5 counting cards held
in hand". `drill.export` returns it, so the CLI and the web template cannot drift
apart from the sort key again, and a test asserts all three places carry it.

Found by checking whether the iteration-23 fix had reached the web UI at all. It had
— `lines_from` is shared — which is exactly why the display went out of step with it.

### 31. `drill`'s flat count silently read 0 outside the summary — FIXED

`drill.interruption_counts` reads a companion `*_summary.json` keyed by hand, and
returns **0** for any hand it does not find. That is indistinguishable from "this
board has no interruptions". The function's own docstring records a previous version
of the same bug ("Ranking on the line field silently scored every line 0").

Live example: `results/sweep_active.jsonl` has 40 hands, but its companion summary
covers only hands **0-19** — the second 20 were added by calling `solve_deck`
directly, and only `sweep()` writes a summary. So hands 20-39 report flat count 0
while their records carry the real value (hand 23: 4, hand 27: 5, hand 39: 3).

Masked by the iteration-30 fix: ranking is now on `_on_field`, computed from the board
itself, so the ORDER is unaffected. Only the displayed "counting cards held in hand"
figure is wrong, and only for hands missing from the summary.

- **FIXED:** `lines_from` reads `solver.blocking.interruptions` from the record
  first, falls back to the summary, and finally to `_on_field` — never to a bare 0.
  Verified on the 40-hand file: zero lines report 0 now, and hands 20-39 show their
  real values. A test covers a record with no companion summary at all.

## P3 — correctness and housekeeping

### 9. The optimiser ledger is truncated, so it is not a checkpoint

`hill_climb` calls `ledger.write_text("")` at the start of every run. Within a run it
logs each finished trial, but a restart **erases** the previous attempt's work —
which is the opposite of what a checkpoint is for, and BACKLOG describes it as
resumption insurance.

Also, the ledger stores `brick_rate`, `survival`, `solved`, `hands`, `score` but NOT
`best_line_rate` or `combo_actions.mean`, so `degenerate_terms` cannot be computed
from a stored run — only live, in memory.

### 10. Dead code: `breaking.ScriptedThenYield` — FIXED

Nothing referenced it or `action_kinds_seen`; `convert_line(close_turn=True)`
superseded both. Removed, and the trap they documented is kept as a comment because it
still bites: the solver's own replays stop when the combo stops, so `--start` rejects
them ("END OF THE RECORDING: the line does not get through the turn"), which is why
`build_start` and `inert_start` re-emit with `close_turn=True`. A test asserts the
code is gone and the warning is not.

### 11. Known blind spots, already documented, still open

- **Handtrap pairs** (Ash + Imperm together): the limitation STANDS, now with the
  mechanism pinned and a negative result rather than an assumption.
  - `--fire` is singular — the solver's help does not mark it "Repeatable", unlike
    `--target` and `--summon` — so a both-fired verdict cannot be requested.
  - `--opp-hand` IS repeatable and `SolveRequest.extra_args` emits it with no
    patching (verified: `extra_args=("--opp-hand", "10045474")` appears on the
    command line). So the opponent CAN hold a second handtrap while the first fires.
  - **It changed nothing measurable.** Ash fired on one solved line of the active
    deck, with and without Imperm held: 2 windows and 1 converted, both ways. The
    `--fire` verdict counts windows of the FIRED card only, so a held card creates
    no new windows and any effect goes unreported.
  - **Evidence is weak**: one hand, one line, 2 windows. That rules out a large
    effect, not a small one.
  - The matrix's understatement is real and needs a repeatable `--fire`, not a flag
    combination — so nobody re-runs this hoping the flags are enough.
- **Maxx "C"**: 94% of its windows convert, so lines continue through it, but what it
  *costs* — the cards handed over — is invisible to the metric.
- **`special_summons`** is never populated in `trials`; real counts need each line
  replayed through ygorl.
