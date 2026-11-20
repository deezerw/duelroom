#!/usr/bin/env bash
# The imitation training uses one CPU core (then the GPU), and day.sh waits on it:
# 15 of the PC's 16 threads would sit idle. Fill them with board data
# (resumable) while bc_train runs, and stop it as soon as training ends, so
# the next step's time-budgeted searches get the CPU to themselves.
cd /home/deezer/duelroom
export DUELROOM_DEVICE=duelpc
venv/bin/python tools/ml/board_data.py 10 180 >> results/ml/boards_run.log 2>&1 &
fill=$!
while kill -0 $fill 2>/dev/null && ssh -o ConnectTimeout=10 deezer@duelpc.local 'pgrep -f "[b]c_train.py" >/dev/null'; do
  sleep 30
done
pkill -TERM -P $fill 2>/dev/null; kill -TERM $fill 2>/dev/null
sleep 5
# board_data's searches only (their scratch dir), never the day run's next step
ssh deezer@duelpc.local 'pkill -f "[r]esults/ml/boards/scratch"' 2>/dev/null
echo "=== $(date) board data filler stopped (training ended) ===" >> results/ml/duelpc_day.log
