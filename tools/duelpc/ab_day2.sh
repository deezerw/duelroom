#!/usr/bin/env bash
# The PC's afternoon of 2026-11-21 (after day.sh's going-first run): the AI
# measurements that decide the next method, before any more long runs.
#   1. negate_live_ab.py ab -- patch 0011 at the games' budget, on the screened
#      positions (the server ran the screen)
#   2. t1_dominus.py -- should Angelechy fire its Dominus traps from the hand on our turn 1
# GAMES_FIRST=1: first resume the going-first games day.sh left (2026-11-21: cut at
# 462 of 480 by a switch that matched YESTERDAY's done line). Both resumable. At STOP (HHMM) it stops everything and powers the PC off,
# as day.sh does. Usage: systemd-run --unit ... --uid=deezer ab_day.sh 1750
cd /home/deezer/duelroom
STOP=${1:-1750}
LOG=results/ml/duelpc_day.log
echo "=== $(date) A/B afternoon starts, stop at $STOP ===" >> $LOG
(
  [ "${GAMES_FIRST:-0}" = 1 ] && venv/bin/python daemon.py games imported Angelechy-Meta 2 10 --device duelpc \
      >> results/ml/duelpc_games_first.log 2>&1 && echo "=== $(date) going-first games done (resumed) ===" >> $LOG
  # the 0011 build is not the live one: put it where the PC's solver path mirrors ours
  rsync -a vendor/ygorl/build/combo-solver-0011/bin/ deezer@duelpc.local:duelroom/vendor/ygorl/build/combo-solver-0011/bin/
  DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/negate_live_ab.py ab \
      >> results/experiments/negate_live_ab_pc.log 2>&1
  echo "=== $(date) patch 0011 A/B done ===" >> $LOG
  DUELROOM_DEVICE=duelpc venv/bin/python tools/experiments/t1_dominus.py \
      >> results/experiments/t1_dominus.log 2>&1
  echo "=== $(date) Dominus turn-1 A/B done ===" >> $LOG
) &
work=$!
while kill -0 $work 2>/dev/null && [ "$(date +%H%M)" -lt "$STOP" ]; do sleep 60; done
pkill -TERM -P $work 2>/dev/null; kill -TERM $work 2>/dev/null
pkill -f "[n]egate_live_ab.py ab|[t]1_dominus.py" 2>/dev/null
sleep 10
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -x combosolver; sudo -n systemctl poweroff' >> $LOG 2>&1
echo "=== $(date) stopped, duelpc powered off ===" >> $LOG
