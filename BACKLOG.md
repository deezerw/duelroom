# BACKLOG

Work queue for the autonomous loop. **Read this first every iteration.**

## Standing decisions (from the user, 2026-10-18 — do not re-ask)

- They want to **create and use their own deck** with the program. Deck authoring is
  the headline feature.
- `locals.toml` is auto-filled from scraped global meta shares.
- Allowed unsupervised: long CPU jobs, `pip install` into the venv, bumping vendored
  submodules (re-run the gate after), pushing to GitHub.
- Priority order: **bug hunting → bigger samples → new features → speed**.

## Loop rules

1. Pick the top unblocked item. Keep it small enough to finish and commit in one turn.
2. Verify before claiming. Run the thing; read the output critically. The bug rate in
   this project has been high and almost every bug was found by *looking at output that
   seemed wrong*, not by reading code.
3. Commit with a message that says what was wrong and how it was proven.
4. Move the item to DONE with a one-line result, then end the turn.
5. Long CPU jobs: launch with `nohup ... &` and pick up the result next iteration.
   Never block a whole turn waiting.
6. If an item turns out to be a bad idea, say so in DONE and move on. Don't force it.

## NOW -- the plan (user, 2026-11-25: "iron it out then begin")

Ordered. User decisions behind it (do not re-ask): no real-match log (too much hassle);
one enemy deck is fine while the app is in development; **making the app as good as
possible comes first**; compute hours are not a concern; `imported` is the deck the user
plays in real life, so it is the target deck for every measurement. The STAGE files hold
the big steps; this list orders them with the small ideas.

### Phase 0 -- server, small
- [x] Re-scrape + `snapshot` 2026-11-25 (129 new lists; `drift Angelechy 2026-10-18 2026-11-25`
      works, but only 7 lists on one side: noise until more are scraped).
- [x] `export` best-placing list -- already done (meta_scrape.best_list). Add the
      tiebreak: among equal placings, the list closest to the archetype's core. DONE 4a15836.
- [x] Optimizer fires Imperm AND Ash -- the trial records carry `"fire": [10045474,
      14558127]` (2026-10-24). Re-check on the next real optimize run.

### Phase 1 -- one solver round, ONE METHOD bump (old era relabelled, hidden, never deleted)
Every METHOD bump makes stored games "old data", so these land together, and duelpc's
2026-11-25c `day.sh` queue waits for them (it would go stale at once).
- [x] DONE 0d22697 (live): Port patch 0003's arena half (uffd page tracker, 1.6x states measured) to the PINNED binary
      (trainer, sweep, every turn-1 solve). Built: `tools/build_pinned_uffd.sh` ->
      build/combo-solver-uffd (arena.cpp/.h hunks only; tracker off = the pinned search).
      Gate: `tools/experiments/pinned_uffd_identity.py` (pinned / uffd-off / uffd-on, byte-
      identical lines at a fixed rollout count, then states at the trainer's wall budget).
      Then engine.SOLVER_BIN -> it and R2V_ARENA_UFFD=1 in engine.env().
- [ ] `R2V_ARENA_VERIFY=1` "barrier INCOMPLETE: page 1154" (+2050: `48 00 00 40`, mirror
      0): one 4-byte write, same in BOTH trackers, so it does not block the port (the
      identity gate proves the port). A write the mirror never saw; if that page is later
      marked and restored, the value reverts to 0. Find what lives there (gdb watchpoint).
- [x] Repeatable `--fire`: DROPPED (user, 2026-11-25). The games' defender fires every hand
      trap it holds and STAGE13's side.py runs on the games; the old one-card pinned `--fire`
      path (optimizer, handtrap matrix) stays one card.
- [x] `--no-activate` for THEIR card: not needed -- the rooted defender plays only what its
      `--opp-respond` lists, so dropping a field piece from that list switches it off.
- [ ] **Patch 0017 `--opp-on-activation`** (user, 2026-11-26: "using hand traps at stupid times,
      look at veiler"): Veiler / Impermanence answer only the searcher's monster effect activated
      on the field and target that monster. BUILT (`build/combo-solver-0017`, in
      build_rooted_solver.sh after 0016), NOT live: goes live with this round's METHOD bump.
      Measured rooted at duel 8's empty-chain window: old binary Veilered there in 16/16 lines,
      0017 in 0/16. The duel's own Python defender has the rule live already.
- [ ] **Hidden-hand option** (user, 2026-11-25: a visible / hidden hand button; chose FULL).
      Today every search sees the other side's real hand -- even duel mode, whose copy is
      the real hand PLUS one of each hand trap (patch 0010). Hidden, both sides, games and
      duel mode: (1) solver flag `--opp-hand-hide` (patch 0016): at the root the opponent's
      real hand is replaced by the assumed one (one of each hand trap of their list);
      (2) game.py: each turn plans on that copy, the plan is played on the REAL duel, and a
      deviation by the real defender re-plans from there (duel.py's plan/follow loop);
      (3) stored under its own label, never pooled with visible; button on /games, duel
      mode hidden by default. Expect ~2-4x a game's time.
      Progress: (1) DONE 5a71336, patch 0016 built to combo-solver-0016, NOT live (goes
      live with the METHOD bump; visible games do not pass the flag, so it changes
      nothing for them). Next, (2): `game.play_hidden(template, responses, player, turn,
      search_args)` -- a DuelSession on the real duel (duel.Live's shape, from a replay
      instead of a DB row); at the searcher's prompts follow the plan (duel._plan_step:
      steps over the copy's extra windows); at the defender's, duel.defend on the REAL
      hand; no plan / off the plan -> re-plan from the current real position
      (breaking.split_position, `--opp-hand-hide --opp-hand-root <their list's hand
      traps> --opp-set-root` + `--reseed`, so it knows neither hand nor draws; cap
      MAX_REPLANS, then close the turn passively). A kill / win is then the REAL playout,
      not a verdict, so `majority` is not needed in hidden mode. Wire turn1 / turn2 /
      turn3 / the break through it when `hidden`; a `hidden` label beside device; the
      button on /games and duel mode (plan_args gains --opp-hand-hide).
- [ ] QUICK_RESPOND on for Havnis / Kashtira, measured: `tools/experiments/quick_respond_ab.py`
      (game.kill_check at OTK_X, paired off/on, 102 stored going-first positions hold one;
      ~18-54 solver-hours: duelpc day).
- [ ] **"Better than me" (user, 2026-11-26: "i dont want it to just copy my boards i want it to
      be better than me"; chose all three):** (a) `game.RESPOND_ALL` (ce0a0d6, OFF): every card
      of the deck may answer the opponent's turn -- Mind Shuffle, Perlereino's destroy and the
      Tearlaments' GY Fusions were never listed. A/B `respond_all_ab.py`. (b) `game.T1_PICK_ALWAYS`
      (7efac41, OFF, needs T1_FALLBACKS > 0): every turn 1 picked by the look-ahead kill check,
      candidates + breadth shapes (`game.t1_candidates`). A/B `t1_starters.py ... always`.
      (c) LIVE (8954742): each duel you play going first compares your turn 1 with the solver's on
      the same hand through the kill check (duel page + tally). Runs: results/experiments/
      better_than_me{,2}.log (Angelechy, then a going-first Blue-Eyes smoke batch: no going-first
      Blue-Eyes positions existed). (a)/(b) go live with this round's METHOD bump if they help.
- [ ] Rebuild both binaries, regression tests, bump METHOD, relabel 2026-11-25c.

### Phase 2 -- calibration (duelpc days)
- [ ] Run `day.sh` on the new METHOD (going-first baseline, starters A/B, t3 plateau,
      side gate, board data) -- plus one hidden-hand sample of the going-first baseline,
      so the size of the "attacker sees your hand" effect is known.
- [ ] A DRAWS axis on the existing `ladder` (draws 1..4 per deck, quality per CPU).
- [ ] Budgets in rollouts (reproducible runs) off the same ladder runs; lands with the
      NEXT METHOD bump, not a bump of its own.
- [ ] More enemy hands: 10 trained enemy hands is a small pool for side advice; train
      20 (`train Angelechy-Meta --vs imported`, ~8 h on the server) and check intervals
      narrow.

### Phase 3 -- re-measure
- [ ] Re-run the 40-hand board library with each deck's DRAWS.
- [ ] Extend `rank_boards` to field pieces (uses `--no-activate`).

### Phase 4 -- stages, in parallel
- [ ] STAGE16 (AlphaZero): next is arm (b), the net prior inside the C++ NRPA
      (step 3(b)) -- the net found 2x the far kills in less time (2026-11-21).
- [ ] STAGE13 side cards: needs repeatable `--fire` and the honest side gate passed.
      When `side.py` covers sides: ASK the user before deleting `second`,
      `gauntlet.rank` and the old side plan (unsupported on the pinned binary).
- [ ] STAGE15 / STAGE11 step 4: pick our turn-1 board by THEIR turn-2 kill check
      (measured 80% -> 90% surviving at 3x turn-1 cost). Re-measure under the new METHOD
      first: on 2026-11-19 only 4 of 1063 boards survived a 16x kill check.

### Pinned (user, 2026-11-26: "lets pin that for now") -- duel AI: smarter AND less think time
- [ ] (3) Measure which of the AI turn's kill searches find kills (plain lethal vs "lethal
      using answer X") on the 31 stored positions; if the answer variants add nothing, give
      their cores to extra plain-lethal seeds (randomized search: more seeds = kill sooner).
- [ ] (2) Ponder: after each of your moves, plan "your turn ends here" in the background,
      dropped on your next move. Measure first how long you pause before ending a turn.
- [ ] (1) = STAGE16 arm (b), the net prior inside the C++ NRPA (2x far kills in less time).

### Phase 5 -- low
- [ ] Untried solver levers: `--options` macros, a per-starter `--adapt` corpus,
      `--ctx-shrink` (`--adapt` already measured useless at equal time).
- [ ] Patch-stack upkeep: 15 patches on vendored combo-solver. A `tools/patches_check.sh`
      that applies 0002..N to a clean upstream checkout and builds, run before any
      upstream bump, so the stack's state is known rather than discovered.

### Always
- [ ] A regression test with every bug fix (`venv/bin/pytest tests/ -q`).

## BLOCKED / WON'T DO

- **Hand-level memoization.** Measured at 18.8% apparent hit rate, all of it from
  re-running an identical gate — i.e. re-validation after a component bump, where a
  cached answer would hide the very change being checked. A correct key needs budget,
  target AND engine revisions, which drops it to ~zero. The optimizer gains nothing
  either: a one-card swap changes the shuffle, so no hand is shared. CLAUDE.md's
  "non-negotiable" claim was wrong and has been corrected.

- **Handtrap pairs** (Ash + Imperm together). `--fire` takes one card per solver
  invocation and funnel_eval loops them separately. Not expressible; documented in
  CLAUDE.md.
- **Board breaking via `--board-remove`.** It relaxes the goal rather than demanding
  destruction. Replaced by the going-second formulation in `breaking.py`.

- **Crash at the end of `optimize`, fixed.** My degeneracy edit left `rows` used
  before assignment, so the command died *after* the full run with
  UnboundLocalError and swallowed the result table. The checkpoint ledger added
  earlier meant nothing was actually lost — the table was rebuilt from
  `results/optimize_kewl-tune.jsonl`. Test added that the assignment precedes the use.
- **FIRST REAL OPTIMIZER RESULT (kewl-tune, 12 hands, Imperm+Ash fired): no swap beats
  the base list.** All 10 candidates scored below base and every one shared an
  identical 83% solve, 17% brick and 0% survival. The honest reading is that these
  flex slots are interchangeable at this sample size, not that base is proven best.
- **The optimizer's objective is degenerate for kewl-tune.** Reading the ledger: all
  10 trials scored an identical 10/12 solved, 17% brick and 0% survival. Only the
  combo-length term varied, so the whole 0.06 score spread came from a tiebreak with
  weight -0.05. `degenerate_terms` now reports which terms were flat and the command
  says the ranking is not evidence. Three of four terms flat is not a bug in the
  objective so much as a true statement about this deck at this sample size — but
  presenting the resulting order as a recommendation would have been.
- **`engine.duel` parameters verified.** It is used by nothing today, so it was
  unverified surface a bot-vs-bot stage would rest on. seed, first, agent_a/agent_b
  and max_turns each demonstrably change the result. `max_turns` is consistently OFF
  BY ONE against the obvious reading — the game stops as turn N+1 begins, so
  max_turns=1 gives two turns — which is upstream's contract, now documented and
  pinned by a test so a change upstream is noticed.
- **Corrected a claim I made twice.** I wrote — when killing memoization, and again
  in CLAUDE.md — that a one-card swap changes every dealt hand. Measured over 80
  (candidate, seed) pairs it leaves the hand UNCHANGED 55% of the time. The
  memoization conclusion survives but for a different reason: the hands coincide, the
  solves do not, because the decks differ by a card and the solver searches the deck.
  The same fact is good news for the optimizer — most trial hands are shared with the
  base, so its paired comparison is tighter than shuffle luck would allow.
- **The fire-list fix was itself a silent no-op.** It wrote `fire` into the per-trial
  targets file — and funnel_eval's `load_deck_targets` returns only `v["targets"]`,
  discarding every other key. Trials kept firing Ash alone while the file claimed
  otherwise, and the test I wrote asserted the FILE contents so it could not catch it.
  Fire now goes on the command line, `evaluate` raises if a run fired fewer handtraps
  than requested, and the test asserts the call rather than the file.
- **Checkpointing**: `hill_climb` writes every finished trial to
  `results/optimize_<deck>.jsonl` as it goes. Killing a multi-hour run — which I did,
  losing 16 trials — otherwise loses everything.
- **Optimizer was optimising for the wrong handtrap.** Its objective weights handtrap
  survival most heavily (+1.5), but `evaluate` wrote a per-trial targets file with no
  `fire` key, so funnel_eval fell back to its default — Ash Blossom alone. The 40-hand
  matrix had just put Infinite Impermanence well ahead (93.4% vs 74.1%), and kewl-tune
  plays through Imperm 0% of the time, so the search was tuning for resilience to the
  second-best card while the best one was what actually beat the deck.
  `threat_handtraps()` now reads the measured ranking and fires the top two.
- **Maxx "C" cost measured** — the other half of the STAGE3 promise, free once summons
  were counted: Maxx "C" draws on every special summon, so the replayed count IS the
  cost. fiendsmith hands over 8.4 cards comboing through it (15 at worst), kewl-tune
  2.5. The survival table shows kewl-tune "surviving" Maxx "C" 76% of the time, which
  reads as fine; the cost column is the question that can see what it actually does.
  BLIND_SPOTS now points at the measurement rather than just declaring the gap.
- **Real special-summon counts, and they CLOSE the Nibiru question.** Replayed every
  stored line through the core counting MSG_SPSUMMONED — cheap, since the line is
  known and no search is involved. fiendsmith 8.4/line, elfnote 6.1, kewl-tune 2.5,
  branded 0.6 (the Maxx "C" shortcut had claimed 6.8 for branded against 1.0 actions).
  The share of lines reaching five summons correlates with observed Nibiru windows at
  **r=0.936** — kewl-tune clears five on 5% of lines and got 8 windows, fiendsmith on
  100% and got 245. So Nibiru's low window count was always correct and the
  "contradiction" I detected two iterations ago never existed. The retraction was
  right, and this is positive evidence rather than absence of it.
- **40-hand handtrap matrix landed and promoted** (7/7 decks, 49 min). Unlike the
  board library, these conclusions DID shift: Infinite Impermanence overtakes Ash for
  first (93.4% vs 74.1%) and Effect Veiler overtakes Ghost Ogre for third. The
  difference is effective sample size — a board-library hand yields one data point,
  while a matrix cell only counts hands that SOLVED and then had a legal window, so
  12 hands left several cells resting on 8-11 windows. The 12-hand file is archived
  under results/archive/ rather than deleted.
- **Mutation list extended to 15** — every bug tonight that could be expressed as a
  code reversal. Three more real gaps found and closed: the handtrap ranking's
  `exclude` argument did nothing testable, word-boundary tag matching was untested
  (the "Ra" inside "Dragon" bug that once put a non-archetype second in the meta),
  and nothing checked that recent set databases get merged. One survivor turned out
  to be a BAD MUTATION rather than a gap: dropping only the left word boundary still
  blocks "Ra" in "Dragon" because the right boundary sees the following "g". All 15
  now killed.
- **`tools/mutation_test.py`** — reverses nine bugs this project actually had and
  checks the suite goes red for each. A green suite is not evidence; the only evidence
  is that it notices when the code is wrong. Found one genuine gap on the first run:
  dropping the hypergeometric bounds checks left every stats test green, yet without
  them "odds of opening 4 copies of a 3-of" raises ValueError instead of returning 0.
  All nine now killed.
- **Hill-climb control flow unit-tested** against a stubbed evaluator (no solver, no
  engine). A real optimize run is hours, so a control-flow bug is expensive to find
  by running it. Covers: the best candidate winning over a later one that also beats
  base, every trial being recorded, and a strictly worse swap being rejected.
  NOTE: the first version of this test was worthless — the fixture ordered candidates
  so the best was also the LAST to beat base, making "best wins" and "last wins"
  agree. Mutation testing caught that; the fixture now puts the best first.
- **`bootstrap.sh`** — the repo on GitHub carries source and decklists only, so a
  fresh clone could not run a single solver command; the engine needed seven setup
  steps that existed solely in a docstring. The script encodes all of them, with each
  gotcha documented where it bites: `--recursive` submodules (ygopro-core nests Lua as
  its own), upstream's conservative Python pin, `libsqlite3-dev`, and the merged card
  database. `--check` reports state without changing anything. Prerequisite detection
  verified by running it with the compilers absent.
- **Bare-command audit.** Ran every command with no arguments. `gate`, `solve`,
  `ladder`, `floodgates`, `second` and `optimize` all began minutes-to-hours of solver
  work immediately, with nothing said about cost or about a sweep already running —
  the audit itself started solver processes. They now warn when solvers are already
  busy (joining a saturated machine slows both jobs). `flex` also falls back to the
  active deck's archetype like `report` and `optimize`.
- **Optimizer: archetype optional, and the CUT is no longer arbitrary.** `optimize`
  still demanded an archetype that `report` had stopped needing. More seriously, the
  hill climb evaluates one removal per candidate (6 slots x 30 candidates at minutes
  each is an overnight job), and it was cutting whichever card came first out of a
  dict — making the single most consequential choice in the search arbitrary. Now
  cuts the least-played non-core card and says which, so the search is over what to
  ADD with a defensible removal.
- **Archetype auto-detection** (`meta_scrape.identify_archetype`). Walking the cold
  first-run path showed `report` needed an archetype argument nobody importing their
  own deck would know to pass. It is inferable: match against each archetype's CORE
  cards (>=90% inclusion) rather than its whole pool, since the core is what makes a
  deck that deck while flex slots are shared staples that match everything. Correctly
  identifies all four test decks, and reports secondary matches for hybrids.
- **Provenance on every measured number.** `report` printed a solve rate with no
  indication of which run or budget produced it, and a budget too low invents bricks
  (10s calls a hand dead that 60s solves in 9 actions). It now names the source file,
  hand count and budget, and warns when the budget is under the 60s default. The
  Blitzclique 0/2 now carries three caveats — trivial target, low budget, tiny sample.
- **`doctor`** — one command running every self-check: engine readiness and pinned
  versions, row counts for each table, all four guards, and the weakest dataset in the
  project. Reports what is MISSING as loudly as what is broken, since an empty
  database passes every check while being useless.
- **Partial-sweep reads caught.** `doctor` reported "40 hands/deck" from a file a
  running job was still writing — 1 of 7 decks. `matrix`, `threats` and `doctor` now
  print how many decks a file actually contains and say plainly when it looks partial.
- **Doc rot audited and mechanized.** CLAUDE.md documented 3 commands that never
  existed (`goldfish`, `scenarios`) and omitted 26 that do — the command list was
  still the one from the original plan. Regenerated from the CLI banner, and
  `tools/check_docs.py` now fails if a doc names a command the dispatch table lacks
  (mutation-tested). Also corrected the Maxx "C"/Droll limitation, which still
  asserted the pre-`survives` 0% figures, and flagged `trials.special_summons` as a
  column nothing populates.
- **Silent no-op in my own workflow.** The 40-hand matrix launched an hour earlier
  exited in 0.0s having done nothing: funnel_eval resumes by SKIPPING decks already
  present in the output file, and it was pointed at the 12-hand results. It reported
  "0 decks" and success, and I had reported it as running. Output paths now carry the
  hand count so sample sizes cannot share a file, and run_matrix raises if a sweep
  gains no records. Verified running before claiming it this time.
- **`tools/check_invariants.py`** — every wrong answer this project produced was caught
  either by a number that could not be true or by two outputs disagreeing, so both are
  now mechanical. 7 invariants over the stored data: counts summing, rates matching
  their own numerator, survival in range and never exceeding offered windows, the
  retracted summon metric staying retracted, Wilson intervals containing their point,
  board frequencies summing to 1, and every derived target being a card its deck can
  actually produce. Mutation-tested.
- **40-hand board library landed and promoted to canonical** (271/280 solved, 83 min).
  The 12-hand conclusions HELD: mean interruptions moved by at most 0.5 (sky-striker
  3.33 -> 2.85), intervals tightened from [76-100] to [91-100], and branded's drop
  from 92% to 78% sits inside its own old interval. 6 of 7 derived targets unchanged;
  only branded moved (Mirrorjade -> Rindbrumm), the deck with the most variable lines.
  193 distinct boards, 304 trials.
- **RETRACTED: the "Nibiru contradiction" was my own error.** It rested on reading
  Maxx "C" windows as special summons. They are RESPONSE windows — Maxx "C" has no
  activation condition, so the solver offers it nearly everywhere; the count
  correlates with line length at r=0.937 and branded showed 1.0 actions against 6.8
  "summons", which is impossible. With the premise gone there was no contradiction:
  kewl-tune's 4-action line may simply never make five special summons, so zero
  Nibiru windows is plausible. Detector, ranking exclusion and the 32.6% figure all
  removed; Nibiru is back to 13.7%.
- **BLIND_SPOTS re-examined on evidence.** Maxx "C" stays (856/908 windows convert, so
  lines continue through it and the cards surrendered are invisible to a
  "did the line finish" metric). **Droll removed** — only 45/85 convert and 19 hands
  died to it, so the metric does capture it; it was only labelled unmeasurable because
  the buggy `status` reading scored it a spurious 0%.
- **`response_windows`** (renamed from the bogus summon count). Measures the
  interruption surface of a line — how often the opponent gets to respond — which is
  real and useful under the correct name. elfnote 22.6 windows/line, branded 6.8.
  Nibiru/Maxx "C" summon counts remain genuinely unavailable without replaying lines.
- **SELF-CORRECTION: my no_window fix read the wrong field.** `status` on a fire entry
  reports whether the SEARCH succeeded, not whether the line lived — an entry can read
  status "solved" with windows 6, converted 3, survives False. I used status, which
  flipped Infinite Impermanence from a correct 0% to a wrong 100%, and `report` and
  `matrix` then disagreed about the same deck. Now uses the authoritative `survives`
  boolean while keeping the no_window exclusion. The result agrees with upstream on 5
  of 7 cards and differs on exactly the two where upstream is wrong (Nibiru: 0 windows,
  upstream calls it 100% survival; Ghost Ogre: upstream counts 2 no-window hands as
  survived). Caught only because two commands printed different numbers for one deck.
- **Direction markers**: `threats` and `matrix` now tag your active deck "(yours)" and
  state which way is good, since the same number inverts depending on whose deck it is.
- **Sign audit.** The same survival number means opposite things in two commands:
  `matrix` reports OPPONENT decks where low is good for you, `report` reports YOUR
  deck where low means you fold. `report` gave no direction, so a reader coming from
  `matrix` would invert it. Now labelled "higher is better — this is YOUR deck",
  ranked worst-first with verdicts, and it names your worst matchup card. Surfaced
  that kewl-tune plays through Infinite Impermanence 0% of the time.
- **Blind-spot cards no longer labelled "fine".** The first version of that fix called
  Maxx "C" at 80% survival "fine"; the metric only asks whether a line reached its
  target, which Maxx "C" never prevents. `scenarios.BLIND_SPOTS` names them and report
  prints NOT MEASURABLE with the reason.
- **Regression suite** (`tests/test_regressions.py`, 19 tests, 0.11s). Covers the bugs
  actually found here: hypergeometric identities, ydke/ydk round-trips, list_hash
  multiplicity, target ranking (extra-deck over frequency, built over dealt, one target
  because a list is a conjunction), trivial_targets on saved specs, write_targets
  merging, no_window not counted as survival, a never-live handtrap scoring zero,
  hybrid weight splitting, best-not-last hill climbing, and 1-for-1 siding.
  **Mutation-tested**: reintroducing three of the fixed bugs failed exactly the three
  corresponding tests.
- **Meta drift machinery** (`snapshot` / `drift`). Drift needs TIME: slicing the one
  scrape by date gives 25 decks in the last 30 days against 485 in 120, i.e. 2-5 lists
  per archetype, which measures noise. So `snapshot` freezes inclusion rates under a
  label and `drift` diffs two, refusing to report a comparison with under 15 lists on
  either side. Baseline captured.
- **Pushed to GitHub**: https://github.com/deezerw/duelroom (PRIVATE, 36 commits).
  Secret-scanned before pushing; venv/vendor/data/results all excluded, so only the
  33 source files went up. Added a README whose "read the numbers carefully" section
  lists what the tool reports that is weaker than it looks.
- **Bug sweep #2 (systematic).** Added `tools/audit_fields.py`: compares every record
  field the parser modules read against every field that actually exists in stored
  records. Three bugs tonight had that exact shape and none of them raised. Codebase
  is clean on it now; re-run after adding any record parser.
- **`trivial_targets` half-worked on saved data** — it read `_zone_rank`, which is
  stripped before the targets file is written, so the zone check defaulted to "fine"
  and could never fire on a spec read back off disk. Now derives the zone from the
  target string itself.

- **`drill [deck] [n]`** — exports a deck's best lines as EDOPro `.yrpX` plus a text
  card-by-card summary (opening hand, end board). Verified the written replay parses
  back and replays cleanly in the rules core. Caught a silent no-op while writing it:
  the per-line `interruptions` field in the JSONL is always None (counts live in the
  companion summary keyed by hand), so "best board first" was scoring every line 0 and
  returning file order while claiming to be sorted.
- **`report [deck] [archetype]`** — one view: legality, engine loadability, the solver
  target (with its warning if flagged), measured solve rate with a Wilson interval, a
  small-sample caveat under 20 hands, opening odds, and how the list compares to the
  scraped meta. Assembles only what exists and says plainly what is missing rather
  than filling gaps with plausible numbers.
- **Bug sweep #1 (silent no-ops).** `.get()`-on-Row and None-arithmetic are clean; the
  real find was four `subprocess.run` calls in breaking.py with `capture_output` and
  no `check`. A non-zero solver exit produced no .yrp, which `going_second` reported
  as "they bricked" — a crashed solver and a deck that cannot combo were
  indistinguishable, and board-break and floodgate rates would have absorbed solver
  failures as deck weakness. Added `SolverFailed` + `_run_solver`; proved with a bad
  `--target` (now surfaces "!! --target: no card" instead of a false brick) and
  regression-checked the happy path.
- **Blitzclique has no payoff board — settled, not a bug.** 16 hands: every solver
  proposal stayed main-deck, and only 1 of 16 end boards held any monster at all.
  `solve_blocking` plans towards INTERRUPTIONS, which for a backrow deck are all
  main-deck cards, so no amount of extra hands can produce an extra-deck target.
  Added `extra_target_from_boards` as a second source (reads what the deck actually
  ends on; honestly returns None here) and `ensure_target` prefers it when the planner
  only offers main-deck cards.
- **gate now warns before it measures**, so a flagged deck no longer reports a
  confident "0/12 (0%)" as if it were evidence the deck is bad.
- **Output ordering fixed**: our prints were block-buffered while solver subprocesses
  write unbuffered to the same fd, so a warning printed BEFORE a long solve appeared
  after its results. stdout is line-buffered now.
- **Crash fixed**: `survival_given_line` is None when nothing solved; gate crashed
  formatting `None * 100`. Prints "n/a (no hand solved)".
- **Targets must be EXTRA-deck** — the strongest signal, now the top ranking key. A
  main-deck one-of has to be DRAWN, so targeting it measures draw luck: Blitzclique
  derived "Called by the Grave" and gated 0/12 while the same deck solved 6/6 under
  per-hand automatic targets. Six of seven working meta targets were already extra
  deck; the exception (elfnote on main-deck Ars Magna) was quietly suspect and is now
  fixed to Mediclius.
- **write_targets MERGES** instead of rebuilding. It was deleting every on-demand user
  deck target on a meta refresh — import a list, spend minutes deriving its target,
  run `targets`, lose it silently.
- **Target quality** — derived targets are now ranked by SUBSTANCE (zone: a monster you
  assembled > a card you set > a card in hand), then by how often the deck reaches
  them. Frequency, not line length, is the right second key: line length is only
  available per record so it cannot discriminate within a hand, and ordering by it
  picked generic extra-deck monsters (S:P Little Knight, F.A. Dawn Dragster) over each
  deck's own boss. All seven meta decks now derive their archetype payoff —
  Mirrorjade, Desirae, Kewl Tune RS, Red-Eyes Dark Dragoon, Sky Striker Ace = Zero.
  `trivial_targets` flags decks whose best target is not worth measuring.
- **On-demand targets** (`scenarios.ensure_target`): a freshly imported deck had no
  target, so gate/optimize/second/floodgates died with a bare CalledProcessError that
  read like a crash. Now derives one via solve_blocking on first use and caches it.
  `target_for` falls back to ygorl's own curated file (its ten test decks) and
  `merged_targets_file` combines both, since funnel_eval takes one --targets path and
  refuses any deck missing from it — without the merge, fixing user decks would have
  broken `gate snake_eye`.
- **locals.toml auto-fill** (`daemon.py locals auto [rogue%]`). Each DECK contributes
  one unit of weight split across its tags: raw tag counts would sum to 166% of the
  deck count (1.66 tags/deck, one list carries five) and inflate hybrid-heavy
  archetypes. 25% reserved for rogue since the scrape only sees decks that TOPPED
  something. Coverage 55% -> 58%; the handtrap ranking is unchanged under the new
  weights, so that conclusion was not an artifact of the placeholders.
- Stages 1-5 all implemented (see CLAUDE.md and git log).
- **Deck authoring** (`skills/importer.py`): import from .ydk path, `ydke://` URI, or
  YGOPRODeck URL; `mydeck` sets an active deck that every deck-taking command falls
  back to when called bare; `ydke` exports for pasting elsewhere. YDKE round-trips
  exactly. Deck names come from the page title, not the URL slug -- YGOPRODeck resolves
  by the trailing numeric id and ignores the slug, so /deck/kewl-tune-734601 served a
  Blitzclique list and would have been saved under the wrong name.
