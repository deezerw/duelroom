#!/usr/bin/env bash
# One-off, 2026-11-20 21:10: once the PC's day run has stopped, file the going-first
# games played before the own-turn / gift fixes (game.METHOD_SINCE) under "gift".
cd /home/deezer/duelroom
if pgrep -f "[d]aemon.py games .*--device duelpc|[s]ide_gate.py" >/dev/null; then
  echo "$(date) duelpc games still running: relabel skipped" >> results/ml/duelpc_day.log; exit 0
fi
cp data/duelroom.db data/duelroom.db.bak-20261120b
venv/bin/python tools/relabel_games.py imported Angelechy-Meta first gift --from duelpc --move-turn1 \
  >> results/ml/duelpc_day.log 2>&1
# the side gate's blank variant (-3 Ash +3 vanilla), going first: its 480 games are old-method too
venv/bin/python tools/relabel_games.py "imported~-3x14558127+3x43096270" Angelechy-Meta first gift --from duelpc \
  --move-turn1 >> results/ml/duelpc_day.log 2>&1
echo "=== $(date) going-first duelpc games relabelled 'gift' ===" >> results/ml/duelpc_day.log
