# Stage 5 — Optimizer

**Rewritten after Stage 2.** ygorl ships deck tuning and MAP-Elites evolution
(`tools/tune_deck.py`, `tools/evolve_decks.py`, pyribs, a synergy graph, a surrogate
model). **Both tuners require `--checkpoint`: a trained policy network.** We do not have
one and cannot train one — the published agents took 32 GPUs for days and this box is
CPU-only with no discrete GPU.

So the tuner path is closed, and the plan reverts to what it always was: hill climb the
flex slots, judged by the checkpoint-free solver metrics from Stages 3 and 4.

This is fine. It may even be better — the solver is a stronger pilot on turn 1 than any
policy we could have trained, and turn 1 is what decides modern Yu-Gi-Oh.

## Why this is affordable

Stage 1's inclusion analysis already cut the problem down. You are not searching 40
cards against a 13,000-card pool — you are searching ~8 flex slots against about 30
cards real players run. Three orders of magnitude off the search space, and the only
reason this stage is cheap.

## Built — `skills/optimizer.py`, `daemon.py optimize`

**Status (2026-10-18): built and exercised.** On kewl-tune it finds 5 non-core slots
(Imperm x3, Ghost Ogre, Pot of Prosperity, Droll) and 8 candidates drawn from real
lists. Swaps validate as legal 40-card decks and the objective orders correctly.

Three bugs were found by running it rather than reading it, all of which would have
surfaced only mid-run: a key mismatch (`pct_lists` vs `pct`), `card_db.resolve`
returning a `sqlite3.Row` with no `.get()`, and -- the one that would have killed every
trial -- funnel_eval keying targets by deck NAME, so generated trial names had no
target. Each trial now writes its own targets file pointing at the BASE deck's payoff,
which also keeps the comparison honest: the question is whether a swap reaches the same
board more often, not whether it reaches an easier one.

### Candidate pool
- Flex and fringe cards from `inclusion` for your archetype
- Plus cards matching your deck's mechanical requirements (type / attribute / level
  overlap with existing extenders)
- Minus anything Stage 3's per-card liveliness flags as a garnet
- Filter by banlist legality (`deck.validate`)

### Main deck search
Hill climb over 1–3 card swaps:

1. Propose a swap within the flex slots
2. Re-run Stage 3 consistency and the Stage 4 scenarios
3. Accept if the weighted objective improves
4. Repeat until no single swap improves

**Paired seeds are mandatory and already built in** — `funnel_eval` uses the same
shuffle permutations across decks by construction. Use `--max-rollouts` so runs are
reproducible rather than load-dependent.

Objective, weighted and configurable:
- Real brick rate (minimize)
- Solve rate and mean interruptions (maximize)
- Handtrap survival, weighted by `locals.toml`
- Board-break rate, weighted by board frequency and `locals.toml`
- Floodgate resilience

### Side deck search
Same loop, different objective. Per archetype in the gauntlet:
- Which candidates most raise the going-second break rate against that archetype's
  board distribution?
- What is the minimum swap set, and does the deck still function after it?
- Output a per-matchup plan: cards in, cards out.

15 slots over ~8 matchups is small. Greedy then local search is plenty.

### Worth stealing from ygorl even without a checkpoint
- `tools/build_synergy_graph.py` — card co-occurrence from real lists (upstream reports
  0.935 recall against the real meta). A better candidate generator than text filtering.
- `funnel_eval`'s verdict logic — `--max-brick-rate` / `--min-survival` early stopping
  kills bad candidates before spending a full budget on them.

## Deliverables

1. Ranked main deck swaps with measured deltas and confidence intervals.
2. Per-matchup side deck plan with measured break-rate improvement.
3. A diff against the scraped consensus list — where your build departs from the meta,
   and whether the numbers justify it.

## Gotchas

- **Confidence intervals on every delta.** A 1-point gain inside a 4-point CI is not a
  gain, and hill climbing on noise converges to garbage. `stats.wilson` exists.
- Budget: at ~20 s/hand, each candidate evaluation at 100 hands is ~35 minutes on 6
  workers. The flex-slot narrowing and early stopping are what make this finish.
- Hill climbing finds local optima. Several random restarts, then compare.
- Hold out a few archetypes, optimize on the rest, check the result holds on the
  held-out set. Otherwise you fit the board library rather than the game.
- When results look strange, suspect `locals.toml` and the objective weights before the
  search.
- Re-run after every banlist: the flex-slot analysis shifts and everything rests on it.

---
Delete this file when the stage is complete and verified.
