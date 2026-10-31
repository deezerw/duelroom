# Stage 7 — The deck trainer: play over and over, and get better

Delete this file once the stage is complete and verified.

## Goal

Pick a deck, start the trainer, leave it overnight. It deals hands, plays them, keeps
what worked, and uses that to play the next hands better. You can **see** it get better
on a learning curve measured against a fixed set of benchmark hands.

## Why this is not RL, and why that is fine

ygorl's own CPU measurements: PPO self-play for 1 h did not beat random, a further 3 h
added nothing, and on a combo deck it "basically learned nothing". The end-of-game
reward has to travel back through about 1,000 decisions. This box is CPU-only.

What does learn on a CPU is already inside the combo solver, unused by ygorl or
duelroom:

- `--adapt <dir>`: a corpus of this deck's solved lines, as `.yrpX`, is replayed and
  adapted into the solver's NRPA move policy **before the first rollout**. Moves are
  identified by the cards they engage, so lines from other hands transfer.
- `--options <n>`: mines recurring multi-step chunks (macros) out of the corpus and
  offers each as one sampling unit, so the solver plays a known sub-combo as a single
  move.

That is expert iteration: search finds lines, the corpus remembers them, the next
search starts from what the corpus learned. The "model" is the corpus. It persists,
grows and can be inspected.

## Gate: measure before building

A/B on held-out hands, same targets, same budget: arm `base` vs arm
`--adapt <corpus>`. **Build the loop only if `adapt` solves more hands or solves them
faster.** If it doesn't, write the numbers down here and redesign.

## The loop (one "generation")

1. **Deal** N fresh hands (new seeds each generation, never benchmark seeds).
2. **Play** each with `solve_for_hand` + `--adapt corpus/<deck>/` (+ `--options` if it
   earns its place in the A/B).
3. **Keep** the solved lines worth learning from, ranked by board quality
   (interruptions on field first, as `drill.lines_from` does). The corpus is capped and
   curated: best K lines per distinct opening, so a thousand copies of one easy line
   do not swamp it.
4. **Benchmark** every G generations: the fixed benchmark hands at a fixed, SHORT
   budget, measured with and without the corpus. The short budget is deliberate,
   because that is where false bricks live and where learning shows.
5. **Record** a row per generation: corpus size, benchmark solve rate,
   time-to-solve, mean interruptions on field. That is the learning curve.

## Where it lives

- `skills/trainer.py`: corpus management, the generation loop, the benchmark.
- `corpus/<deck>/*.yrpX` (gitignored) + a table `trainer_runs` / `trainer_gens`.
- `daemon.py train <deck> [--hours H]`, and a web job `train` with a page showing the
  learning curve and the corpus. Every corpus line gets a **Watch** button (Stage 6),
  so you can see what it learned.
- Resumable: a generation is the unit of checkpointing. Killing the job loses at most
  one generation.

## Honest limits to state on the page

- It learns **your turn**: openings and combo lines, with and without hand-trap
  interference. Grind games and the opponent's turn are not modelled (that is the
  separate full-duel bot, and it is blocked on the turn-2 resume problem).
- The learning curve is over the benchmark hands, at the benchmark budget. Report the
  hand count and an interval with it, per web/PLAN.md.

## Verified when

- The A/B gate result is written above with its numbers.
- A two-generation run on one deck completes, resumes after a kill, and its curve
  page shows both generations.
