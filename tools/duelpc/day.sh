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
(
  # Stage 16 step 2: imitation training on the PC's GPU (it runs there, in ~/duelroom-ml)
  if [ -f results/ml/demos/demos.jsonl ] && [ ! -f results/ml/bc/policy.pt ]; then
    rsync -a tools/ml/bc_train.py deezer@duelpc.local:duelroom/tools/ml/ \
      && rsync -a results/ml/demos/demos.jsonl deezer@duelpc.local:duelroom/results/ml/demos/ \
      && ssh deezer@duelpc.local 'cd ~/duelroom && YGORL_THIRD_PARTY=$HOME/duelroom/vendor/third_party_merged \
           ~/duelroom-ml/bin/python tools/ml/bc_train.py results/ml/demos/demos.jsonl results/ml/bc \
           --epochs 8 --max-samples 300000' >> results/ml/bc_train.log 2>&1 \
      && mkdir -p results/ml/bc && rsync -a deezer@duelpc.local:duelroom/results/ml/bc/ results/ml/bc/
    echo "=== $(date) imitation training: $( [ -f results/ml/bc/policy.pt ] && echo done || echo FAILED, see results/ml/bc_train.log) ===" >> $LOG
  fi
  venv/bin/python tools/experiments/budget_plateau.py --stages kill --levels 32 >> results/experiments/plateau_32.log 2>&1
  echo "=== $(date) plateau 32x done ===" >> $LOG
  # the honest going-first baseline (16x kill check, trained enemy turn 2), run here and
  # not through the jobs queue: a queued duelpc job is retried with Wake-on-LAN, at night too
  venv/bin/python daemon.py games imported Angelechy-Meta 2 10 --device duelpc \
      >> results/ml/duelpc_games_first.log 2>&1      # every trained hand (resumable)
  echo "=== $(date) going-first games done ===" >> $LOG
  venv/bin/python tools/experiments/use_timing.py second >> results/experiments/use_timing/run_second.log 2>&1
  echo "=== $(date) use_timing second done ===" >> $LOG
  venv/bin/python tools/ml/board_data.py 10 180 >> results/ml/boards_run.log 2>&1
  echo "=== $(date) board data done ===" >> $LOG
) &
work=$!
while kill -0 $work 2>/dev/null && [ "$(date +%H%M)" -lt "$STOP" ]; do sleep 60; done
pkill -TERM -P $work 2>/dev/null; kill -TERM $work 2>/dev/null
pkill -f "budget_plateau.py|use_timing.py|board_data.py|daemon.py games" 2>/dev/null
sleep 10
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -f combosolver; pkill -f bc_train.py; sudo -n systemctl poweroff' >> $LOG 2>&1
echo "=== $(date) stopped, duelpc powered off ===" >> $LOG
