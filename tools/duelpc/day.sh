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
# Power button -> Windows, Wake-on-LAN -> Linux (tools/duelpc/boot-choice.sh): installed
# (idempotent) on every day run; it acts from the NEXT boot on.
rsync -a tools/duelpc/boot-choice.sh tools/duelpc/boot-choice.service deezer@duelpc.local:/tmp/ \
  && ssh deezer@duelpc.local 'sudo -n install -m 755 /tmp/boot-choice.sh /usr/local/bin/boot-choice.sh \
       && sudo -n install -m 644 /tmp/boot-choice.service /etc/systemd/system/boot-choice.service \
       && sudo -n systemctl daemon-reload && sudo -n systemctl enable boot-choice.service' >> $LOG 2>&1 \
  && echo "=== $(date) boot-choice installed ===" >> $LOG
(
  # Queue rewritten 2026-11-25 for METHOD 2026-11-25c (one committed defence, a majority of
  # futures, CPU-work budgets). Done on 2026-11-23 and not repeated: the net-lethal test (net
  # found 0 of 194), the blunder checker (18 of 95). The four steps the PC went down for come back.
  # 1. the honest going-first baseline on 2026-11-25c: smoke, audited, then the full run
  venv/bin/python daemon.py games imported Angelechy-Meta 1 2 --device duelpc \
      >> results/ml/duelpc_games_first.log 2>&1
  if venv/bin/python tools/experiments/play_audit.py Angelechy-Meta first duelpc --gate \
       >> results/ml/duelpc_audit.log 2>&1; then
    echo "=== $(date) going-first smoke audit passed (2026-11-25c) ===" >> $LOG
    venv/bin/python daemon.py games imported Angelechy-Meta 2 10 --device duelpc \
        >> results/ml/duelpc_games_first.log 2>&1
    echo "=== $(date) going-first games done (2026-11-25c) ===" >> $LOG
  else
    echo "=== $(date) going-first smoke audit FAILED: long run skipped, see results/ml/duelpc_audit.log ===" >> $LOG
  fi
  # 2. turn 1 falls back to remembered starter boards, picked by look-ahead (tables exist now)
  venv/bin/python tools/experiments/t1_starters.py >> results/experiments/t1_starters.log 2>&1
  echo "=== $(date) starter-fallback A/B done ===" >> $LOG
  # 3. is our turn 3 converged at 4x (game.T3_X)?
  venv/bin/python tools/experiments/budget_plateau.py --stages t3 --levels 8 >> results/experiments/plateau_t3_8.log 2>&1
  echo "=== $(date) plateau t3 8x done ===" >> $LOG
  # 4. Stage 13's gate under the honest settings, before any side card is read
  venv/bin/python tools/experiments/side_gate.py >> results/experiments/side_gate_honest.log 2>&1
  echo "=== $(date) side gate done ===" >> $LOG
  # 5. last and long, so the PC never idles before STOP
  venv/bin/python tools/ml/board_data.py 10 180 >> results/ml/boards_run.log 2>&1
  echo "=== $(date) board data done ===" >> $LOG
) &
work=$!
while kill -0 $work 2>/dev/null && [ "$(date +%H%M)" -lt "$STOP" ]; do sleep 60; done
pkill -TERM -P $work 2>/dev/null; kill -TERM $work 2>/dev/null
pkill -f "budget_plateau.py|use_timing.py|board_data.py|daemon.py games .*--device duelpc|side_gate.py|blunders.py|t1_starters.py" 2>/dev/null
sleep 10
rsync -a deezer@duelpc.local:duelroom/results/ml/net_lethal/results.jsonl results/ml/net_lethal/ 2>/dev/null
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -x combosolver; pkill -f "[b]c_train.py|[p]rior_ab.py|[n]et_lethal.py"; sudo -n systemctl poweroff' >> $LOG 2>&1
echo "=== $(date) stopped, duelpc powered off ===" >> $LOG
