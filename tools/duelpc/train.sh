#!/usr/bin/env bash
# GPU training on duelpc in one command from the server (user, 2026-11-26: "perfect so we can
# start gpu training"). Builds the data here (the DB and game files live here), ships code +
# data + the value positions' replay files, trains the policy (bc_train) then the value head
# (value_train) on the PC's GPU, and pulls the nets and reports back.
#
#   tools/duelpc/train.sh [bc_train args...]          e.g. --epochs 8
#   OUT=results/ml/bc-test tools/duelpc/train.sh --epochs 1 --max-samples 3000   # smoke
#
# Wake the PC first if it sleeps (venv/bin/python -c "from skills import remote;
# remote.prepare(remote.DEVICES['duelpc'])"). Old-era lines are left out (bc_train --eras).
set -euo pipefail
cd /home/deezer/duelroom
H=deezer@duelpc.local
OUT=${OUT:-results/ml/bc-$(date +%Y%m%d-%H%M)}
say() { echo "=== $(date '+%F %T') $*"; }

say "data: label new games, convert lines, value positions"
nice venv/bin/python tools/ml/demos.py | tail -4
nice venv/bin/python tools/ml/value_data.py | tail -4

say "quarantine: games a play audit found a misplay in (whole pairings left out)"
# audit EVERY live section first: a game nobody audited must not reach training unchecked
venv/bin/python - <<'PY'
import sqlite3, subprocess
from skills import game
conn = sqlite3.connect("data/duelroom.db")
for enemy, first, device in conn.execute(
        "SELECT DISTINCT enemy, first, device FROM game_results WHERE method=? "
        "AND device IN ('server','duelpc')", (game.METHOD,)):
    out = subprocess.run(["venv/bin/python", "tools/experiments/play_audit.py", enemy,
                          "first" if first else "second", device], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"play_audit {enemy} {device} failed:\n{out.stderr[-2000:]}")
    print(f"  {enemy} going {'first' if first else 'second'} [{device}]:",
          next((l.strip() for l in out.stdout.splitlines() if "quarantined" in l), "?"))
PY
venv/bin/python -c "from skills import db, quarantine; quarantine.dump(db.connect(), quarantine.FILE)"

say "ship code + data to duelpc"
rsync -aR tools/ml skills results/ml/demos/demos.jsonl results/ml/value/positions.jsonl results/ml/bad_pairings.json "$H:duelroom/"
# the value positions replay from their game files: send the ones the PC lacks
python3 -c "import json,sys; [print(json.loads(l)['prefix'].removeprefix('/home/deezer/duelroom/')) for l in open('results/ml/value/positions.jsonl')]" \
  | sort -u > /tmp/duelroom-train-prefixes.txt
rsync -a --files-from=/tmp/duelroom-train-prefixes.txt --ignore-missing-args ./ "$H:duelroom/"

# the era filter applies to BOTH nets (it reached only bc_train before: the value head trained on
# every era after 2026-11-20 while the policy trained on the live method)
VERAS=""
args=("$@")
for i in "${!args[@]}"; do [ "${args[$i]}" = "--eras" ] && VERAS="--eras ${args[$((i+1))]}"; done
say "train on the GPU -> $OUT"
ssh "$H" "cd ~/duelroom && export YGORL_THIRD_PARTY=~/duelroom/vendor/third_party_merged && \
  ~/duelroom-ml/bin/python tools/ml/bc_train.py results/ml/demos/demos.jsonl $OUT $* && \
  ~/duelroom-ml/bin/python tools/ml/value_train.py results/ml/value/positions.jsonl $OUT $OUT/value $VERAS" \
  2>&1 | grep -v '^{"epoch"' | tail -40

say "pull $OUT back"
mkdir -p "$OUT"
rsync -a "$H:duelroom/$OUT/" "$OUT/"
say "done: $OUT/report.json, $OUT/policy.pt, $OUT/value/"
