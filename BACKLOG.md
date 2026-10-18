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

## NOW

- [ ] Keep adding a regression test whenever a bug is fixed. `venv/bin/pytest tests/ -q`
      runs in under a second; there is no excuse for skipping it.

- [ ] IN FLIGHT: 40-hand board library rebuild (`results/boards40.log`). Pick up the
      result, load it, and re-derive targets from the bigger sample.
- [ ] Nibiru count and Maxx "C" cost. NOT nearly free as STAGE3 claimed — the solver
      prints summon counts to stdout but nothing lands in the JSONL (no 'summon' key
      anywhere in a record). Exact counts need the line replayed through ygorl and
      summon actions counted; a proxy from the end board would undercount materials
      sent to GY, and a plausible-but-wrong number is the failure mode this project
      keeps hitting. Do it properly when CPU is free.
- [ ] Re-scrape on a later day and `snapshot` again — drift needs a second data point.
      Baseline `baseline-2026-10-18` is stored (5599 rows).

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
