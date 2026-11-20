#!/usr/bin/env bash
# Board data on duelpc until STOP (HHMM), then stop it and power the PC off -- what
# day.sh's last step does, started on its own (2026-11-20: board_data restarted on
# the games' method mid-run; day.sh would have powered the PC off when it ended).
cd /home/deezer/duelroom
STOP=${1:-2100}
LOG=results/ml/duelpc_day.log
export DUELROOM_DEVICE=duelpc
echo "=== $(date) board data (method $(venv/bin/python -c 'from skills import game; print(game.METHOD)')) until $STOP ===" >> $LOG
venv/bin/python tools/ml/board_data.py 10 180 >> results/ml/boards_run.log 2>&1 &
work=$!
while kill -0 $work 2>/dev/null && [ "$(date +%H%M)" -lt "$STOP" ]; do sleep 60; done
kill -TERM $work 2>/dev/null; sleep 10
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -x combosolver; sudo -n systemctl poweroff' >> $LOG 2>&1
echo "=== $(date) board data stopped, duelpc powered off ===" >> $LOG
