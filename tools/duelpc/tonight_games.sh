#!/usr/bin/env bash
# 2026-11-26, after the METHOD 2026-11-26a bump: current-settings games on duelpc for GPU
# training data, until the PC is needed back (user: off at 20:35). The project's rule for a
# long run: a smoke batch first (the same job, 2 enemy hands, 1 run: its FIRST SLICE, never
# thrown away), play_audit --gate on it, then the long run resumes it with 10 enemy hands.
#   tools/duelpc/tonight_games.sh [stop HH:MM, default 20:25]
set -uo pipefail
cd /home/deezer/duelroom
P=venv/bin/python
STOP=${1:-20:25}
LEFT=$(( $(date -d "$STOP" +%s) - $(date +%s) ))
say() { echo "=== $(date '+%F %T') $*"; }
[ "$LEFT" -gt 600 ] || { say "less than 10 minutes before $STOP: nothing started"; exit 0; }
say "smoke: imported vs Angelechy-Meta going first, 2 enemy hands, on duelpc (stop $STOP)"
timeout -s INT "$LEFT" $P daemon.py games imported Angelechy-Meta 1 2 --device duelpc --workers 10
if $P tools/experiments/play_audit.py Angelechy-Meta first duelpc --gate; then
  LEFT=$(( $(date -d "$STOP" +%s) - $(date +%s) ))
  if [ "$LEFT" -gt 600 ]; then
    say "audit clean: the long run (10 enemy hands) until $STOP"
    timeout -s INT "$LEFT" $P daemon.py games imported Angelechy-Meta 1 10 --device duelpc --workers 10
  fi
else
  say "smoke audit FAILED: long run skipped (fix first)"
fi
# a stopped run leaves its solvers running on the PC under their own timeout (up to ~25 min):
# end them, so the PC is free when the user wants it back
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -f "[c]ombosolver"' 2>/dev/null || true
say "games stopped, PC solvers ended; audit (bad games quarantined) + labelling them for training"
$P tools/experiments/play_audit.py Angelechy-Meta first duelpc | grep -v '^    '
[ "${POWEROFF:-0}" = 1 ] && ssh -o ConnectTimeout=10 deezer@duelpc.local 'sudo -n systemctl poweroff' && say "duelpc powered off"
$P tools/ml/demos.py | tail -3
say "done"
