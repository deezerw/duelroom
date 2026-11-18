# Stage 11 — Pick the turn-1 board that survives (learned, measured)

Goal (user, 2026-11-17): use the gaming PC (`duelpc`, RTX 5070 Ti 16 GB, driver
595.91.07) on the days nobody is home. Priority order: **accurate and helpful
information first, then speed.** Delete this file when every step is done and
verified.

## Rules (they outrank any step below)

1. **Every reported number is engine-played.** Win rates, killed-on-turn-2,
   card impact, what-you-stopped: always real games. A model may choose what
   the engine plays (which board, which move first), never stand in for a
   result.
2. **A model ships only through a paired A/B** like `t1_backup_bench.py`:
   same positions, same seeds, both arms through the real turn-2 kill check,
   sign test on the positions. Held-out hands only (split by hand, never by
   game: games of one hand are near-copies).
3. **Report intervals, not points.** `stats.crossed_bootstrap` resamples your
   hands and their hands. Measured 2026-11-17: 2400 games going first = 26%
   with an honest 95% of 15-39% (Wilson on the pooled games says 24-28%).
4. **Simple before neural.** Every model step first fits a gradient-boosted
   baseline; the network is kept only if it beats it on the held-out A/B.

## Why this stage (measured)

- Our turn 1 reaches its trained goal 14/240 vs Angelechy-Meta: something
  fires in 236 of 240 (Part 2.3).
- The goal is the wrong objective, not just hard: aiming at its interruptions
  only reaches 122/240 with more pieces and LOSES MORE (73% vs 69%, p=0.009).
- A board value learned from the stored games was no better than counting
  (Part 2.2) because there is ONE board per hand. The missing data is many
  boards per hand against fixed enemy hands.

## Steps

0. ~~Prerequisites~~ — **done 2026-11-17**: NVIDIA driver on duelpc (signed
   prebuilt `linux-modules-nvidia-595-server-open`, Secure Boot stays on);
   games jobs requeue and resume when their device goes away (EXIT_DOWN 75,
   lane retries every 5 min) instead of ending "done"; matchup and per-hand
   intervals on `/games`.
1. **Away-day runs you can leave alone.** *Mostly done 2026-11-17:* the /jobs
   queue per machine with left/time from measured games (c0f91ec); heat log +
   pause at 3 min over 89 C CPU / 85 C GPU (612629b; measured 75 C with 8
   solvers); borderline pairings on /games (7c15b38); PyTorch 2.11 cu128 in
   `~/duelroom-ml` on duelpc (GPU checked). Left: the GPU lane (built with the
   first GPU job, step 3) and the .ps1 test (needs the user at Windows).
   - A queue view: what will run on duelpc, in order, with an estimate from
     measured throughput (t1_backup: 1440 kill checks in 76 min).
   - Thermal log per job (k10temp, GPU temp/power every minute into the job
     log); the job lowers its workers if the CPU passes 90 C or the GPU 83 C.
   - A GPU lane on duelpc beside the games lane (one job each); duelpc games
     drop to 7 workers while a GPU job runs. Measure the X3D cache cost
     first: solver states/s with and without training running.
   - Test `tools/duelpc/reboot-to-duelpc.ps1` once (untested so far).
2. **Board data.** `tools/ml/board_data.py` built + smoke-tested (one
   position: 12 turn 1s -> 10 distinct boards, labels mixed); full run waits
   for the plateau budgets. For each trained hand x enemy hand (going first): K
   distinct end boards (K ~ 10-20) from turn-1 solves that vary the seed and
   the goal (full goal, interruptions only, goal minus one card, ...), each
   through the real kill check (`--goal-win`, OTK_MS, DEFENCES). Store
   board, how it was made, kill-check result per defence. Resumable, on
   duelpc. Start with seeds + goal variants (no solver patch); patch the
   solver to dump distinct end states only if those boards are too alike.
3. **Board model.** `tools/ml/board_model.py` (offline pick scoring, GBM
   baseline) written; the GPU network comes after the GBM numbers. Inputs: our end board, hand, GY, banished (ygorl's
   encoder, `ygorl.env.encoding`: 160 cards x 23 features) and the enemy
   deck. Target: survives their turn 2. Within-hand evaluation (Spearman,
   as board_value.py), held-out hands. GBM baseline on the server/CPU first,
   then a small set-transformer on the GPU (PyTorch cu128, its own venv on
   duelpc: the server never imports torch).
4. **Paired A/B of picking by the model.** Arm A: the board the games play
   today. Arm B: turn 1 makes K candidates, the model picks one. Both through
   the real kill check; report lost-on-turn-2 and wins by turn 3 with
   intervals, and the extra turn-1 time B costs. **Ask the user before
   making B the games' turn 1** (design change).
1b. **Budget plateau + spread games** -- `tools/experiments/budget_plateau.py`,
   full run on duelpc started 2026-11-17 (36 positions a side, 2 seeds) (see Decided below) before step 2, so
   the board data is made with converged searches.
5. **Re-test the deck** vs the meta with the adopted turn 1, intervals shown.

## Decided (user, 2026-11-17): play as well as possible, then spread the games

The user plays real matches from this data, so the solver's decisions must be
as close to optimal as the compute allows; within that, the games may change.

- **Play quality first: the budget plateau.** On a fixed sample of pairings
  (both sides, all three searches: our turn 1, their turn-2 kill check, our
  turn 3 / going-second break), run 1x, 2x, 4x the current budget (SOLVE_MS
  60 s, OTK_MS 20 s), paired seeds. A search whose result still moves from
  2x to 4x is not converged: raise it until it stops. Our side and their
  side both count -- a lazy enemy flatters your deck just as much.
- **Then spread the games.** Replays of one pairing are near-copies
  (resampling hands only gives 16-38%, enemy hands only 22-32%); RUNS goes
  10 -> 2, and the freed compute buys more enemy hands and more trained
  hands. Two replays stay as a consistency check: a pairing whose two runs
  disagree is marked "borderline" on /games (the line is there, the solver
  only finds it sometimes -- a real opponent may or may not).
- Old 10-run games stay readable but are never pooled with the new ones
  (same rule as devices).
