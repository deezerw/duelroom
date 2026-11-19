# Stage 16 — An AlphaZero-style player (learned policy + value, search on the real engine)

Goal (user, 2026-11-19): "lets try the alphazero thing". Not MuZero: we have a perfect
simulator (ocgcore), so only policy and value are learned; the engine stays the rules.
Delete this file when every step is done and verified.

## Rules (from Stage 11, unchanged)

1. Every reported number is engine-played. The net chooses moves; it never stands in for a result.
2. A step ships only through a paired A/B against today's solver on the same positions and
   seeds, held-out hands, with intervals. Each step must beat the one before.
3. Simple before neural; and every step must pay on its own (the user is not buying a promise
   of step 5).
4. **The net learns card effectiveness itself** (user, 2026-11-19): its training targets are
   game outcomes and search visit counts, never the user's card values / interrupter prefs
   (trainer `card_values`, `trainer_prefs`) or the Angelechy `szone` values Claude set. The
   scripts (solver, trainer) may keep using those. Imitation of solver lines -- whose goals were
   chosen with those values -- is only a warm start that self-play must replace; the shipped
   net is judged on wins. It must also produce its OWN card rankings as an output: the learned
   value of positions with vs without a card, and paired in/out swaps played by the net.

## What already exists (vendor/ygorl, M4)

ygorl has most of the machinery: `ygorl.nets` PolicyNet (card/effect encoder, board
Transformer, event history, a scoring head over the LEGAL options of each prompt), a value
critic + Q head (`docs/training.md`), PPO self-play, behaviour cloning from solver lines
(`ygorl.train.bc`, `docs/bc.md`: 86.7% line reproduction from 60 demos), belief heads that
predict the opponent's hidden hand (`docs/belief-heads.md`), a C++ vectorised environment,
snapshot/restore and `fork(replay, t)` branching, paired-seed arena. What it lacks is OUR data
(thousands of converged solver lines, both sides, labelled by real kill checks) and a search
that uses the net.

## Steps

0. **Environment.** ygorl's `train` extra (PyTorch) on duelpc next to `~/duelroom-ml`
   (cu128, 5070 Ti checked 2026-11-17); the server keeps no torch. Check: one BC epoch runs on
   the GPU from a 10-line demo set; an arena game with `policy:PATH` plays on the server CPU.
1. **Demonstrations from what we already played.** Convert every stored line into ygorl's
   demo format (action index per decision, replay-verified): trainer best lines (4 decks x 24
   hands), the games' turn 1s, turn-2 kill lines and approaches (the ENEMY's side: 4000 kill
   checks re-read today), going-second breaks at 8x, board_data's 1000+ boards. Both seats, so
   one net learns our deck and Angelechy. Split by hand (never by game). Report: samples per
   side and turn, decisions per line, how many lines fail to replay (must be ~0).
2. **Policy by imitation (BC) on the GPU.** `ygorl.train.bc` on step 1's data. Measure on
   held-out hands: step accuracy, line reproduction (does the greedy net reach the line's
   board), and board quality of the net's own turn 1 (trainer score). Baseline: ygorl's 60-demo
   BC. Cheap: minutes on the 5070 Ti.
3. **The net as a prior in the search.** Two arms, measured against today's solver at EQUAL
   wall time on the plateau positions:
   (a) MCTS/PUCT in Python over ygorl's DuelSession with snapshots (AlphaZero proper; measure
       states/s first -- one turn is ~150 decisions);
   (b) the net's policy as the starting weights of the C++ solver's NRPA rollouts (a solver
       patch; Stage 12's "move prior"), batched GPU inference across the 8 solver threads.
   Keep whichever finds wins/boards faster. Success = the same converged result in less time
   (cheaper 16x kill checks and 8x breaks), or a better result at the same time.
4. **Value net.** Train the critic on outcome labels only: kill-check outcome + effort (time
   to the first win, damage taken -- Stage 15's rich labels), game outcomes, the 16x rechecks.
   (No card-value features: the board is the input, the outcome the target.) Use:
   cut lost rollouts early, judge turn-1 boards (Stage 11's board model done properly).
   Measured as within-position ranking on held-out hands, then in the search (A/B).
5. **Self-play loop (AlphaZero).** Search + net play both sides; the net trains on the
   search's visit counts (policy) and the outcomes (value); a new net replaces the old only if
   it wins a paired arena AND does not lose the solver A/B on the games harness. The enemy
   searches its defensive replies the same way (replaces the scripted defender: the strong
   enemy the user asked for). Hidden information: the searcher samples the opponent's hand from
   the belief heads instead of seeing it (the "omniscient attacker" caveat).

## Compute

GPU: training only (duelpc 5070 Ti). CPU: the engine and the search, everywhere -- still the
bottleneck. duelpc runs Mon-Wed days, plus nights if the 2026-11-19 trial is fine for the user
(RGB off, screens off). The server keeps running the games harness at night.

## Order with the rest

Steps 0-2 can start now (little compute). Step 3 onwards waits until the honest baselines
(16x kill / 8x break games) are in, because the A/B needs them.

## Progress

- **2026-11-19, step 0 done:** ygorl (the server's built package, Python 3.12) copied into
  duelpc's `~/duelroom-ml` with numpy/ribs/nashpy; imports, loads 14924 cards, sees the 5070 Ti.
- **2026-11-19, step 1 done:** `tools/ml/demos.py`: 4748 lines -> demonstrations, 0 rejected
  (kill 2354, break 1870, board_kill 524; ~1.05M decisions before filtering).
- **2026-11-19, step 2 built:** `tools/ml/bc_train.py` (turn player only, repeats dropped,
  held out by deck+hand). Smoke test, 60 lines / 2 epochs: held-out 43% vs 32% uniform. The
  full run is first in duelpc's 3 AM queue (2026-11-20, tools/duelpc/day.sh) -> results/ml/bc/.
- **2026-11-19, step 3 groundwork:** Python stepping speed on the server (one core, niced,
  beside a games job): replaying a line 5,684 decisions/s; snapshot + restore + 30 random
  steps 4,853 decisions/s (6.2 ms per rollout). The C++ solver logs ~11k states/s on 2 threads:
  same order. AlphaZero-style MCTS expands one node per simulation (value net, no rollout to
  the end): 800 simulations a decision ~0.15 s of engine per decision + batched net calls, so
  arm (a) is viable; arm (b) (net prior inside the C++ NRPA) stays the faster-to-adopt option.
- **2026-11-19, step 3(a) search built:** `skills/mcts.py` -- PUCT over the real engine
  (snapshots), prior/value as plug-in functions (uniform / outcome-only until the net exists).
  With no net, 400 simulations find a stored kill from 4 and 8 decisions before its end, not
  from 16 or 30 (the tree is too wide without a prior: the net's job). Test:
  test_mcts_finds_a_known_kill_near_its_end.
- **2026-11-19, net prior wired:** `skills/net_prior.py` (duelpc only: torch) gives the search a
  ygorl checkpoint's softmax as its prior; the search carries a copy of the event history down
  each path (card db / vocab / encoder shared). Plumbing check with the 60-line smoke-test net:
  the stored kill is found from 16 decisions out (uniform prior: not), 1.8 s, 278 net calls --
  but that line may be in the smoke test's training sample, so not yet evidence.
- **2026-11-19, step 4 data:** `tools/ml/value_data.py` -> results/ml/value/positions.jsonl,
  positions at the start of their turn 2 labelled by HARDNESS (from the enemy's side: +1 killed
  at once ... 0 ... -1 untouched; the defender keeps its best defence), never mixing kill
  budgets. Now: 200 board_data positions at 80 s (99% lost, median +0.78) and 2000 old games at
  20 s (46% lost, median -0.44). The honest 16x going-first games (duelpc, running) become the
  main set; a value head is trained on it once there are enough.
