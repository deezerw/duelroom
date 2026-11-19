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
  venv/bin/python tools/experiments/budget_plateau.py --stages kill --levels 32 >> results/experiments/plateau_32.log 2>&1
  echo "=== $(date) plateau 32x done ===" >> $LOG
  venv/bin/python tools/experiments/use_timing.py second >> results/experiments/use_timing/run_second.log 2>&1
  echo "=== $(date) use_timing second done ===" >> $LOG
  venv/bin/python tools/ml/board_data.py 10 180 >> results/ml/boards_run.log 2>&1
  echo "=== $(date) board data done ===" >> $LOG
) &
work=$!
while kill -0 $work 2>/dev/null && [ "$(date +%H%M)" -lt "$STOP" ]; do sleep 60; done
pkill -TERM -P $work 2>/dev/null; kill -TERM $work 2>/dev/null
pkill -f "budget_plateau.py|use_timing.py|board_data.py" 2>/dev/null
sleep 10
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -f combosolver; sudo -n systemctl poweroff' >> $LOG 2>&1
echo "=== $(date) stopped, duelpc powered off ===" >> $LOG
