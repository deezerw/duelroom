#!/usr/bin/env bash
# Training-data games on the SERVER (it has no curfew, unlike duelpc): the project's rule for a
# long run -- a smoke batch first (2 enemy hands, 1 run: its first slice, kept), play_audit
# --gate on it, then the long run resumes it with every enemy hand. Niced: your duels and the
# web app go first; budgets count CPU work (patch 0015), so the searches are not weakened.
#   tools/server_games.sh <enemy> [enemy hands, default 10] [first|second, default first]
set -uo pipefail
cd /home/deezer/duelroom
P=venv/bin/python
ENEMY=${1:?enemy deck}; HANDS=${2:-10}; SIDE=${3:-first}
FLAG=$([ "$SIDE" = second ] && echo --second || echo)
say() { echo "=== $(date '+%F %T') $*"; }
say "smoke: imported vs $ENEMY going $SIDE, 2 enemy hands, on the server"
nice -n 10 $P daemon.py games imported "$ENEMY" 1 2 $FLAG
if $P tools/experiments/play_audit.py "$ENEMY" "$SIDE" server --gate; then
  say "audit clean: the long run ($HANDS enemy hands)"
  nice -n 10 $P daemon.py games imported "$ENEMY" 1 "$HANDS" $FLAG
else
  say "smoke audit FAILED: long run skipped (fix first)"
fi
say "labelling the games for training"
$P tools/ml/demos.py | tail -3
say "done"
