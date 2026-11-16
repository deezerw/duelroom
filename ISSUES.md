# ISSUES

Found by a comprehensive pass on 2026-10-24. Ordered by **impact on getting a deck
optimised for a tournament**, not by how interesting they are.

Every guard currently passes — 287 tests, 7 invariants, field audit clean, doc
drift clean, undefined-name gate clean, 31/31 mutations killed. So nothing here is
a test failure. These are gaps between what the program reports and what it can
actually support.

**Status of the 61 items, as tagged on each heading:** 52 FIXED · 1 RESOLVED
by measurement, fix not warranted (#3) · 1 RE-RUN IN PROGRESS (#5) · 3 AUDITED CLEAN, no fix needed
(#33, #34, #37, #46) · **3 OPEN** — #2 (the objective measures consistency, not
winrate: inherent to the solitaire design), #4 (`gauntlet` board ranking: both
causes sit in the pinned C++ solver), #11 (the documented blind spots, which are
limits rather than defects).

---

## P0 — blocks deck optimisation outright

### 1. The optimiser's ranking can be empty and still prints an order — FIXED (refusal before any table)

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

### 2. The optimiser scores a consistency proxy, not winrate — OPEN — inherent to the solitaire design

`Objective` weights brick rate (-2.0), solve rate (+1.0), handtrap survival (+1.5)
and combo length (-0.05). None of those is a win. The program measures how reliably
a deck reaches its own board and how well it plays through handtraps; it does not
measure games won. That is the honest ceiling of the solitaire design (CLAUDE.md,
"Why No Duel AI"), and it should be stated wherever a "best list" is printed rather
than implied away.

### 3. The optimiser's inputs come from the single-draw solver — RESOLVED 2026-10-25 — the proposed fix is NOT warranted; the guard stays

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
- **RESOLVED 2026-10-25, against the original proposal.** The rebuilt library
  settles it with a second, independent experiment. Comparing the single-draw and
  four-draw arms over 40 identical hand seeds on each of five decks, **the set of
  unsolved hands is identical in both arms for every deck** — 0 rescued, 0 lost
  across all 200 paired hands. Branded's same 9 hands failed at one draw and at
  four.

  Scope, stated precisely: this means **re-drawing the same goal does not rescue
  it**, not that those hands hold no line at all. They might solve toward an
  easier target.

  Sample size, stated honestly: only branded had any unsolved hands, so the
  evidence is 9 brick-hands here plus the 5 that `paired_check` re-solved three
  ways on the active deck — **0 of 14 rescued, 95% CI 0-22%.** That rules out a
  large effect, not a small one.

  So the "Fix" below is **not warranted**: routing `evaluate` through the full
  sweep would cost four times the solver time to chase an effect measured at 0
  and bounded at 22% of bricks. `confirm_bricks` stays as the guard — it is free
  when there are no false bricks, and it is now the second measurement to find
  none. This joins CLAUDE.md's "measured and NOT worth doing" list rather than
  its backlog.
- **Fix (NOT TAKEN — see above):** route `evaluate` through the sweep, or at
  minimum re-draw the hands it calls bricks before believing them.

---

## P1 — a whole feature reports numbers that mean nothing

### 4. The going-second harness never put a board on the field — SEARCH FIXED 2026-11-02 in the rooted binary (patch 0002); `second`/`gauntlet.rank` not yet moved onto it

**2026-11-02, resolution.** The missing guidance was the whole problem, plus one bug
nobody had seen. `patches/0002-rooted-search.patch` adds `--root <prefix>`, which
makes probe, NRPA and finisher all root at the resumed position, and fixes
`MSG_SELECT_PLACE` answering the zone owner RELATIVE to the chooser (the core wants
the absolute player), which turned every player-1 summon into a MSG_RETRY -- the
unnamed "second blocker" in patch 0001's note. Measured with
`tools/experiments/rooted_gate.py`, empty field, 8 hands, 60 s: kewl-tune going first
8/8, `--approach` turn 2 0/8, `--root` turn 2 8/8; snake_eye 7/8, 0/8, 7/8 with the
same hand failing both ways. Stage 9 (`skills/game.py`) runs on it. The pinned binary
is unchanged, so `second`, `gauntlet.rank` and `optimizer.side_plan` still refuse;
moving them to `game.ROOTED_BIN` + `--root` is the remaining work. The account below
is the history.

**Every going-second number this project ever printed was measured against an empty
field, on both sides, with the search pointed at a seat nobody controlled.** That is
the whole explanation of the result that blocked this feature: a board holding one
vanilla Alexandrite Dragon "stopped" 18/18 meta going-second hands because there was
no board in front of them and no board behind them either.

`going_second_args` passed the start replay as the solver's **positional** argument.
That slot is a TEMPLATE, not a position. With `--deck`/`--hand` beside it the solver
enters synthetic-start mode and builds a **fresh duel** — it prints
`=== synthetic start ===` and names the template's contribution, which is "flags, LP,
hand size, draw count, opponent decklist" and nothing else. The recorded line went in
only as a reference to transplant.

Queried directly at the root (`breaking.root_state`, which replays the prefix and asks
the core), the position the search actually started from was:

| | player 0 | player 1 |
|---|---|---|
| monster zones | empty | empty |
| hand | 5 | 5 |

And the seat: synthetic-start substitutes `--deck`/`--hand` into **side a**, i.e. engine
player 0. `--player 1` therefore pointed the search at side b, whose deck and hand came
from the template's own decklist order. The solver's starting-position block proves it —
with six cards passed to `--hand`, it listed five different ones (Effect Veiler, Ghost
Belle, Kewl Tune Reco, Ghost Belle, Kewl Tune Cue), and they are side b's list, not the
hand anybody asked for.

So all four earlier "fixes" were real bugs about the wrong thing. Each one changed which
hand `--hand` carried, and `--hand` was never reaching the searched seat:

1. `--hand` does not reach player 1 — true, and the reason is the seat, not the flag.
2. the hand is not the tail of the `--deck` file either — true, same reason.
3. the dealt list must go into the START replay — it changed the hand *of the seat that
   was never searched*.
4. `template_vs` hardcoded `shuffle_decks=False` — a real bug, and the one fix that
   still matters: it is what makes the template seed vary the other seat's hand.

**The supported shape, and it works.** `--start` plus `--approach` is how this solver is
told to search from a position a duel has reached; it is what
`ygorl.solver.combo_solver.SolveRequest` uses for continuations. `start` carries the
duel's parameters with no responses, `prefix` the same header plus the responses that
reach the position, and the finisher roots on the approach's own duel:

    <reference> --start <start.yrpX> --approach <prefix.yrpX> --no-ref
                --target <payoff> --player 1

Built that way and queried at the root, the position is a real turn 2:

| | board builder | attacker |
|---|---|---|
| monster zones | **1 card** | empty |
| hand | 3 | **6** (five plus the draw for turn) |
| deck | 33 | 34 |

`breaking.check_root` is that check and it now runs on every position the module builds,
refusing one where the attacker never drew or the board is absent.

**The turn cuts are real and the patch fixes them.** Three hardcoded `turns >= 2` sites
in `solver-patched/search.cpp` assume a first player's turn-1 combo. At a resumed root
the duel is already at turn 2, so the approach root was `EXHAUSTED` after **0
expansions**. With `patches/0001-going-second-turn-cuts.patch` applied to a separate
binary:

| | turn cuts | approach root |
|---|---|---|
| pinned | 100% of rollouts | EXHAUSTED, 0 expansions, 0.0 s |
| patched | 4% of rollouts | 591 expansions, 4.5 s, budget-limited |

**And it still does not solve.** This is the part that keeps the feature unsupported, and
it is now a two-line reproduction instead of an inference from a printf. Same deck, same
target, same budget, same patched binary, empty field in every case:

| position | result |
|---|---|
| turn 1, player 0, approach carries no responses | **SOLVED** |
| turn 2, player 1 — the going-second shape | nothing |
| turn 3, player 0, both players having passed | nothing |

So it is **not the seat and not the turn depth**: it is that a resumed root gets only the
finisher's bounded-discrepancy walk, with no plan, no probe and no repertoire — the
guidance machinery is built for the duel's start. 1306 expansions in 4.5 s is nothing for
a line needing ten correct decisions. Confirmed from the other side too: both attacker
hands that reached nothing going second **solve in 30 s going first**, with six cards.

Retired beliefs, all of them stated here because each one cost time:

- ~~"the mode never summons from the extra deck"~~ — the evidence was the solver's own
  hint line on `hindsight: 0 substitute goal(s) kept`. Hindsight only counts extra-deck
  summons *in rollouts that survive*, and 100% of them were being turn-cut, so the
  counter was reporting the turn cuts, not an extra-deck defect.
- ~~"`!! REQUESTED but INERT` is consistent with it"~~ — that line named `reenter 0.50`,
  a ladder-dependent heuristic, and has nothing to do with summoning.
- ~~"the floor is not a floor"~~ — the floor was fine; there was simply no board in
  front of it, and no board in front of the real candidates either, so all of them
  measured the same empty position.

**What changed in the code.** `going_second_args` raises instead of returning a command
line, because the broken one had been copied into three call sites and the fixes landed
in different subsets of the three. `breaking.second_position` is the single builder, and
it takes `board=False` for the **paired control**: the same decks, the same seed and the
same cards dealt to the attacker, with the builder passing. That replaces the deck of 40
vanilla monsters, which changed the decks, the seed and the attacker's hand all at once —
so a difference against it was never attributable to the board. `gauntlet.floor` is gone;
each candidate carries its own control.

`floodgate_check` is rewritten onto the same builder. It had never received fix (3) at
all: it still passed `--deck our_deck` with a sampled `--hand`, four lines from the call
site that was fixed, so every floodgate was tested against one hand however many were
asked for.

**The feature that replaced it.** `gauntlet` now ranks boards with
`gauntlet.rank_boards`, which asks the same question of `--fire` — machinery that works
and is already measured over 40 hands per cell. See #69.

## P2b — found on the 2026-10-24 02:00 pass

### 12. Every solver draw leaked a scratch directory — FIXED

`solve_hand` removes the `job` subdirectory it is handed, never the mkdtemp ROOT the
caller made, so `sweep._solve_one` and `gauntlet._one_draw` each leaked one directory
per solve. 914 of them under /tmp over two days, 126 MB, growing by one per
(hand, goal, draw). Fixed in af67334 with a `finally` in both paths plus a test; 856
stale dirs cleared (126M -> 16M).

### 13. Long background jobs do not survive a turn boundary — FIXED by procedure (setsid + age check)

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

### 16. Orphaned pool workers survive for days — FIXED at the source

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

### 17. `pkill -f` on a job pattern kills the caller — FIXED by rule (kill by PID or pkill -x)

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

**New symptom of the same root cause, 2026-10-25.** The footgun also has a
*silent* form that destroys work rather than shells. Two background pollers
armed as `until ! pgrep -f run_floor.py; do sleep 20; done; <launch next step>`
**never terminated**, because `pgrep -f` matched the polling shell's own command
line — the poller saw itself as the job it was waiting for. Nothing died and no
exit code showed it; the chained next step simply never fired, and the floor
diagnostic had to be launched by hand. Both pollers were still spinning hours
later and were stopped today.

So `-f` in a *wait* loop is as wrong as `-f` in a kill: use `pgrep -x <exe>`,
match a PID captured at launch, or hide the pattern from itself with a character
class (`'[r]un_floor.py'`, which does work — the third poller used it). The rule
is unchanged and now has two failure modes behind it.

### 19. Nothing can compare the sweep and the funnel on the same hands — FIXED (paired_check)

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

### 22. The deployed web service is 5 days stale — FIXED via the #36 procedure — was LOW impact

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

### 28. `import` silently repoints every bare command — FIXED (notice names what it replaced)

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

### 32. The depth rounds spent their budget on goals already at their ceiling — FIXED

My regression, caught by validating the new board library against the old one before
promoting it. Same 40 hands, same goals, same deck:

| deck | old 1-draw | new multi-draw |
|---|---|---|
| branded | 1.58 | **1.84** (better, as expected) |
| despia | 2.38 | **2.27** (WORSE) |

More draws should never produce a worse board. Five despia hands regressed, and hand
10 shows the mechanism:

    draw 0  stack:2         unsolved  best_placed=1   (15 s, triage)
    draw 2  piece:51409648  solved
    draws 3-7               piece goals, five more times

`stack:2` is the only goal on that hand that could yield TWO interrupters. It got one
15-second attempt, was ranked behind two solved single-piece goals, and **never saw
the full budget** — while six draws went to a goal already capped at one.

`allocate` ranked "solved" ahead of "near miss", which is backwards whenever the near
miss asks for more: a solved goal can only pick up incidental extras, while an
unsolved goal demanding more interrupters carries the real upside. It now ranks by
what a goal could be worth (how many targets it demands) first, and only then prefers
solved over near-miss at equal demand — so the earlier rule survives where it was
right. Budget unchanged.

**Validated on branded, and the honest result is NEUTRAL.** Re-ran branded under the
fixed ranking and compared it per hand against the stale-ranking run (same 40 hands,
confirmed identical):

| | |
|---|---|
| hands better | **0** |
| hands worse | **2** |
| hands the same | **38** |
| mean paired delta | **-0.050** |
| sign test over the 2 differing hands | two-sided **p = 0.50** |

Indistinguishable from noise. The two differing hands moved because a different
allocation draws different (reproducible) seeds, not because the ranking is worse.

So the fix is **justified by the despia diagnosis, not by an aggregate gain**: hand 10
had `stack:2` — the only goal that could yield two interrupters — get one 15-second
attempt while six draws went to a goal already capped at one. That mechanism is wrong
regardless of how it averages out, and branded simply has few hands with that
structure. **VALIDATED on despia — the fix is real.** Re-ran despia under the fixed ranking and
compared it per hand against the stale-ranking run:

| | |
|---|---|
| hands better | **11** |
| hands worse | **0** |
| hands the same | 29 |
| sign test | **p = 0.001** |

Eleven hands improved and none regressed, on exactly the deck where the mechanism was
diagnosed. And against the old single-draw library despia now goes **7 better, 0
worse (p = 0.016)** — where before the fix it was *worse* than the old library
(2.38 → 2.27). So the fix turned a regression into a measured gain.

The stale-ranking records are kept as `boards_multi.stale-ranking.jsonl` — they are
the control that caught the bug, and the paired comparison above needed them.

### 33. `stats.wilson` audited — AUDITED — CORRECT; one input edge hardened

Audited because every confidence interval reported this session rests on it. It is
right. Checked against the closed form, including the exact figures quoted for the
active deck:

| case | interval | |
|---|---|---|
| 40/40 | 0.912 – 1.000 | the "91-100%" reported |
| 15/20 | 0.531 – 0.888 | the "53-89%" reported |
| 20/20 | 0.839 – 1.000 | the "84-100%" reported |
| 0/20 | 0.000 – 0.161 | |
| 10/12 | 0.552 – 0.953 | |

`trials == 0` returns `(0, 0, 1)` — "no information" — rather than crashing, which is
correct for the one call site that can reach it (`web/views.py` falls back to 0).

**One edge hardened:** `successes > trials` reached the sqrt with p > 1 and died on
`math domain error`, which names nothing. It now raises a message that says the
arguments are `(successes, trials)`. Loud either way; now legible.

The known values are pinned in a test, so a change to the interval maths cannot
silently move every number in the project.

### 34. Job queue audited — AUDITED — CLEAN, no fix needed

Checked after restarting the worker, since that is exactly when a claimed-but-
unfinished job becomes stuck forever (CLAUDE.md records a restart killing a job
mid-flight twice). No stuck jobs — and `web/worker.py` already resets orphaned
`running` jobs on startup, with the comment "A job left 'running' by a crashed worker
would block the queue". `jobs.cancel` also refuses to end a running job on purpose,
because "killing a running solver mid-sweep leaves partial records". Both correct;
recorded so the area is not re-audited.

### 35. The handtrap ranking never said how much of your room it saw — FIXED

Audited `config.weights` and `weighted_handtrap_ranking` because they underpin every
weighted ranking, the same foundational role as `stats.wilson`.

**The maths is correct.** Each card's score is divided by `card_weight[code]` — the
weight of the decks it was actually measured on — so it does not assume full coverage
and is not understated by the missing share. The weights file also already sums to
1.0, so normalisation is a no-op and there is no silent rescaling.

**What it could not say is how much of the room it covered**, and that matters because
`threat_handtraps` turns this ranking into the handtraps the OPTIMISER fires. So the
share is a decision input, not a footnote. Measured:

| | |
|---|---|
| weighted coverage of the stated meta | **58%** |
| `rogue` — the single largest slice — | **0.250, no deck file** |
| also unmeasured | mitsurugi 6%, azamina 4%, invoked 4%, doomz 3% |

`scenarios.covered_share` reports it and `matrix` now prints it alongside the existing
deck-count caveat — which is a different number (70% of decks vs 58% of weight) and
the weighted one is the one that decides. The note also states the consequence the
normalisation cannot fix: a handtrap that only answers an unmeasured deck cannot
appear in the ranking at all.

### 36. The deployment goes stale with every commit — FIXED — now a standing procedure

Issue #22 was "the deployment is 5 days stale"; it was restarted. **It went stale
again within 90 minutes**, because commits kept landing: the worker restarted at
23:03 and the next commit was 00:36. Of the `skills/` files changed after the
restart, the web app imports **`skills/drill.py`** — so the live drill page was still
showing the flat count bug (`0` for hands outside the companion summary) that had
already been fixed in the repo.

So this is not a one-off to tick off; it recurs on every commit that touches a module
the web app imports (`carddb`, `db`, `engine`, `handlab`, `skills.deck`, plus
`skills/drill.py` via the drill view).

**Resolved for now with the documented SIGHUP**, which reloads the gunicorn workers
without touching the job worker: checked `jobs.active` was None, sent SIGHUP to the
master, confirmed 3 gunicorn procs, the job worker untouched, `/login` answering 200,
and a fresh worker start time.

- Note on the documented command: the master is the process whose **ppid is not 1**
  (it sits under the tmux session). My first attempt filtered `$2 == 1` and found
  nothing. CLAUDE.md has it right; I read it backwards.
- **Standing rule:** after any commit touching those modules, SIGHUP. A full
  `systemctl restart` is only needed for `web/worker.py` or `skills/` the WORKER
  uses, and it kills the job in flight.

### 37. `gate.py` audited — AUDITED — CLEAN, no fix needed

Audited because "the gate passed" appears throughout CLAUDE.md and a defect there
would make every pass claim suspect. There is no automated verdict: `gate_report`
summarises and `inspect_bricks` lists brick hands with card names so a human can
judge — "a brick hand holding an obvious starter means the solver failed, whatever
the rate says". `solve_rate` is the honest unconditional rate over all hands, and the
comment about discounting genuine bricks describes the human step, attached to the
`brick_hands` field it explains. Correct as written; recorded so it is not
re-audited.

## P3 — correctness and housekeeping

### 9. The optimiser ledger is truncated, so it is not a checkpoint — FIXED (ledger archived, not truncated)

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

### 11. Known blind spots, already documented, still open — OPEN — documented limits, not defects

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
- **Maxx "C": CORRECTED 2026-10-25 — the cost is measured, and this entry used to
  say it was not.** The old wording ("what it *costs* — the cards handed over — is
  invisible to the metric") is true only of the **survival** metric, which is the
  narrow claim. A separate measurement answers the real question and has for some
  time: Maxx "C" draws on every special summon, so the replayed summon count *is*
  the cost, and `threats` prints it as "cards surrendered to Maxx 'C' if they combo
  through":

  | deck | cards handed over | worst |
  |---|---|---|
  | fiendsmith | **8.4** | 15 |
  | elfnote | **6.1** | 13 |
  | light-and-darkness-ritual | 4.2 | 14 |
  | despia | 3.0 | 7 |
  | kewl-tune | 2.5 | 6 |
  | sky-striker | 2.0 | 5 |
  | branded | 0.6 | 6 |

  So the correct statement is: **survival reads "fine" at 94% conversion while
  fiendsmith hands over 8.4 cards**, and the two numbers must be read together.
  Calling it a blind spot invited the opposite error — ignoring a table that
  exists. The figures above are now also stated on the web `threats` page with
  their source library (#50, #52).
- **`special_summons` in `trials`: storage gap only, not a measurement gap.** The
  column really is never populated — verified 2026-10-25, **0 non-null of 316
  rows**. But the old wording, "real counts need each line replayed through
  ygorl", describes work that is **done**: `scenarios.special_summons` replays each
  line and counts `MSG_SPSUMMONED`, and as of #50 `threats` regenerates those
  counts whenever its cache is missing, older than the library, or built from a
  different one. What is missing is only writing them into `trials`, which nothing
  currently reads from.

---

## 38. Handtrap survival was labelled with the wrong denominator — FIXED 2026-10-25

**Severity: high.** It inverts the maining decision, which is the whole
point of `matrix`.

`handtrap_matrix` computes `survival = survived / tested` where `tested`
counts **hands that had at least one legal window** and `survived` counts
hands where `fire.survives` is true — and `survives` is the all-windows
standard (`converted >= windows`). So the number is "of the hands where it
was live, how often did the line survive it being fired in every window."

All three display sites described it as a **per-window** rate: "of the
windows where the handtrap could be used, how often the line still
completed" — `skills/scenarios.py` docstring, `daemon.py matrix` legend,
and the `web/templates/matrix.html` caption.

**Measured over the 47 cells of `results/handtrap_matrix_40h.jsonl`:**
mean |per-window − per-hand| = **17.3 points**, max **77**, and **8 of 47
cells land on opposite sides of 50%**. The extreme case is the damaging
one: fiendsmith vs Effect Veiler is **0% per hand and 77% per window**.
Veiler stopped the line in all 35 hands where it was live; a reader
following the printed label concludes it gets played through three times
in four and declines to main it.

The computation was always right — `weighted_handtrap_ranking` consumes
`1 - survival` as "given a window, chance the line fails", which is the
per-hand reading — so **no stored ranking was wrong**. Only the labels
lied, and they lied in the direction that makes a stopping handtrap look
played-through.

Fixed: all three labels now say hands, name the all-windows standard, and
give the "8 of 11 windows converted counts as 0" example.
`test_handtrap_survival_denominator_is_hands_not_windows` pins the metric
(mutation-checked: a per-window denominator returns 0.73 and fails) and
`test_handtrap_matrix_labels_say_hands_not_windows` pins the wording.

**Not done, deliberately:** the per-window conversion rate is genuinely
informative and very different — it is the "do they play through it"
question the old label was reaching for. Showing both would be a better
matrix. That is a display change rather than a bug, so it is recorded here
rather than taken.

This is the **eighth** instance of the defect class that has dominated
this review: a number printed without the thing it is over. The other
seven are #6, #9, #13, #18, #22, #27 and #31.

---

## 39. CLAUDE.md's architecture map sent you to two files that never existed — FIXED 2026-10-25

**Severity: high**, because of *which* two files and *who* it misdirects.

The architecture diagram listed `skills/solver.py` ("Action-tree search over
own legal moves") and `skills/scoring.py` ("End-board evaluation"). The
configuration table gave them tunable knobs: `MAX_DEPTH`, `MAX_NODES`,
`BEAM_WIDTH`, and "Board weights: negates, card advantage, ATK, GY
resources".

**Neither file has ever existed.** There is no Python search and no weighted
board score. The search is the C++ `combosolver` driven by a wall-clock
budget, so the only real lever is `engine.SOLVE_MS` calibrated with `ladder`;
board quality is whether the line satisfied the planned `--target`
conjunction plus `solver.blocking.interruptions` counted by ygorl.

This is the costly part: the original request that opened this whole review
was *"it isn't getting to the best end boards"*. Anyone acting on that —
the user, me, a future session — reads the config table, goes looking for
the board weights it promises, and finds nothing. The table hides the one
lever that works behind two that do not exist.

**Audited every row and every diagram entry.** Also wrong:

- **10 real modules were undocumented**, including `gate.py` (an entire
  implementation stage) and `config.py` (which reads `locals.toml`, the
  weights every weighted ranking inherits — the file the "placeholder
  weights" limitation is about, with no entry in the map).
- **8 of the table's constants do not exist.** `card_db` has no
  `YGOPRODECK_API`, no `RATE_LIMIT_RPS` and **no `CACHE_DAYS`** — it has no
  day-based TTL at all; `needs_refresh` compares the stored `checkDBVer`
  against the remote one, so the mirror turns over when YGOPRODeck's database
  actually changes. The rate limit is `meta_scrape.MIN_INTERVAL` (0.4 s).
  `meta_scrape.FORMAT` is really `DEFAULT_FORMAT`, `LOCALS_WEIGHTS` does not
  exist, `engine`'s `CORE_PATH`/`SCRIPTS_PATH`/`CDB_PATH` do not exist, and
  neither does `scenarios.TRIALS_PER_SCENARIO`.
- **`card_db.py` and `carddb.py` both exist**, one underscore apart, and do
  different jobs — the YGOPRODeck mirror versus the merged BabelCDB the rules
  engine loads. Importing the wrong one fails silently because both answer
  about cards. Documented rather than renamed: renaming is a design change.

Fixed: the diagram now lists all 20 modules grouped by what they are for, the
table lists only constants that exist, and both carry a note saying what is
absent and what the real lever is.

`tools/check_docs.py` had passed throughout, because it only ever checked CLI
commands — a guard that validated the one section that had not drifted. It
now also fails on a named module that does not exist, a real module that is
not documented, a table constant not defined at module level, and a table row
naming a missing file. Mutation-checked on all four.

One redundancy noted, not fixed: `scenarios.NIBIRU = 27204311` is defined
twice (lines 632 and 683), same value, so it is harmless shadowing.

---

## 40. Every forbidden card validated as legal at three copies — FIXED 2026-10-25

**Severity: CRITICAL. This is the worst bug found in this review**, because
it is the one that loses a tournament before a single game is played.

`card_db.banlist_limit` mapped the ban list like this:

```python
return {"Banned": 0, "Limited": 1, "Semi-Limited": 2}.get(status, 3)
```

The mirror does not store `"Banned"`. It stores **`"Forbidden"`** — 121
cards on the TCG list, 
including Apollousa, Abyss Dweller, Agido, Archlord Kristya and
Archnemeses Protos. No key matched, so `.get(status, 3)` returned **3** for
every single one of them.

`"Limited"` and `"Semi-Limited"` were spelled correctly and worked
perfectly, which is exactly why this survived: the ban list *looked*
functional. `check` would tell you a deck running three Apollousa was
legal.

**It is not only `check`.** `optimizer.py:116` excludes candidates with
`banlist_limit(conn, card_id) == 0`, so no forbidden card was ever
excluded — **`optimize` could recommend adding a banned card to a
tournament list** and then rank it as an improvement.

**Measured on `assets/decks`, before and after:**

| deck | before | after |
|---|---|---|
| Blitzclique | **legal** | 1 — Dimension Shifter forbidden |
| kewl-tune | **legal** | 1 — Kewl Tune Rotary ×3 forbidden |
| snake_eye | 1 problem | **4** — +3 forbidden (Original Sinful Spoils ×3, Maxx "C" ×2, Moon of the Closed Heaven) |

Two decks flipped from "legal" to illegal and snake_eye's problem count
quadrupled. The one problem snake_eye did report beforehand was Triple
Tactics Talent at "limit 1" — a `Limited` card, the spelling that worked.

The active deck `blue-eyes-branded-test` is **legal** both before and
after, so no tournament list of yours was affected. The bug was still live
for every future `check` and every `optimize`.

**Fixed.** `BANLIST_LIMITS` now maps `Forbidden` and `Banned` (the API has
used both) plus `Unlimited`, and an unrecognised non-null status **raises
`ValueError`** instead of defaulting to 3 — the permissive default is the
entire mechanism of this bug, and one upstream rename would reopen it
silently. `Deck.validate` catches that and reports it as a per-card
problem, so `check` still works and says what it could not read.

Three tests, all mutation-checked: the `Forbidden`/`Banned` mapping, the
refusal on an unknown status, and `validate` surfacing it.

## 41. Pendulum extra-deck frames were treated as main-deck cards — FIXED 2026-10-25

**Severity: medium** — a false legality error on real lists, and a missed
real one.

`deck.EXTRA_FRAMES` held `{fusion, synchro, xyz, link}`. The mirror also
has `fusion_pendulum` (15 cards), `synchro_pendulum` (8) and
`xyz_pendulum` (10), which are **extra-deck cards** — a Fusion Pendulum
monster such as Starving Venom Wing Dragon goes in the extra deck.

Both directions were wrong. A correctly-built list with one in its extra
deck was told `"main-deck card in the extra deck"` — and the scraped decks
really do hold 3 `fusion_pendulum` and 2 `xyz_pendulum` cards in their
extra sections, so this fires on genuine tournament lists. The same card
sitting illegally in a main deck went unflagged.

`ritual_pendulum` (6 cards) is deliberately left out: Ritual monsters are
main-deck cards.

Fixed with a mutation-checked test covering both directions and pinning
`ritual_pendulum`/`effect_pendulum` as main-deck frames.

**Related note, not a defect:** the `cards` table has a `frame` column that
CLAUDE.md's schema section does not list, and `frame` is what every
section-routing decision reads. Added to the schema docs.

---

## 42. A solver crash was counted as a deck brick — FIXED 2026-10-25

**Severity: medium**, latent today but high the moment it fires — and
CLAUDE.md documents the thing that makes it fire.

Found by hunting siblings of #40's defect class: a lookup on external
vocabulary where an unhandled member degrades silently. The solver's
records carry **four** status values — `solved` (1236 across all stored
results), `brick` (103), `unsolved` (41) and `error` (12) — while the code
branches on only `solved`, `brick` and `no_window`. `error` is named
nowhere.

`sweep.summarize` does:

```python
row["hands"] += 1
if r["status"] != "solved":
    continue
row["solved"] += 1
```

So an errored hand lands in the denominator and not the numerator: it
depresses `solve_rate` **exactly like a brick**, and no summary field or
display said a crash had happened. The 12 stored `error` hands are all
`solver exited with -11` — SIGSEGV, the BabelCDB segfault CLAUDE.md warns
follows any submodule bump without `daemon.py setup`.

**Checked rather than assumed, in both directions:**

- The 12 errored hands are confined to `results/boards_stale_prefix.jsonl`,
  an archived arm. **No live record carries one**, so no number I have
  reported is wrong. This is a latent defect, not a stored error.
- `sweep.summarize` is **byte-identical to the vendored
  `tools/solve_blocking.py:summarize`** it says it copies, so this is
  inherited upstream behaviour, not drift in duelroom's copy. The
  docstring's contract warning was doing its job.
- No `summary` anywhere carries a nonzero `errors` count, so the existing
  `check_counts` invariant (`solved + bricks + errors == hands`) has never
  had anything to check.

It matters because `brick_rate` carries weight **-2.0** in the optimiser
objective, the heaviest term after nothing. A handful of segfaults during
an `optimize` run — a submodule bump away — would make it prefer whichever
candidate list happened not to crash, and present that as a deck
improvement.

**Fixed:** `summarize` now counts `errors` per deck, and `boards` prints
`(N SOLVER FAILURE(S), not deck bricks)` beside the solve rate — the same
treatment `floodgates` already gave them at `daemon.py:1429`, which is
where the precedent came from. `solve_rate` **keeps** its definition
(`solved / hands`), because `gate.load_boards` and every stored record
depend on it; the fix is that the count exists and is shown, not that the
rate changes meaning.

Two tests, mutation-checked.

**Also checked and CLEAN:** `meta_scrape.PLACEMENT_RANK`, the other
vocabulary map that became load-bearing when `best_list` started choosing
which scraped list `export` writes. All **505** stored deck rows resolve to
a real rank (`Top 8`, `Top 4`, `Top 32`, `Runner-Up`, `Winner`, `Top 64`,
`Top 16`, `3rd Place`) — 100%, with the `.strip().lower()` normalisation
doing the work. Its `top N` fallback orders unlisted sizes correctly too.
No fix needed.

---

## 43. Two of the five floodgates were the wrong card — FIXED 2026-10-25

**Severity: high for the feature.** 40% of `floodgates` tested nothing.

Same defect class as #40 and #42, found by sweeping the rest of it: a
hardcoded lookup on external vocabulary, degrading in silence. Checked
every id in `HANDTRAPS`, `FLOODGATES` and `BLIND_SPOTS` against the
mirror:

| listed as | id | actually is |
|---|---|---|
| There Can Be Only One | `97642679` | **Dark Master - Zorc**, a Ritual Effect Monster |
| Summon Limit | `23616456` | **not a card at all** |

So `floodgates <deck>` spent two of its five checks building a board
around a Ritual Monster that is not a floodgate, and one around a card
that cannot exist. A missing card just fails to assemble, so both read as
"could not build the board" rather than as an error — the feature looked
partly untestable instead of misconfigured.

Correct ids: There Can Be Only One is **24207889**, Summon Limit is
**23516703**. All 7 `HANDTRAPS` ids were already right, which is why the
matrix was unaffected.

Fixed, with a test that resolves **every** hardcoded id against the mirror
and compares it to its own label — the only thing that catches a
transposed digit. Mutation-checked.

## 44. Face-up defence was labelled "(set)" — FIXED 2026-10-25

**Severity: high.** 27.3% of every board card the tool has ever printed.

`drill.describe` and `handlab`'s board rows both tested:

```python
facing = " (set)" if pos in (4, 8, 0xA) else ""
```

under a comment stating "EDOPro position bits: 0x4|0x8 are the face-down
ones". That comment is wrong, and it is the whole bug. From
`ygorl.engine.constants`:

```
POS_FACEUP_DEFENSE    0x4      <- FACE UP
POS_FACEDOWN_ATTACK   0x2
POS_FACEDOWN_DEFENSE  0x8
POS_FACEDOWN          0xA      = 0x2 | 0x8
```

So the test was wrong in both directions: it called every **face-up
defence** monster set, and would have missed a **face-down attack** one.

**Measured over every board in `results/*.jsonl`:**

| zone | position | meaning | n | old label |
|---|---|---|---|---|
| mzone | 1 | face-up attack | 1981 | face-up ✓ |
| mzone | **4** | **face-up defence** | **1480** | **SET ✗** |
| szone | 0xA | face-down | 1185 | set ✓ |
| szone | 0x5 | face-up | 683 | face-up ✓ |
| mzone | 8 | face-down defence | 87 | set ✓ |

**1480 of 5416 — 27.3% — were labelled wrong**, and all of them in the
same direction: a face-up monster reported as set. For a monster that is
the difference between its effect being live and not, and `drill`'s whole
purpose is telling you what your end board actually is. Verified on a
stored branded line: Mirrorjade the Iceblade Dragon in face-up defence now
reads as a monster rather than a set card.

`drill._on_field` used `& 0x5` correctly all along, so **the ranking and
the text disagreed about the same card** — the same shape as #30, in a
different place. `sweep.summarize` was also already correct.

Fixed in both places with `& 0xA`. The test pins the engine's own four
constants (so a vendor bump that moves them fails here rather than
quietly changing what "set" means), checks all four positions through
`drill.describe`, and guards `handlab`'s copy at the source since it needs
a solved hand to reach.

**One self-inflicted miss worth recording:** the source guard initially
failed on `drill.py` because the explanatory comment I had just written
contains the literal string `in (4, 8, 0xA)`. The guard was matching its
own documentation. It now strips comment lines, and was re-verified to
still catch a real reversion in `handlab.py`.

---

## 45. `ZONE_RANK` had no rank for `mzone` — FIXED 2026-10-25 (latent)

**Severity: low-latent.** Nothing emits the zone today; the fix is one
line and a guard.

The last unswept instance of #40's class. `_zone_rank` is
`ZONE_RANK.get(_zone_of(spec), 1)` and `derive_targets` ranks candidate
targets by it to choose the most **substantive** one — the mechanism that
stops ygorl's planner proposing "Droll in hand" as a deck's payoff.

The solver's own `--help` states its vocabulary:

```
zone = atk def (monster zone, face up), mzone, szone,
       hand, grave, banished. Add :fd for a card SET face down
```

`ZONE_RANK` covered `atk def "" grave banished removed szone szone:fd
hand` — **`mzone` and `mzone:fd` were absent.** A monster sitting in a
monster zone therefore fell to the default rank 1, level with a card set
from hand, two ranks under `grave` and three under the `atk` spelling of
the same board.

**Measured, and it is latent:** 0 of the stored target specs mention
`mzone`, and duelroom never constructs one — real specs are only `atk`
(1185), `hand` (1173) and `szone:fd` (467). So **no derived target was
ever mis-ranked.** It stays reachable, because `--target card@mzone` is
documented and `--target` is repeatable through
`SolveRequest.extra_args`.

Fixed: `mzone` ranks 3 (a field zone, same as `atk`), `mzone:fd` ranks 1
explicitly (a set monster is no more built than a set backrow card, which
is what the default happened to give — relying on a default that was
wrong one key over is the pattern itself). `removed` is kept and labelled:
it is not a solver zone, just an older alias.

The guard is the point:
`test_zone_rank_covers_the_solver_vocabulary` parses the solver's **own
`--help`** and fails if a zone appears there without an explicit rank, so
this cannot drift the way the ban-list map did. Mutation-checked.

**That closes the defect class** opened by #40 — a hardcoded lookup on
external vocabulary with a silent fallback. Six maps swept:

| map | verdict |
|---|---|
| `card_db` ban-list statuses | **#40, critical** — every forbidden card legal at 3 |
| solver record statuses | **#42** — a crash counted as a brick |
| `FLOODGATES` card ids | **#43** — 2 of 5 were the wrong card |
| `drill`/`handlab` position bits | **#44** — 27.3% of board cards mislabelled |
| `ZONE_RANK` zones | **#45** — `mzone` unranked, latent |
| `meta_scrape.PLACEMENT_RANK` | clean, 505/505 rows resolve |

Each of the five defects was invisible for the same reason: the map
answered every lookup, so nothing ever looked broken.

## 46. Web session and TLS configuration audited — CLEAN, no fix needed

Checked because memory records a real `session` cookie clash between
stockroom and flipper, and duelroom is a third Flask app on the same host
with an internet-facing port.

- `web/__init__.py` already sets `SESSION_COOKIE_NAME="duelroom_session"`,
  with a comment naming the stockroom/flipper collision as the reason. The
  lesson was already applied here.
- `SESSION_COOKIE_SECURE` is gated on `DUELROOM_TLS`, which looked like a
  way to ship a non-Secure cookie over HTTPS — so it was checked against
  the live process rather than the code: `/proc/<master>/environ` has
  `DUELROOM_TLS=1`, and `tmux-start.sh` passes `--certfile/--keyfile`. The
  flag is on in the deployment that matters.
- `HTTPONLY` and `SAMESITE=Lax` set; `SECRET_KEY` comes from the
  environment or `data/secret_key`, never a literal.

No change made. Recorded so the next pass does not re-derive it.

---

## 47. The exact hand math was unreachable where it mattered most — FIXED 2026-10-25

**Severity: medium.** The numbers were right; three of them could not be
asked for.

`skills/stats.py` is **correct**. Audited it rather than assumed it:

- `pmf` against `scipy.stats.hypergeom` over **728** `(N, K, n, k)` cases
  — max absolute difference **2.22e-16**, i.e. floating-point noise.
- `pmf` sums to 1 over its support; `at_least`/`at_most` complementary.
- `combo_chance` against **exact brute-force enumeration over labelled
  cards** on five cases, including three disjoint groups and a six-card
  hand — identical to 1e-12.
- Closed forms match: `open_chance(40,3,5)` = `1 - C(37,5)/C(40,5)`.
- A hand larger than the deck returns `0.0` rather than dividing by zero.

The defect is reachability. Checking every function for callers:

| function | callers in the app |
|---|---|
| `open_chance` | 3 |
| `brick_chance` | 2 |
| `wilson` | 6 |
| **`group_chance`** | **0** |
| **`combo_chance`** | **0** |
| **`trials_needed`** | **0** |

(`pmf`/`at_least`/`at_most` are internal helpers, reached through the
others.)

This is precisely the "parameter that exists and cannot be set" that
CLAUDE.md calls **this project's signature bug** — and it was stranding
the most useful question in the file. Handtraps are *interchangeable* for
"do I open any interaction", so per-card odds systematically understate a
deck. On the active deck:

| question | answer |
|---|---|
| best single handtrap (3x Ash) | **31.0%** |
| the group of 8, ≥1 | **65.3%** |
| the group of 8, ≥2 | **21.9%** |

`hand` printed only the per-card column. Read as the answer it is wrong by
**more than two to one**, in the direction that makes a player add
interaction the deck already has.

**Fixed.** `hand` now prints an interaction block built from
`HANDTRAPS ∩ deck` — naming every card that formed the group, so the
number carries its own denominator — at ≥1 and ≥2, going first and
second. On the active deck: 65.3% / 72.4% for ≥1, and 21.9% / 29.7% for
≥2, which is the actionable one, because going second you usually want
two pieces.

`trials_needed` now backs the advice it was missing from. The degeneracy
refusal said "re-run with more hands" without a number; it now says ~392
hands per arm unpaired to resolve a 10-point difference, and that paired
seeds need far fewer but the current count is well short either way.

`combo_chance` stays unreached: it needs a starter list, and there is no
reliable one. Guessing which cards are starters would be inventing data,
so it is left for when a list exists.

Two tests: one pins the group/per-card gap and the two wirings, one keeps
a closed-form check on the hypergeometric in the suite.

**Caught by my own guard:** the first version of this referenced `Counter`
without importing it. `tools/check_names.py` — added earlier in this
review after two missing-import incidents — failed with
`daemon.py:378 undefined name 'Counter'` before the code ever ran. Also
removed a `for ... break` loop left in my own edit that could only ever
run once.

---

## 48. Reachability swept: the side planner has no way in — FIXED 2026-10-25 (docs)

Followed #47 by sweeping the whole codebase for its defect, rather than
one module at a time: a function that exists, is documented, and cannot be
invoked. CLAUDE.md calls this the project's signature bug.

Method: parse every `skills/*.py` for public top-level defs, then AST-walk
`daemon.py`, `web/*`, `tools/*` and the other skills modules collecting
every call, **resolving import aliases** (`meta_scrape as ms`,
`from . import card_db`). My first two passes reported false positives
because they missed aliases and relative imports; the numbers below are
from the corrected pass, verified by grep.

**8 public functions are genuinely unreachable from app code:**

| function | verdict |
|---|---|
| `optimizer.side_plan` | **the finding** — see below |
| `optimizer.propose_side` | its companion, same gap |
| `stats.combo_chance` | needs a starter list (#47) |
| `stats.at_most` | helper, reached via `at_least`'s complement |
| `gate.load_blocking` | the DB-backed board-library store |
| `gate.liveliness_from_db` | reads per-card solve rates back out |
| `meta_scrape.discover_meta` | alternative scrape strategy |
| `meta_scrape.scrape_archetype` | alternative scrape strategy |
| `card_db.by_name` | name lookup; `builder.py` has its own search |

**The finding: the side deck planner cannot be run at all.** Nothing in
`daemon.py`, `web/` or `tools/` calls `side_plan`. CLAUDE.md said *"the
side plan and floodgate check are implemented but not run at scale — each
side candidate is `hands` full going-second runs, roughly ten minutes
apiece"*, which reads as runnable-but-slow and invites a user to try it
before an event. It is not slow. There is no route.

**And it must stay that way for now.** `side_plan` rates every candidate
through `breaking.going_second` — verified to be the *same* function
`daemon.py:1421` uses for the `second` command, which this file and
CLAUDE.md both mark **UNSUPPORTED** on a decisive measurement: kewl-tune
reaches its target in 10 of 12 hands going first and **0 of 4** going
second into one vanilla Alexandrite Dragon (#4).

So wiring up a `side` command would have handed over a planner whose
objective is known not to measure what it claims — for the 15 cards that
decide games two and three of every match. **Deliberately left
unreachable.** A confident wrong answer about a side deck before a
tournament is worse than no answer, which is the judgment this whole
review has been applying.

Fixed in the only honest way available: CLAUDE.md now says the side plan
has **no way in** and that its metric is the broken one, with a pointer to
#4 and the cost note preserved for whenever it becomes trustworthy. A
guard test asserts no app file calls `side_plan` and tells a future editor
to delete that test in the same commit if they are wiring it up because #4
is fixed.

**One inference of mine was wrong and is corrected here.** I first read
`propose_side` as dead code superseded by `side_in`, because `side_plan`
uses `side_in`. It is not: `side_in`'s own docstring says *"to test a card
that is not yet in the side, use `propose_side` to put it there first"*.
They are complementary — `side_in` moves a side card into the main deck,
`propose_side` changes what is in the side. Nothing was removed.

The remaining six are recorded rather than wired: two are alternative
scrape strategies superseded in practice by `scrape_recent`, two are the
DB-backed persistence path for board-library records (`runs`/`trials` are
populated by the funnel path instead), one needs data that does not exist,
and one is duplicated by `builder.py`'s own search. None of them is a
wrong number; they are unused capability, and that is now written down
instead of rediscovered.

---

## 49. The report showed the inflated survival and called it "fine" — FIXED 2026-10-25

**Severity: high.** It told you your deck beats a card it never beats.

`handtrap_matrix` carries a docstring warning that
`summary.survival_given_line` "counts a `no_window` outcome as a survival,
which conflates two opposite facts and inverts the answer" — and computes
its own number from the per-hand records. **Four other display paths read
the inflated field anyway:** `daemon.py` `gate` and `report`,
`skills/report.py`, and `web/views.py` → `report.html`.

CLAUDE.md states the principle this breaks: *"two implementations of a
number is how this project's worst bugs got in."* This was two
implementations that **disagree**.

**Measured over the 47 comparable cells of
`results/handtrap_matrix_40h.jsonl`:** 19 cells higher, 28 equal, **0
lower** — the summary field never reads lower, so it systematically
flatters the deck. Mean gap 10.3 points, max **91**, and **7 of 47 land on
opposite sides of 50%**.

| deck | card | summary | honest | live in |
|---|---|---|---|---|
| kewl-tune | Nibiru | **91%** | **0%** | 3 of 33 hands |
| sky-striker | Nibiru | **89%** | **0%** | 4 of 35 |
| despia | Nibiru | **83%** | **0%** | 5 of 29 |
| light-and-darkness | Nibiru | 83% | 25% | 8 of 36 |
| sky-striker | Ghost Ogre | 91% | 50% | 6 of 35 |

The mechanism is Nibiru's rarity: it is live in 3 of 33 hands, and the
other 30 `no_window` outcomes are each counted as a survival.

**Where it cost the most.** `report` printed these under *"PLAYS THROUGH
(higher is better — this is YOUR deck)"* and derived **both** the verdict
and the summary line from them:

- the verdict thresholds (`<=0.15` folds / `<=0.5` struggles / else fine)
  labelled Nibiru at 91% **"fine"**;
- `worst = min(judged, ...)` picked "your worst matchup card" from the
  inflated numbers, so a card the deck beats 0% of the time could never
  be named.

**Fixed with one implementation, not five.** `survival_from_hands(rec)` is
factored out of `handtrap_matrix` and is now the only place this is
computed. `report.deck_report` carries its output as `survival_tested`,
and all four display paths use it:

- `gate` prints `X% of the N hand(s) where it was live (live in N/M)`, or
  `never had a legal window in M solved hand(s) — UNMEASURED, not
  harmless`.
- `report` leads with `% is of the hands where the card was LIVE, not of
  all hands`, shows `live N/M` per row, judges on the honest number, and
  lists never-live cards separately as unmeasured rather than scoring
  them.
- The web panel gained the same caption and a `live N/M` column.

**On the active deck nothing changed**, and that is worth stating plainly:
Imperm and Ash are live in all 15 solved hands, so both methods agree at
0%. The gap only opens for rarely-live cards, and the active deck's funnel
never fired Nibiru. The fix matters for kewl-tune, sky-striker and despia
today, and for any future run of yours that does fire it.

Mutation-checked: making `survival_from_hands` count `no_window` as a
survival fails the test.

**Caught by my own guard, again:** the refactor left an unreachable
`return matrix` after `return row`, and `tools/check_names.py` failed with
`skills/scenarios.py:400 undefined name 'matrix'` before anything ran.
That is twice in two passes it has caught my own edit.

---

## 50. Three `threats` tables rendered a file nothing could write — FIXED 2026-10-25

**Severity: medium**, and it was about to bite tonight.

`results/special_summons.json` backs three of `threats`' tables — special
summons per line, "Nibiru live", and the cards surrendered to Maxx "C" —
and **nothing in the app wrote it**. `scenarios.special_summons` is the
producer; no command called it. The file was created out of band on
**2026-10-18** and had been rendered as current ever since.

Two consequences, the first immediate:

- It goes stale the instant the board library is rebuilt — which is
  **happening right now**. Tonight's re-run would have left these tables
  describing the old `boards.jsonl` with nothing saying so.
- Deleting it removed three tables permanently, with no way to get them
  back.

This is the reachability class again (#47, #48), with the twist that a
*stale artifact* of the unreachable function was already on screen.

**Checked, and the numbers themselves were right.** Regenerating from
`results/boards.jsonl` reproduced **all 7 decks byte-identical** to the
2026-10-18 file, so nothing displayed was wrong — the replay is
deterministic and the cache was honest, just unmaintainable. Cost: **6.6
seconds** for all 280 records, because replaying a known line involves no
search.

Fixed: `threats` now rebuilds the cache whenever it is missing or older
than the library it describes, and the table says which —
`(replayed, not inferred; rebuilt just now from boards.jsonl)` or
`cached from boards.jsonl`. Verified both paths: a deleted cache rebuilds
and matches, and a fresh one is reused without rebuilding.

**Audited and CLEAN in the same pass**, recorded so it is not re-derived:

- `trials.special_summons` really is never populated — **0 non-null of 316
  rows**, exactly as CLAUDE.md says.
- The discredited shortcut is **not** being shown as summons.
  `scenarios.response_windows` still computes Maxx "C" window counts, but
  `threats` prints it as *"interruption surface: times the opponent gets
  to respond"* with an explicit note: *"Not special summons — Maxx 'C'
  has no activation condition, so its window count tracks line length,
  r=0.94."* The correct replayed figure is separately labelled
  *"(replayed, not inferred)"*. Both numbers carry their meaning, which is
  the discipline this review has been enforcing — already applied here.
- Swept for other instances of #49's shape — a module warning against a
  field that other call sites still read. `survival_given_line` was the
  only one.

---

## 51. `targets` was hardcoded to the single-draw library — FIXED 2026-10-25

**Severity: high**, because of where it sits. Targets decide what every
later solve aims at, so this choice propagates into the solve rate, the
brick rate, the handtrap matrix and the optimiser objective.

`cmd_targets` read `results/boards.jsonl` unconditionally — the
**single-draw** library, which CLAUDE.md's own key finding describes as
understating every deck: *"the solver is a RANDOMIZED search, and one
attempt is one draw ... the best board a hand can reach is usually NOT the
one a single draw returns."* A multi-draw library existed and could not be
used, or even compared against.

**It changes the answer.** Deriving from the 4-deck multi-draw arm picks a
different target for **2 of 5** decks:

| deck | from single-draw | from multi-draw |
|---|---|---|
| branded | Rindbrumm the Striking Dragon | Stardust Dragon - Victim Sanctuary |
| fiendsmith | Fiendsmith's Desirae | F.A. Dawn Dragster |
| despia, elfnote, kewl-tune | *same* | *same* |

**The default deliberately stays `boards.jsonl`.** Silently re-pointing the
measurement chain at whatever file is newest would change every downstream
number with nobody asking, and newer is **not** better here — the
multi-draw arm above is `boards_multi.narrow-stacks.jsonl`, a control
preserved precisely because it was built by defective code. So instead:
the source is nameable (`targets [library]`), it is always printed with
its date, and newer candidates are **reported, never used**.

Found and fixed two bugs in my own first version of that reporting:

1. It recommended `newer[-1]` from a name-sorted list, which is
   `boards_multi.stale-ranking.jsonl` — a deliberately-kept defective arm.
   Candidates are now ranked by mtime, and any library whose stem
   contains a dot (`boards_multi.narrow-stacks` vs `boards_multi`) is
   listed as a preserved variant and never suggested.
2. It would have recommended `boards_multi.jsonl` while tonight's run is
   still appending to it — that library currently holds 2 of 8 decks.
   `sweep._checkpoint` writes `<out>.partial` for the duration of a run,
   so that file's presence is a reliable in-progress signal; such a
   library is now flagged `RUN IN PROGRESS — incomplete` and excluded from
   the suggestion. With nothing complete to offer it says so rather than
   naming something wrong.

Verified both paths and restored `assets/derived_targets.json` to its
original content afterwards (branded back to `51409648`).

**Audited in the same pass:**

- `scenarios.merged_targets_file` regenerates `targets_merged.json` on
  every call, so the file every funnel run is handed cannot go stale.
  Clean.
- `assets/derived_targets.json` (2026-10-24) is newer than the library it
  came from (2026-10-18), so that chain was consistent — the defect was
  the inability to choose, not a stale file.

---

## 52. `threats` was hardcoded to the single-draw library too — FIXED 2026-10-25

Sibling of #51, found by sweeping every hardcoded artifact path. Only one
other existed: `daemon.py` `cmd_threats` read `results/boards.jsonl`
unconditionally, so the special-summon and **"Nibiru live"** figures came
only from the single-draw library.

**It understates Nibiru exposure, because better boards need more
summons.** Measured on the same decks from both libraries:

| deck | single SS | multi SS | single Nibiru-live | multi Nibiru-live |
|---|---|---|---|---|
| elfnote | 6.1 | **8.2** | 68% | **90%** |
| despia | 3.0 | **3.9** | 32% | **48%** |
| kewl-tune | 2.5 | 2.7 | 5% | 8% |
| branded | 0.6 | 1.1 | 3% | 3% |
| fiendsmith | 8.4 | 7.9 | 100% | 95% |

Up to **22 points** understated, on the figure that answers "will I get
Nibiru'd" — a main/side-deck decision.

**Fixed with ONE shared selector, not a second copy.** `_board_library()`
now does the choosing for both `targets` and `threats`, for the same
reason `survival_from_hands` is one function (#49): two implementations of
a choice drift apart. Same deliberate default, same provenance line, same
refusal to auto-switch.

The cache also now records `_source` — the library it was built from —
because a **timestamp alone cannot detect a library switch**: a cache
built from a different library can still be newer than the one now
selected. Verified: switching to the multi-draw arm rebuilt correctly on
the `_source` mismatch despite the cache file being newer.

### Three bugs of my own in this change, all caught before commit

1. **I deleted a large block of `daemon.py`.** My first extraction used
   `s.index()` splicing and cut out most of the command table and several
   `cmd_*` definitions. `tools/check_names.py` reported **32 undefined
   names** — `_resolve_deck`, `cmd_gate`, `cmd_threats` and more. Restored
   with `git checkout` (the tree was clean at c9ed900) and redone as a
   verified line-range replacement with assertions on both boundary
   lines. Index splicing is the wrong tool for this; anchored or
   line-ranged edits are not.
2. **My own #51 test asserted the code's location, not its behaviour.**
   It scoped `args[0]`/`strftime` to `cmd_targets`' body, so extracting
   the helper broke it. Re-scoped to the helper, plus a new assertion
   that **both** consumers go through it — the invariant is that the
   choice is made once, not where it lives.
3. **Adding `_source` 500'd two web pages.** `web/views.py` builds its
   rows with `{"archetype": a, **v}` over every cache entry, and `**` on
   the new string value raised `TypeError: 'str' object is not a
   mapping` — breaking `/threats` **and** `/report`. I changed a shared
   data format and updated only one of its two consumers.
   `tests/test_web.py`'s smoke walk over every GET route caught it, which
   is exactly why that walk was added (#29). Both readers now skip
   underscore keys, mutation-checked, and the web panel states its
   provenance like the CLI.

---

## 53. The ban-list bug had a SECOND copy, in the deckbuilder — FIXED 2026-10-25

**Severity: critical, and I missed it in #40.**

#40 fixed `card_db.banlist_limit`, which read `"Banned"` while the mirror
stores `"Forbidden"` and so returned 3 copies for all 121 TCG-forbidden
cards. **`skills/builder.py:148` held a second, independent copy of the
same dict with the same defect** — and `builder.py` is the deckbuilder's
card search, the thing a list actually gets built in.

Found by applying the review's established defect-class checklist to the
modules not yet audited (`handlab`, `builder`, `importer`, `images`,
`carddb`) instead of waiting to stumble on it. The grep was one line:
`.get(` with a literal default on an external value.

**In the deckbuilder it was visible.** `web/templates/build.html` gates
the restriction badge on `c.limit < 3`:

```js
const lim = c.limit < 3
  ? `<span class="lim l${c.limit}">${['forbidden','limited','semi'][c.limit]}</span>` : '';
```

A forbidden card got `limit: 3`, so it showed **no badge at all**, `full =
n >= c.limit` never triggered, and the "Already at N copies — the list
allows N" warning never fired. **121 of 121** TCG-forbidden cards appeared
unrestricted, Apollousa and Maxx "C" included.

`Deck.validate` still refused to save such a list (#40 fixed the
authoritative check), so this was a **lying display rather than a saveable
illegal deck** — but it is the display a tournament list gets built in,
and the user would only learn at save time.

**Fixed with one implementation of the mapping, properly this time.**
`card_db.limit_for_status(status)` is now the single place the dict is
read; `banlist_limit(conn, id)` delegates to it for callers that only have
an id, and `builder` calls it with the row it already holds, so no N+1
query was introduced. Verified after: Apollousa and Maxx "C" report
`limit=0, badge=forbidden`; Triple Tactics Talent `limit=1, badge=limited`;
Ash Blossom `limit=3, no badge`.

The new test **fails on any module that re-spells the map** — it greps
every `skills/*.py`, `daemon.py` and `web/*.py` for a `"Semi-Limited":`
key outside `card_db` — and separately asserts the deckbuilder reports 0
copies for a forbidden card. Mutation-checked by restoring builder's
copy.

The lesson is the one CLAUDE.md already states and I under-applied: when a
number has two implementations, **fixing one is not fixing the bug.** #49
made the same point about `survival_given_line`; this is the same mistake
committed by me, one issue later.

**Audited CLEAN in the same sweep:** `handlab`'s follow-up labels use
`K.get(f"CATEGORY_{key}", 0)`, the same risky shape — all **13** keys
resolve to real constants, so no label is unreachable. Pinned with a test
anyway, because a vendor rename would drop a label from the pilot view
with nothing failing. No other silent-default lookups on external values
remain in those five modules.

---

## 54. Duplicated-computation sweep — one real instance, two audited clean — FIXED 2026-10-25

After the ban-list map turned up **twice** (#40, then #53 in the
deckbuilder) and `survival_given_line` turned up in five places (#49), I
swept for the class itself rather than waiting for a third: the same
quantity computed in more than one place.

**Audited CLEAN — correct by construction:**

- **"Is this an extra-deck card"** looked like a second implementation:
  `builder.is_extra(frame)` alongside `deck.EXTRA_FRAMES`. It is not —
  `builder.py:24` *imports* `EXTRA_FRAMES` from `.deck` and `is_extra` is
  a three-line wrapper over it. So the #41 pendulum fix propagated into
  the deckbuilder automatically, which is what that design buys.
- **Browser-side legality.** CLAUDE.md's load-bearing rule is that
  "legality is only ever `Deck.validate`, asked over HTTP on every edit —
  never reimplemented in the browser". It holds: `build.html` posts to
  `/api/validate` on a debounce, under a comment reading "legality, from
  the server and only from the server". No legality rule is evaluated
  client-side.

**FIXED — one real instance.** `build.html` carried
`const CAP = {main:[40,60], extra:[0,15], side:[0,15]}` — the deck size
limits as literals, duplicating `deck.MAIN_MIN`/`MAIN_MAX`/`EXTRA_MAX`/
`SIDE_MAX`.

Scoped honestly: **this is display only** and the values **matched**, so
nothing was being shown wrongly. `CAP` drives the section counter
(`47 / 40–60`) and whether that number is coloured red. The defect is
drift, not error: change `MAIN_MAX` and the browser would keep colouring
60 green while the server rejected the list — the user's two sources of
truth disagreeing with no warning, which is exactly how this project
describes its worst bugs arriving.

Fixed by passing `caps` from `skills/deck.py` through the view, so the
page renders the server's own constants. The test asserts the rendered
`CAP` equals those constants **and** that the literals are gone from the
template; mutation-checked by restoring the hardcoded version.

No other duplicated computation found in the sweep.

---

## 55. Swallowed-exception sweep: two silent failures, three audited clean — FIXED 2026-10-25

A class not previously swept: a broad `except` that hides a real failure.
It produces no wrong number, which is what makes it worse — the feature
quietly does less, or nothing, and nothing says so. Nine broad handlers
exist; each was read.

**FIXED — `scenarios.special_summons` (the worse one).** The replay loop
had `except Exception: continue  # a line that will not replay tells us
nothing` with **no tally**. Every figure it produces therefore carried an
invisible denominator: "elfnote 6.1 summons" over an unknown number of
lines. CLAUDE.md warns that a card whose Lua did not load "replays as
nothing" after a submodule bump — that breakage would have read as a
**smaller sample**, not as a failure, and the `n` it does compute is not
displayed anywhere.

**Measured before claiming: 0 failures over all 271 solved lines** of
`results/boards.jsonl`. So nothing reported was wrong — this is a latent
defect closed. The function now returns `attempted` and `failed` beside
`n`, and `threats` prints a block naming any deck with failures and
pointing at `setup`. It normally prints nothing, which is the right state
for a guard.

(My first attempt to measure this reimplemented the replay outside
`special_summons` and got **271 of 271** failures — `FileNotFoundError`,
because I had not reproduced its workdir setup. That was my harness, not
the code. Instrumenting the real function instead gave 0, and is also the
fix.)

**FIXED — `cmd_drill`'s source loop.** It walked every `results/*.jsonl`
under `except Exception: continue`, so a real error in `drill.export` — a
bug, a malformed record, a vendor change — was indistinguishable from
"this source has no lines for that deck", silently, for every source in
turn. The loop still moves on, because one malformed record should not
stop the search, but the failures are now reported when nothing is found.

**NARROWED — `meta_scrape.same_scrape`.** `except Exception: return None`
around a `SELECT` on `inclusion_history`; the intent is "no such table
yet", which is a `sqlite3.OperationalError`. Narrowed to `sqlite3.Error`,
because `drift` reads None as "cannot tell whether these are the same
scrape" — a refusal indistinguishable from the benign case. Both paths
re-verified (missing labels and missing table both still return None).

**Audited CLEAN, no change:**

- `daemon.py`'s top-level command dispatch — `except Exception:
  traceback.print_exc()`. Correct: the REPL must survive a command
  raising, and it prints the traceback rather than hiding it.
- `tools/check_invariants.py` — comment reads "a broken check is itself a
  finding", and it reports rather than skipping.
- `tools/audit_fields.py` and the `json.JSONDecodeError` handlers — tool
  internals skipping unparseable files, which is the intent.
- `web/jobs.py` and `web/views.py` handlers both bind the exception and
  surface it to the user.

Both fixes mutation-checked.

---

## 56. A cache keyed on `id()` returned the wrong vocabulary — FIXED 2026-10-25

Swept a class not previously covered: per-process and identity
assumptions that break under the 6-worker pool or across runs. Two hits,
one of them a real correctness bug.

**FIXED — `meta_scrape.compile_vocab` keyed its cache on `id(vocab)`.**

```python
key = id(vocab)          # the object's MEMORY ADDRESS
```

CPython reuses an address once the object at it is collected, so a new and
**different** vocabulary allocated there receives a **stale cache hit**.

**Demonstrated, not reasoned about.** Alternating two vocabularies with a
`gc.collect()` between them, it fired on the **third iteration**:
`compile_vocab(["Snake-Eye", "Fiendsmith"])` returned the compiled
patterns for `["Branded", "Despia"]` — a completely unrelated
vocabulary. `match_archetypes` would then match every deck name against
the wrong tag set, and `identify_archetype` feeds `export` and `flex`.

**Live impact was almost certainly nil, and that should be said plainly:**
`fetch_archetype_vocab` returns the same ~600 tags on every call, and each
caller (`discover_meta`, `scrape_archetype`) holds its list for the whole
loop, so the two distinct live vocabularies this needs never existed. But
the failure mode is the silent misidentification of every deck name, the
fix is one line, and "it happens not to be reachable today" is not a
property worth relying on. Keyed on `tuple(vocab)` now. Re-ran the
demonstration against the fix: **0 stale hits in 4000 iterations**, cache
still 2 entries.

**FIXED — `gauntlet` built scratch names from `hash()`.**
`f"{name}_h{h}_{abs(hash(candidate.board_key())) % 10**8}"`, and Python
salts `hash()` of anything containing a `str` per process. Nothing was
computed wrongly — it is only a directory name — but it changed every
run, so a scratch directory could not be matched back to the candidate
that produced it, which is precisely what is wanted when a gauntlet
search misbehaves. Verified across three interpreters: `hash()` gave
**three different values**, `zlib.crc32` one. Now crc32, which is what
`sweep._draw_seed` already uses — there it affected results, and this is
the same lesson applied one module over.

**Audited CLEAN:** no `random.*` call anywhere lacks an explicit seed (the
solver's randomness is its own, passed as `--seed`), and
`_pattern_cache` is the only module-level mutable container in `skills/`
— under a `spawn` pool each worker gets its own copy, so there is no
cross-worker contamination.

Both fixes mutation-checked.

---

## 57. Rounding and tie-break sweep: one latent fix, three audited clean — FIXED 2026-10-25

Two more classes swept, both chosen because they produce a number that
reads as something it is not — the theme of this whole review.

**Class A: a rate that rounds to a false 0% or 100%.** Scanned **3528
stored rate values** across every `results/*.json` and `*.jsonl`, looking
for any nonzero value that displays as `0%` or any sub-1.0 value that
displays as `100%` at the `:.0f` precision the CLI uses.

**Zero of 3528.** Sample sizes protect it: at 40 hands the granularity is
2.5%, so the smallest nonzero rate rounds to 3%, never 0%. Verified rather
than assumed — the assumption was that it was safe, and it is, but only
because `hands` is small. **If `hands` ever rises past ~200 this stops
being true**, and a 1-in-250 rate would print as 0%. Noted here rather
than guarded, since the guard would be dead code today.

**Class B: rankings whose ties resolve arbitrarily.** A tie broken by file
or dict order means two runs of the same input can disagree, which the
paired-comparison method this project rests on cannot tolerate.

**Audited CLEAN:**

- **`optimizer.candidate_pool`** — the highest-stakes one, because it
  ranks by inclusion `pct` and then truncates to 12, so a tie straddling
  the cut would change which candidates get tested. Branded genuinely has
  **3 candidates sharing the pct at the 12th slot**. It is safe anyway:
  `meta_scrape.classify` orders by `pct_lists DESC, c.name`, a full
  tie-break, and Python's sort is stable, so the name ordering survives.
  The determinism lives one layer down in the SQL.
- **`optimizer.removable`** — its docstring records this exact bug being
  fixed before ("it used to take whichever card came first out of a dict,
  which made the single most consequential choice in the search
  arbitrary").
- **`Counter.most_common` elsewhere** — insertion order is deterministic
  for a given input file, so these are reproducible even where arbitrary.

**FIXED — `scenarios.extra_target_from_boards`.** It picked the most
common extra-deck card with `seen.most_common(1)[0]`, breaking a tie by
**whichever card appeared first in the JSONL**. Real ties exist: branded's
top extra-deck card has **2 hits with a three-way tie**.

Latent, and measured as such: 2/31 is 6.5%, which fails the `min_share`
gate of 0.4, and every deck that does clear the gate today has a unique
top card (despia 31 hits, fiendsmith 36, kewl-tune 36, sky-striker 34, all
unique). But the derived target decides what every later solve aims at, so
a target that depends on record order is not a measurement. Tie now broken
by passcode — meaningless but deterministic. Verified stable across 5
shuffled input orders; mutation-checked.

---

## 58. I broke `report` and no guard caught it — FIXED 2026-10-25

**Severity: high, and it was my own regression, live for several commits.**

The survival fix (#49) widened `surv_rows` from a 3-tuple to a 5-tuple
`(row, code, survival, tested, offered)`. A **second, stale unpack site**
further down `cmd_report` still read three values:

```python
for row, code, s in surv_rows:        # ValueError: too many values to unpack
```

So `daemon.py report` — the project's headline command, "everything known
about a deck, in one view" — **raised on every invocation** from the #49
commit until now. The loop was also redundant: #49 already added an
`unmeasured` block that prints never-live cards *with* the count of solved
hands they were absent from. Removed rather than repaired.

**Why nothing caught it, which is the real finding.** `daemon.run` wraps
every command in `except Exception: traceback.print_exc()` — correct for a
REPL, which must survive a bad command — so a broken command prints a
traceback and **the process still exits 0**. The web has
`tests/test_web.py`'s walk over every GET route, which caught two real
500s this week (including one of mine, #52). The CLI had no equivalent, so
a command could be broken by a commit and stay broken silently.

**Fixed, plus the missing guard.** `tests/test_cli.py` walks 17 read-only
commands, calling the `cmd_*` functions **directly** rather than through
the dispatcher — because the dispatcher is precisely what hides the
failure. It runs in 3 seconds. Mutation-checked: re-introducing the stale
unpack fails `test_read_only_command_does_not_raise[report]`.

A second test asserts **every registered command is either walked or
named in the exclusion list**, so a new command cannot quietly arrive with
no smoke coverage. The 17 exclusions each carry a reason: network calls
(`cards`, `meta`, `images`), minutes-to-hours of solver (`solve`,
`ladder`, `gate`, `boards`, `optimize`, `second`, `gauntlet`,
`floodgates`), interactive (`pilot`), or file writes (`import`, `export`,
`snapshot`, `setup`, `threats`).

Walking the other 16 found no further breakage, so #49 and the ~20 other
`daemon.py` edits this session did not damage anything else.

**The lesson about my own work:** I widened a tuple and searched for its
consumers by eye. Two unpack sites existed and I changed one — the same
shape of mistake as #53, where I fixed one copy of the ban-list map and
left the other. Both times a cheap mechanical check would have found it
immediately, and now one exists.

---

## 59. My own #40 fix could 500 the card search — FIXED 2026-10-25

Generalising #58's lesson — I changed a data shape and missed a consumer
— into a sweep of **every** shape and behaviour I changed tonight. One
real problem, created by me, worse than the bug it came from.

#40 made `card_db.limit_for_status` **raise** rather than guess 3 for an
unrecognised ban-list status, because guessing permissively is exactly how
121 forbidden cards became legal. That is right for the authoritative
check: `Deck.validate` catches it and reports the card as unverifiable.

I protected **one of three** call sites.

| caller | before this fix |
|---|---|
| `deck.validate` | catches, reports the card — correct |
| **`builder._card`** | **raises out of `builder.search`** |
| **`optimizer.candidate_pool`** | **raises mid-pool-build** |

Demonstrated on a scratch mirror holding one status of
`"Semi-Forbidden?"`: `validate` returned a clean per-card problem, and
`builder.search` raised `ValueError`. That path feeds `/api/cards`, so a
single odd card would have **500'd the whole deckbuilder search** — not
one row, the entire search. And `candidate_pool` would have died mid-build,
taking an `optimize` run of several hours with it.

So my fix traded a wrong number for a dead feature at two sites. Both now
treat an unreadable status as the **most restrictive** answer — 0 copies,
exclude the candidate — never the permissive one, which is the direction
the original bug got wrong. Verified: the odd card renders `limit=0`
(forbidden badge) and the normal card `limit=3`. The raise remains the
default for anyone who wants it. Mutation-checked.

**The rest of the sweep — every shape I changed tonight, all CLEAN:**

| change | consumers | verdict |
|---|---|---|
| `surv_rows` 3→5-tuple | 2 sites | **was broken (#58)**, now fixed |
| `drill.export` 3→4-tuple | `daemon.py:1917` unpacks 4; web uses `lines_from` | clean |
| `report.deck_report` + `survival_tested` | additive key | clean |
| `sweep.summarize` + `errors` | additive key | clean |
| `special_summons` + `attempted`/`failed` | additive; web spreads `**v` | clean |
| `special_summons.json` + `_source` | **was broken (#52)**, now fixed | fixed |
| `_board_library` new tuple return | both callers written with it | clean |
| `handtrap_matrix` → `survival_from_hands` | same shape returned | clean |

Three of eight shape changes I made tonight broke a consumer (#52, #58,
this one). That rate is the argument for the mechanical checks rather than
reading call sites by eye, and all three were caught by a guard rather
than by me noticing: `test_web.py`'s route walk, the new
`tests/test_cli.py` walk, and this sweep.

---

## 60. The corrections are now pinned to the screen — FIXED 2026-10-25

`tests/test_web.py`'s route walk proves a page answers **200**. It does not
prove the page says the right thing — and every correction this review made
to a user-facing number lives in a template, where a later edit could drop
it without failing anything.

Verified all seven corrections are live on the pages the user actually
opens:

| page | correction | state |
|---|---|---|
| `/matrix` | survival is per HAND with a window (#38) | live |
| `/matrix` | the all-windows standard is named (#38) | live |
| `/report` | plays-through is of LIVE hands, with `live N/M` (#49) | live |
| `/report` | never-live reads "unmeasured", not 0% (#49) | live |
| `/threats` | which board library the summon figures came from (#50, #52) | live |
| `/threats` | that single-draw understates them (#52) | live |
| `/build` | section caps are the server's constants (#54) | live |

Then pinned each as a parametrised test. Mutation-checked two ways:
reverting the matrix caption to "Of the windows where the" fails the
denominator case, and changing `/threats`' "Derived from" fails the
provenance case — so the guards bite on the specific wording that carries
the meaning, not on the page merely rendering.

**One false alarm of my own, worth recording because it nearly cost
time.** My first ad-hoc check reported `/build`'s caps as MISSING. The
pattern was `const CAP = \{"main"`, and Jinja's `tojson` **sorts keys**, so
the page renders `{"extra": [0, 15], "main": [40, 60], ...}`. The page was
correct and my regex was wrong — the unit test that parses that object as
JSON had been passing all along. I checked the rendered output before
"fixing" anything, which is the only reason nothing was broken chasing it.
The committed pattern now matches the key rather than the position.

287 tests. This closes the loop the review has been running on: a number is
only fixed once the thing the user reads says what it means, and a test
says so.

---

## 61. A 3-list archetype outranked a 94-list one — FIXED 2026-10-25

**Severity: high for `optimize` and `flex`**, and it was mis-labelling the
active deck.

Found while quantifying something else: `identify_archetype` called the
active deck **Primite** at overlap **1.0**, ahead of **Branded** at
**0.60**. The ranking is on overlap alone, and:

| archetype | core | overlap | built from | cards matched |
|---|---|---|---|---|
| Primite | 3 cards | **1.00** | **3 scraped lists** | 3 |
| Branded | 5 cards | 0.60 | **94 scraped lists** | 3 |

**Both matched three cards absolutely.** Primite won because its core is
smaller, and its core is smaller because at three lists any card present
in all three reads as 100% "core". The deck genuinely runs all three
Primite cards, so this was a **sample-size** error, not a phantom match —
which is why it looked plausible.

**It is not cosmetic.** `optimize`, `flex` and `report` take
`guesses[0][0]` programmatically. With `arch="Primite"`, `removable`
ranked the deck's cards against a 3-list inclusion table in which **7 of
12 candidates had no entry at all**, so the ordering of the single most
consequential choice in the hill climb — which card to cut — was
arbitrary. That is the exact bug `removable`'s own docstring records
having been fixed once before.

Fixed with the same floor logic the function already applies to thin
cores (`len(cards) < 3`): an archetype whose core rests on fewer than
`MIN_LISTS_FOR_ID = 10` lists cannot identify a deck. Reasoning and
precedent are `MIN_LISTS_FOR_DRIFT = 15`. The tuple also now carries
`n_lists`, because the docstring makes judging the caller's job and the
caller previously had **no way** to — it is appended as a 4th element,
additive, because seven consumers read `[0]`, `[1]` and `[2]` by index
(the #58 lesson, applied deliberately this time; a test pins that
compatibility).

After: the active deck identifies as **Branded, overlap 0.60, 5-card
core, 94 lists.** Mutation-checked.

### A limitation this surfaced, worth stating plainly

Re-running `removable` against the correct archetype still leaves **14 of
22 cut candidates with no meta inclusion figure**, because the deck is a
Blue-Eyes / Primite / Branded hybrid and Branded's 94 lists contain none
of its Blue-Eyes engine.

So for this deck specifically, `optimize` has little to offer: its
candidate pool is Branded's flex slots, which do not cover the half of
the list that makes it a Blue-Eyes deck, and `removable`'s ordering is
uninformative across most of it. That is consistent with the measured
result already on record — **48 distinct swaps across 3 cut slots, none
beating the current list**. The honest reading is not "the list is
optimal" but "the meta data cannot evaluate this list", and the two look
identical in the output.

---

# Bug-fix pass, 2026-10-26

Six defects, found by running commands across every deck and reading the
output rather than by reading code — the method this file keeps
recommending. All fixed, each with a regression test. 332 tests pass; the
four guards are clean.

### 62. The test suite ran itself recursively, and a broken import was the only brake — FIXED

`venv/bin/pytest tests/ -q`, the command BACKLOG.md and the
`test_regressions.py` docstring both give, aborted at collection:
`No module named 'daemon'`. Only `python -m pytest` worked, because that
form puts the cwd on `sys.path`. `test_regressions.py` and `test_web.py`
each did the insert themselves; `test_cli.py` did not, so one missing line
aborted the whole run. Fixed with `tests/conftest.py`.

Fixing it uncovered what it had been hiding: `cmd_doctor`'s GUARDS block
shells out to `venv/bin/pytest tests/ -q`, and `test_cli.py` calls
`cmd_doctor`. doctor → pytest → doctor → pytest. The inner run had always
died instantly on the ImportError, so the recursion stopped one level deep
and doctor simply reported **FAIL regression tests** — its own headline
guard, failing, for as long as the tests have existed. With collection
fixed the loop ran: the suite hung past 600 s and the 15-minute load
average reached 13.8 before the timeout killed the tree. `cmd_doctor` now
skips the guard that IS pytest when `PYTEST_CURRENT_TEST` is set.

### 63. `draws` printed the opposite of its own data — FIXED

The command exists to answer one question per deck: are extra draws worth
2.6x the solver time. `at_draw[k]` counted only the hands that still had an
attempt at draw k, so hands that stop early (patience) left the denominator
instead of carrying forward as finished. branded read
`draw 0 40/40 ... draw 13 31/31`, and `cmd_draws` compared the **rendered
strings** `"40/40"` and `"31/31"`, found them unequal, and concluded *later
draws kept improving boards, so draws is earning its budget on this deck.*

Recomputed cumulatively from the same records: all 31 of branded's solved
hands reached their eventual best board in the breadth round, and 553 of
its 655 solves (84%) bought nothing. With a moving denominator the two
strings almost never match, so the "earning its budget" branch was close to
unconditional.

Two more defects inflating the same count: only `interruptions` was
compared, so a 3 int / 1 neg → 3 int / 2 neg gain counted as having peaked
at draw 0; and hands that never solved have best (0, 0), which equals their
running best at every draw, so all 9 of branded's unsolved hands of 40
counted as "already at their best board". Now excluded and reported
separately, mean labelled per SOLVED hand.

The corrected verdicts reproduce the independent sign test in CLAUDE.md:
despia and elfnote better, branded no different.

### 64. `rescore` described edits that were never tested, and counted 12 of them twice — FIXED

Candidates were named by parsing the trial FILENAME. Trial names gained the
cut partway through the project — `<deck>_r<round>_<add>` became
`<deck>_r<round>_<cut>_<add>` — so the older half of every stored run has no
cut in its name and rendered as a bare `+Called by the Grave`: an addition
with nothing removed, which at 45 main cards is not a legal list and was
never what ran.

The decklists settle it. `blue-eyes-branded-test_r0_24224830.ydk` and
`..._r0_30576089_24224830.ydk` are byte-identical and both cut 30576089.
All 12 old-style trials are the same swap as a new-style one, summaries
matching to four decimals — the solver being deterministic, so this is two
names for one experiment. The table showed them as two independent agreeing
rows. 49 rows → 37.

`optimizer.trial_change` now takes the cut and add from the trial's own
`.ydk`; `_merge_duplicate_lists` collapses identical decklists and keeps
disagreeing ones as flagged separate rows.

### 65. The #1 ranking guard went into `optimize` and not into `rescore` — FIXED

Both commands print an ordering from the same stored summaries under the
same objective. Only one checked for degenerate terms. On the stored
kewl-tune run — the run #1 measured — brick rate (0.17), solve rate (0.83)
and handtrap survival (0.00) are each one value across all 10 trials, so
the order rests entirely on combo length at weight -0.05 and window
recovery at 0.5. Its top row beat the current list by 0.006 and `rescore`
closed by recommending it.

`degenerate_terms` now accepts plain summaries as well as Candidates, so
there is one implementation rather than two. `rescore` refuses on the same
`len(varying) <= 1` threshold and otherwise names the constant terms above
the table.

**Not changed, because it redefines the rule rather than enforcing it:**
the threshold counts any 2 moving terms as enough, so kewl-tune discloses
but does not refuse — even though the three HEAVY terms (-2.0, +1.0, +1.5)
are the dead ones and only the tiebreaks move. Worth a decision.

### 66. `report` crashed on 7 of 12 decks — FIXED

The headline command was only ever walked with no arguments, i.e. only
against the active deck, which happened to miss both crashes in it.

**ValueError on 6 decks.** `identify_archetype` gained a fourth field and
`cmd_report` still unpacked three:

    for alt, ov, n_ in (r.get("archetype_alts") or [])

so every deck with an alternative archetype guess raised. The entry
directly above this one added that field and argued it was safe because
"seven consumers read `[0]`, `[1]` and `[2]` by index (the #58 lesson,
applied deliberately this time; a test pins that compatibility)". The test
pinned the consumers that index and missed the one that **unpacks** — which
is #58 recurring in the same function, two entries after it was declared
learned. The fix indexes; the docstring, which had said three fields for as
long as there were four, is corrected.

**UnboundLocalError on Blitzclique.** `blind` was bound inside `if live:`
but read from the UNMEASURED loop, so a deck with no live handtrap and at
least one unmeasured one crashed.

**The blind-spot note printed twice or never.** It sat inside the
unmeasured loop: branded printed the Maxx "C" note once per unmeasured row,
and the six decks whose handtraps were all live printed it zero times — so
the note explaining that a 94%-converted survival is not a pass never
appeared on any deck whose numbers most invited that misreading.

The dispatcher catches all three, prints a traceback and exits 0, which is
how they survived. An AST scan for the same scoping shape across `skills/`,
`daemon.py` and `web/` found no other instance.

### 67. `report` named one card as the worst matchup when three tied — FIXED

branded folds to Infinite Impermanence, Ash Blossom and Effect Veiler, all
at 0%. `min` returned whichever the stored record listed first — a card
chosen by JSON key order, printed as the answer. Ties are named as ties.

### Audited clean in the same pass

- Every read-only deck-taking CLI command (`mydeck`, `hand`, `check`,
  `ydke`, `report`, `drill`, `rescore`, `flex`, `drift`) against all 12
  deck files: 108 invocations, 0 raised.
- Every web route, static and per-deck, for all 12 decks: 80 GETs, no 5xx.
- `hand`'s hypergeometric odds against a hand calculation: 31.0% / 36.3%
  for 3 copies in 44 cards at 5 and 6 drawn, both exact.

## 68. The recovery metric either aborted or overstated itself — FIXED 2026-10-26

`handlab.analyse` asks "where does this line break" by firing each hand trap at every
legal window and seeing whether the board comes back. Two defects in how it asked,
both proven against the solver on a stored kewl-tune line.

**A zoned `--fire-spare` spec aborted the whole fire test.** The spare came straight off
the goal conjunction, and those targets carry a zone — the base rung is things like
`94145021@hand`. `--fire-spare` resolves through the solver's `ResolveCard`, which has no
zone grammar (`--target` uses `ResolveCardZone`; they are different functions), and on
failure `RunFireTest` **returns before the first window**:

| `--fire-spare` | windows | verdict |
|---|---|---|
| none | 4 | 3 of 4 converted, 3 full board |
| `15665977` | 4 | 4 of 4 converted, 0 full board |
| `15665977@atk` | **0** | **none** |

Every hand trap then reported "no legal window in this line" — which reads as the safest
possible line, not as a crash. `spare_spec` strips the zone.

**Offering a spare at all costs more than it buys.** Same line, same budget:

- no spare: three windows `CONVERTIE (board COMPLET)` at `best 5/5`, and the fourth
  graded `best 4/5, manque : Zalen the Shackled Dragon`
- one spare: all four `CONVERTED (board WITHOUT the sacrificed card(s))` at `best 4/5`,
  including the three that had just rebuilt the whole board

`converted` went up and the report said less. The spare is now opt-in (`spare=`),
reversing the earlier default here.

**The replacement was free and already in the log.** The solver prints `best 4/5` beside
every window and `_ROLLOUTS` has always captured the pair — nothing read it, so a window
keeping four of five board cards and one keeping none both printed "NO RECOVERY FOUND".
`parse_windows` keeps `kept`/`of`, `recovery_grade` sums them over the windows that
graded, and the CLI and the web hand lab both show the share. `report`'s per-window
column comes from the funnel, which is binary and never sees the grade; it is now
labelled as the floor of it rather than a rival reading.

Also: `analyse` shadowed its own `chosen` parameter with the chosen goal, and indexed
`attempts[blocking["chosen"]]` without checking — multi-draw records carry no `chosen`
key at all.

## 69. `gauntlet` ranks boards again, on measured data — FIXED 2026-10-26

The going-second ranking cannot work on this solver (#4). The same question is
answerable with machinery that does: a board is a set of interruptions, and `--fire`
already measures over **forty hands per cell** whether a deck's own turn-1 line still
reaches its payoff with a given card played at every legal window.
`results/handtrap_matrix_40h.jsonl` holds 7 decks x 7 cards, so ranking a board costs no
solver time at all.

Per piece, P(their line gets through it) is `1 - window_rate x (1 - survival)` — the
same expectation `weighted_handtrap_ranking` scores a maining decision on, so a card
that is never live stops nothing. Per board it is the product, weighted by `locals.toml`.

Three limits the command states on every run, because each is a way to misread it:

- `--fire` plays a card from the opponent's **hand**, so only pieces the board holds in
  hand are measured. A field negate is not something anyone can activate from hand and
  no flag on this solver plays one, so those pieces are counted and **named**, never
  folded in with a weight.
- the matrix holds only `scenarios.HANDTRAPS`, so a held piece outside it is eligible
  but unscored, and is listed apart from the measured ones.
- the per-board figure multiplies its pieces, which assumes independence. A hand trap
  landing shortens the line the next one faces, so it is a **lower bound** on the board.

First run (kewl-tune, `random:7`): Ash in hand lets 0% of branded and despia through and
71% of sky-striker. All three candidate boards scored identically, because they differ
only in field pieces — and the command says so instead of printing an ordering. That is
the honest state of this method: it separates boards by what they **hold**, not by what
they **built**. Extending it to field pieces needs a different primitive; `--no-activate`
(forbid activating a card of theirs) is the candidate and is not built.

## 70. Bug-test pass on the Stage 9 games — 8 defects FIXED 2026-11-02

Found by reading `skills/game.py` and its web/CLI surface end to end, probing every
games route with the test client, and an isolated live run (imported vs snake_eye,
3 hands x 2 enemy hands, 20 s, scratch DB). Every win in that run was replayed to a
player-0 win, and every recorded damage equals the opponent's LP drop.

1. **Rooted solver: `--goal-win` never kept a best approach when the goal card was
   already out.** The turn-3 target is the trainer's goal (e.g. a set Trivikarma),
   often still on the field from turn 1, so target overlap was maxed at the root,
   `best_path` stayed empty, no `best_approach` was written and the finisher got no
   seed. Games recorded `0 damage` with no line. At equal overlap `--goal-win` now
   prefers lower opponent LP, then fewer opponent monsters (`Search::WinProgress`,
   `Duel::Lp`, `BetterApproach` at all five merge sites; patch 0002 regenerated,
   rebuild byte-identical). The same stored position went from no line to **lethal
   found**; the live run's three non-wins now carry 7000 / 2800 / 500. Gate B still
   PASS, and its one-short cases now write an approach at 1 LP.
2. **`POST /games/<x>/start` and `/train/<x>/start` queued jobs for decks that do not
   exist.** `deck_path` returns a Path for any safe name and a Path is truthy. The
   worker ran the job and marked it done.
3. **Stopping `train <deck> ... <enemy>` still started hours of games.** `train()`
   clears the stop flag before returning, so the `stop_requested` check after it was
   always false. Games now start only at a plateau.
4. **A hand 0-for-2 with 28 games still queued showed as DUD.** Tiers now wait for a
   hand's games to complete (expected count read off the stored games by default).
5. **Games are keyed by the enemy's file name and `export` rewrites it in place**, so a
   re-export would silently mix two lists. `run` now records the list's hash and
   refuses to resume against a different one.
6. **One crashed game ended the whole multi-hour run.** It is now reported, left
   unrecorded and retried by the next run.
7. **A duel won on the enemy's turn was a KeyError** (`turn2` returned no board); it is
   now a win. `games <deck> <enemy> ten` raised ValueError; counts are validated.
8. **Games page:** no-turn-1 squares linked to a 404; the hours estimate ignored the
   solve budget and the kill check; "trained" counted a different set of hands than
   the run plays.

Also: the two `/report` copy checks that had been carried as known failures were a
test bug — bare `/report` follows the active deck, and `imported` has no matrix rows.
Pinned to kewl-tune; the suite is fully green (361).

## 71. The defender wiped its own board at the first window — FIXED 2026-11-11 (patch 0004)

Asked to make the AI play smarter. Read the 4800 games of 2026-11-09 first: going first,
19% won and 60% LOST ON THEIR TURN 2 — at the same rate with 1 or 4 pieces on our board,
and in 1866/2370 games our board stopped nothing. Traced h0/e1000: the enemy's first
action was a hand effect onto an empty field; the greedy defender fired S:P Little Knight
there, and the "first legal choice" follow-up banished our own Lars and S:P.

Three defects, one patch (`patches/0004-smart-defender.patch`, rooted binary only):

1. **Greedy timing and targeting.** `--opp-smart` answers in `--opp-respond` order (hand
   traps, negates, the rest) and picks follow-up cards by value; `--opp-hold` keeps board
   pieces until the attacker controls a monster (`--opp-hold-until n`).
2. **`MSG_SELECT_CARD` dedup made prompts unanswerable.** "Select 2" among two identical
   copies left a pool of one, no subset was emitted, the branch was killed: 688 times in a
   5 s search, all on the defender's prompt — the attacker's rollouts died for no game
   reason, which flattered the defender. Below the minimum, copies stay distinct. Dead
   ends 135 → 0 on the same search.
3. **No approach at zero overlap.** A turn that never touched its target wrote no line, so
   19% of going-second games were `no_t1`. At equal overlap the approach now prefers more
   own monsters; the h0/e1006 enemy hand (two Ash, Fuwalos, two bricks) summons Ash and passes.

Proven on all 237 stored going-first positions (`tools/experiments/defender_bench.py`,
enemy's 20 s kill search, games' seed): greedy lost 142 (60%, the stored games' own rate),
new binary with greedy flags 142 (1 flip each way = noise), smart 128, smart + hold **90
(38%)**: 65 saved, 13 newly lost, 77 lost under both. `game.DEFENDER = "smart"` is the
games' default; `game.require` refuses a rooted binary without `--opp-smart`.

A fixed script is readable by the attacker's rollouts (trace h0/e1009: one bait
activation for Lars, then effect-less summons into lethal), so `game.DEFENCES` tries a
family per turn (`--opp-hold-until` 1 and 2): hold-until-2 alone lost 106/237, but lost
under both scripts 67 (28%); a third script (until 3) only reached 63. Each script is one
more randomized kill search for the attacker to miss in, so the family's number is
slightly flattered (noise floor: 1 flip in 237 per arm).

## 72. Turn 1 never got to play the trainer's lines, and half the enemy's hand traps never fired — FIXED 2026-11-13

Found by asking why our turn 1 reached its trained goal in only 39 of 237 going-first
positions vs Angelechy-Meta. `tools/experiments/t1_fired.py` replays every stored turn 1
and lists what the enemy fired: in 148 of them NOTHING, yet only 29 of those reached the
goal. Two defects, both fixed:

- **The game played a different duel from the trainer's.** The game forced the hand onto
  the .ydk file order with the enemy seed's core randomness; the trainer solved on the
  hand seed's shuffle and its own core seed. 23 of 24 best lines break on the file order,
  14 of 24 on the right order with another seed, 0 of 24 on both. `trainer.hand_start` +
  `game.ordered_deck` + `breaking.template_vs(core_seed=)` now give turn 1 (and your deck
  going second) the trainer's exact duel.
- **The defender's list was `scenarios.HANDTRAPS` cut to the deck**: Ash, Droll and Ghost
  Ogre for Angelechy-Meta, so its three Mulcharmy Fuwalos and three Purulia never fired.
  `game.respond_codes` reads every main-deck interruption from the scripts.

Not the fix, measured (`tools/experiments/t1_bench.py`, 40 positions where the old games
saw nothing fired and missed the goal, paired): with the trainer's duel AND the defender
now firing Mulcharmies and Dominus traps, 60 s reaches the goal 2/40; warm-starting the
policy from the best line (`--adapt`) 4/40; replaying the best line first as far as the
enemy lets it (`patches/0005-approach-line.patch`, UNAPPLIED) 5/40, +5 -2 against plain,
pieces 9 more / 9 fewer -- noise. A traced case: the line holds 70 of 148 answers, then
a card our Pilgrim Reaper milled gives them a trigger and the game has changed. Our turn
1 now loses to real interruptions, which is the point of the matchup test.
