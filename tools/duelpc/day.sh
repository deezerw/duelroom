#!/usr/bin/env bash
# A day on duelpc (user: Mon-Wed usually away, never at night -- it is loud).
# Wakes it, runs the queue in order (each resumable), and at STOP (HHMM) stops
# everything and powers it OFF. Usage: duelpc_day.sh 2100
cd /home/deezer/duelroom
STOP=${1:-2100}
LOG=results/ml/duelpc_day.log
export DUELROOM_DEVICE=duelpc
echo "=== $(date) day starts, stop at $STOP ===" >> $LOG
venv/bin/python -c "from skills import remote; remote.ensure_up(remote.get('duelpc'))" >> $LOG 2>&1 \
  || { echo "duelpc did not come up (left on in Windows?): nothing run" >> $LOG; exit 0; }
# Power button -> Windows, Wake-on-LAN -> Linux (tools/duelpc/boot-choice.sh): installed
# (idempotent) on every day run; it acts from the NEXT boot on.
rsync -a tools/duelpc/boot-choice.sh tools/duelpc/boot-choice.service deezer@duelpc.local:/tmp/ \
  && ssh deezer@duelpc.local 'sudo -n install -m 755 /tmp/boot-choice.sh /usr/local/bin/boot-choice.sh \
       && sudo -n install -m 644 /tmp/boot-choice.service /etc/systemd/system/boot-choice.service \
       && sudo -n systemctl daemon-reload && sudo -n systemctl enable boot-choice.service' >> $LOG 2>&1 \
  && echo "=== $(date) boot-choice installed ===" >> $LOG
(
  # Queue of 2026-11-23 (rewritten 2026-11-21 after METHOD 2026-11-21c went live; the old
  # steps -- imitation training, the net-prior A/B, turn-1 budget, kill plateau -- are done).
  # 0. (background, GPU + one core) does the learned net find wins the games' own solver
  #    missed? 194 no-win lethal turns (tools/ml/net_lethal.py) -- decides Stage 16 step 3b
  if [ -f results/ml/bc/policy.pt ] && [ -f results/ml/net_lethal/positions.jsonl ]; then (
    rsync -a skills/__init__.py skills/mcts.py skills/net_prior.py deezer@duelpc.local:duelroom/skills/ \
      && rsync -a tools/ml/net_lethal.py deezer@duelpc.local:duelroom/tools/ml/ \
      && rsync -a results/ml/net_lethal/ deezer@duelpc.local:duelroom/results/ml/net_lethal/ \
      && ssh deezer@duelpc.local 'cd ~/duelroom && YGORL_THIRD_PARTY=$HOME/duelroom/vendor/third_party_merged \
           ~/duelroom-ml/bin/python tools/ml/net_lethal.py run results/ml/bc 800' >> results/ml/net_lethal.log 2>&1
    rsync -a deezer@duelpc.local:duelroom/results/ml/net_lethal/results.jsonl results/ml/net_lethal/ 2>/dev/null
    echo "=== $(date) net-lethal test: $(venv/bin/python tools/ml/net_lethal.py summary 2>&1 | tail -1) ===" >> $LOG
  ) & fi
  # 1. the honest going-first baseline on METHOD 2026-11-21c (the 21b games are 'm21b'):
  #    smoke first (the full run's first slice), audited; the long run only if it is clean
  venv/bin/python daemon.py games imported Angelechy-Meta 1 2 --device duelpc \
      >> results/ml/duelpc_games_first.log 2>&1
  if venv/bin/python tools/experiments/play_audit.py Angelechy-Meta first duelpc --gate \
       >> results/ml/duelpc_audit.log 2>&1; then
    echo "=== $(date) going-first smoke audit passed (2026-11-21c) ===" >> $LOG
    venv/bin/python daemon.py games imported Angelechy-Meta 2 10 --device duelpc \
        >> results/ml/duelpc_games_first.log 2>&1
    echo "=== $(date) going-first games done (2026-11-21c) ===" >> $LOG
  else
    echo "=== $(date) going-first smoke audit FAILED: long run skipped, see results/ml/duelpc_audit.log ===" >> $LOG
  fi
  # 2. the blunder checker: which decision threw each lethal turn's win (every no-win game)
  venv/bin/python tools/experiments/blunders.py lethal Angelechy-Meta >> results/experiments/blunders.log 2>&1
  echo "=== $(date) blunder checker (lethal turns) done ===" >> $LOG
  # 3. turn 1 falls back to remembered starter boards, picked by look-ahead (needs the tables)
  venv/bin/python tools/experiments/t1_starters.py >> results/experiments/t1_starters.log 2>&1
  echo "=== $(date) starter-fallback A/B done (or no tables yet) ===" >> $LOG
  # 4. is our turn 3 converged at 4x (game.T3_X)? 87/89/94% at 1/2/4x
  venv/bin/python tools/experiments/budget_plateau.py --stages t3 --levels 8 >> results/experiments/plateau_t3_8.log 2>&1
  echo "=== $(date) plateau t3 8x done ===" >> $LOG
  # 5. Stage 13's gate under the honest settings, before any side card is read
  venv/bin/python tools/experiments/side_gate.py >> results/experiments/side_gate_honest.log 2>&1
  echo "=== $(date) side gate done ===" >> $LOG
  # 6. last and long, so the PC never idles before STOP (2026-11-21 it ran out at 14:37)
  venv/bin/python tools/ml/board_data.py 10 180 >> results/ml/boards_run.log 2>&1
  echo "=== $(date) board data done ===" >> $LOG
) &
work=$!
while kill -0 $work 2>/dev/null && [ "$(date +%H%M)" -lt "$STOP" ]; do sleep 60; done
pkill -TERM -P $work 2>/dev/null; kill -TERM $work 2>/dev/null
pkill -f "budget_plateau.py|use_timing.py|board_data.py|daemon.py games .*--device duelpc|side_gate.py|blunders.py|t1_starters.py" 2>/dev/null
sleep 10
rsync -a deezer@duelpc.local:duelroom/results/ml/net_lethal/results.jsonl results/ml/net_lethal/ 2>/dev/null
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -x combosolver; pkill -f "[b]c_train.py|[p]rior_ab.py|[n]et_lethal.py"; sudo -n systemctl poweroff' >> $LOG 2>&1
echo "=== $(date) stopped, duelpc powered off ===" >> $LOG
