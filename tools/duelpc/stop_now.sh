#!/usr/bin/env bash
# Stop whatever the PC is running for the server and power it off now (user's
# time: "stop at 8:35", 2026-11-20). Stops the board-data unit first, so its own
# 21:00 power-off loop cannot race this one.
cd /home/deezer/duelroom
LOG=results/ml/duelpc_day.log
sudo -n systemctl stop duelpc-boards-1120.service 2>/dev/null
for p in $(pgrep -f "[b]oard_data.py|[d]aemon.py games .*--device duelpc|[t]1_budget.py|[s]ide_gate.py"); do
  tools/stop_tree.sh "$p" >/dev/null 2>&1
done
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -x combosolver; sudo -n systemctl poweroff' >> $LOG 2>&1
echo "=== $(date) stopped on the user's time, duelpc powered off ===" >> $LOG
