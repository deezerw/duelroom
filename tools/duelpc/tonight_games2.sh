#!/usr/bin/env bash
# Replaces tonight_games.sh's tail (2026-11-26 18:4x): after the smoke (pid $1) the long run would
# solve 192 NEW turn 1s (8 more enemy hands, ~3 h) and play no game before the 20:25 stop --
# and a turn-1 change at the next bump would discard them. A SECOND RUN of the smoke's 48
# pairings reuses their turn 1s (games at once; 2 runs is the project's consistency check).
#   tools/duelpc/tonight_games2.sh <smoke pid> [stop HH:MM, default 20:25]
set -uo pipefail
cd /home/deezer/duelroom
P=venv/bin/python
STOP=${2:-20:25}
say() { echo "=== $(date '+%F %T') $*"; }
while kill -0 "$1" 2>/dev/null; do sleep 30; done
say "smoke finished"
if $P tools/experiments/play_audit.py Angelechy-Meta first duelpc --gate; then
  LEFT=$(( $(date -d "$STOP" +%s) - $(date +%s) ))
  if [ "$LEFT" -gt 600 ]; then
    say "audit clean: a second run of the 48 pairings until $STOP"
    timeout -s INT "$LEFT" $P daemon.py games imported Angelechy-Meta 2 2 --device duelpc --workers 10
  fi
else
  say "smoke audit FAILED: no more games (fix first)"
fi
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -f "[c]ombosolver"' 2>/dev/null || true
say "games stopped, PC solvers ended; labelling them for training"
$P tools/ml/demos.py | tail -3
say "done"
