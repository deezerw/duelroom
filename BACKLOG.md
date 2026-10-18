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

- [ ] Run `gate Blitzclique` end-to-end now that targets resolve, to confirm the whole
      user-deck path works. Expect weak numbers — its target is flagged trivial.
- [ ] Bug sweep: grep for the patterns that already bit us —
      `.get(` on a `sqlite3.Row`, metrics that count a missing window as a success,
      any place a list is treated as alternatives when it is a conjunction.
- [ ] Hand-level memoization `(deck_hash, opening_hand) -> result`. Named in CLAUDE.md
      as a non-negotiable and never actually implemented. Biggest speed win available.
- [ ] Scale up: gate/boards/matrix at 40+ hands now that long jobs are allowed. The 6-12
      hand numbers currently in `results/` are too noisy to act on.
- [ ] Nibiru count and Maxx "C" cost — the solver already tracks special summons per
      line, so these are nearly free and were promised in STAGE3.
- [ ] Meta drift: diff inclusion rates across scrapes to surface rising tech cards.
- [ ] EDOPro `.yrpX` export for a deck's best lines, so they can be drilled.
- [ ] Push to GitHub (user approved).

## BLOCKED / WON'T DO

- **Handtrap pairs** (Ash + Imperm together). `--fire` takes one card per solver
  invocation and funnel_eval loops them separately. Not expressible; documented in
  CLAUDE.md.
- **Board breaking via `--board-remove`.** It relaxes the goal rather than demanding
  destruction. Replaced by the going-second formulation in `breaking.py`.

## DONE

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
