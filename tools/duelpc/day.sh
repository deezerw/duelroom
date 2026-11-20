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
  # Stage 16 step 2: imitation training on the PC's GPU (it runs there, in ~/duelroom-ml)
  if [ -f results/ml/demos/demos.jsonl ] && [ ! -f results/ml/bc/policy.pt ]; then
    # it uses one core then the GPU: board data fills the other 15 until it ends
    (sleep 60; tools/duelpc/fill_while_bc.sh) &
    rsync -a tools/ml/bc_train.py deezer@duelpc.local:duelroom/tools/ml/ \
      && rsync -a results/ml/demos/demos.jsonl deezer@duelpc.local:duelroom/results/ml/demos/ \
      && ssh deezer@duelpc.local 'cd ~/duelroom && YGORL_THIRD_PARTY=$HOME/duelroom/vendor/third_party_merged \
           ~/duelroom-ml/bin/python tools/ml/bc_train.py results/ml/demos/demos.jsonl results/ml/bc \
           --epochs 8 --max-samples 300000' >> results/ml/bc_train.log 2>&1 \
      && mkdir -p results/ml/bc && rsync -a deezer@duelpc.local:duelroom/results/ml/bc/ results/ml/bc/
    echo "=== $(date) imitation training: $( [ -f results/ml/bc/policy.pt ] && echo done || echo FAILED, see results/ml/bc_train.log) ===" >> $LOG
  fi
  venv/bin/python tools/experiments/budget_plateau.py --stages kill --levels 32 >> results/experiments/plateau_32.log 2>&1
  echo "=== $(date) plateau 32x done ===" >> $LOG
  # is our turn 3 converged at 4x (game.T3_X)? 87/89/94% at 1/2/4x, 2x->4x still flipped 3
  venv/bin/python tools/experiments/budget_plateau.py --stages t3 --levels 8 >> results/experiments/plateau_t3_8.log 2>&1
  echo "=== $(date) plateau t3 8x done ===" >> $LOG
  # why is going first weak? does a bigger turn-1 search survive the converged kill check?
  venv/bin/python tools/experiments/t1_budget.py 48 --levels 1,4 >> results/experiments/t1_budget.log 2>&1
  echo "=== $(date) t1 budget test done ===" >> $LOG
  # the honest going-first baseline (16x kill check, trained enemy turn 2), run here and
  # not through the jobs queue: a queued duelpc job is retried with Wake-on-LAN, at night too
  venv/bin/python daemon.py games imported Angelechy-Meta 2 10 --device duelpc \
      >> results/ml/duelpc_games_first.log 2>&1      # every trained hand (resumable)
  echo "=== $(date) going-first games done ===" >> $LOG
  # Stage 13's gate under the honest settings (blank -3 Ash +3 vanilla going first must
  # not gain; -3 Engraver +3 vanilla going second must lose) before any side card is read
  venv/bin/python tools/experiments/side_gate.py >> results/experiments/side_gate_honest.log 2>&1
  echo "=== $(date) side gate done ===" >> $LOG
  # a second opponent: is going first weak in general, or against Angelechy's going-second list?
  # only once its 10 game hands are trained (the server trains them after its own jobs) and
  # the trainer is not still improving them: never a mix of trained and one-target turns
  if venv/bin/python -c "
from skills import db, engine, trainer
c = db.connect()
dh = trainer.enemy_key(trainer.deck_hash(engine.DECKS / 'blue_eyes_oct_2026.ydk'),
                       trainer.deck_hash(engine.DECKS / 'imported.ydk'))
n = c.execute('SELECT COUNT(*) FROM trainer_hands WHERE deck_hash=? AND best_goal IS NOT NULL', (dh,)).fetchone()[0]
raise SystemExit(0 if n >= 10 else 1)" && ! ps -eo cmd | grep -q "[d]aemon.py train blue_eyes_oct_2026"; then
    venv/bin/python daemon.py games imported blue_eyes_oct_2026 2 10 --device duelpc \
        >> results/ml/duelpc_games_first_be.log 2>&1
    echo "=== $(date) going-first games vs Blue-Eyes done ===" >> $LOG
  else
    echo "=== $(date) Blue-Eyes games skipped: its enemy hands are not trained yet ===" >> $LOG
  fi
  venv/bin/python tools/experiments/use_timing.py second >> results/experiments/use_timing/run_second.log 2>&1
  echo "=== $(date) use_timing second done ===" >> $LOG
  venv/bin/python tools/ml/board_data.py 10 180 >> results/ml/boards_run.log 2>&1
  echo "=== $(date) board data done ===" >> $LOG
) &
work=$!
while kill -0 $work 2>/dev/null && [ "$(date +%H%M)" -lt "$STOP" ]; do sleep 60; done
pkill -TERM -P $work 2>/dev/null; kill -TERM $work 2>/dev/null
pkill -f "budget_plateau.py|use_timing.py|board_data.py|daemon.py games .*--device duelpc|side_gate.py|t1_budget.py" 2>/dev/null
sleep 10
ssh -o ConnectTimeout=10 deezer@duelpc.local 'pkill -x combosolver; pkill -f "[b]c_train.py"; sudo -n systemctl poweroff' >> $LOG 2>&1
echo "=== $(date) stopped, duelpc powered off ===" >> $LOG
