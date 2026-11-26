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

say "ship code + data to duelpc"
rsync -aR tools/ml skills results/ml/demos/demos.jsonl results/ml/value/positions.jsonl "$H:duelroom/"
# the value positions replay from their game files: send the ones the PC lacks
python3 -c "import json,sys; [print(json.loads(l)['prefix'].removeprefix('/home/deezer/duelroom/')) for l in open('results/ml/value/positions.jsonl')]" \
  | sort -u > /tmp/duelroom-train-prefixes.txt
rsync -a --files-from=/tmp/duelroom-train-prefixes.txt --ignore-missing-args ./ "$H:duelroom/"

say "train on the GPU -> $OUT"
ssh "$H" "cd ~/duelroom && export YGORL_THIRD_PARTY=~/duelroom/vendor/third_party_merged && \
  ~/duelroom-ml/bin/python tools/ml/bc_train.py results/ml/demos/demos.jsonl $OUT $* && \
  ~/duelroom-ml/bin/python tools/ml/value_train.py results/ml/value/positions.jsonl $OUT $OUT/value" \
  2>&1 | grep -v '^{"epoch"' | tail -40

say "pull $OUT back"
mkdir -p "$OUT"
rsync -a "$H:duelroom/$OUT/" "$OUT/"
say "done: $OUT/report.json, $OUT/policy.pt, $OUT/value/"
