# Stage 12 — A move prior for the search (only if Stage 11 paid off)

Goal: the solver finds lines through hand traps more often at the same time
budget, so your deck is not under-rated by a weak pilot. Same rules as
Stage 11 (engine-played numbers, paired A/B, intervals, simple first).
Start only after Stage 11 step 4 shows a measured gain. Delete this file when
done and verified, or when its gate fails (record why in CLAUDE.md).

Caution up front: `--adapt` (carrying NRPA's learnt policy between solves)
was measured useless at equal time (Stage 7). A network differs in that it
generalises across positions, but that is the claim this stage must prove.

1. **Training data**: (state, chosen option) pairs from every solver line
   the games and the trainer already found (ygorl encodes both: state as
   above, up to 128 options x 10 features).
2. **Prior net**: small network scoring the legal options; offline check =
   top-1/top-5 agreement with held-out solver lines.
3. **Into NRPA**: the cheapest place first: seed NRPA's root policy weights
   from the net (one call per solve), then the first N plies of each rollout.
   Inference via ONNX Runtime inside the solver (a patch like 0003-0005);
   CPU threads batch positions to the GPU.
4. **Gate**: paired, equal wall time, the 240 stored turn-1 positions: goal
   reached and lost-on-turn-2. Then expert iteration (solve with the prior,
   retrain on the better lines) only if round one gains.
