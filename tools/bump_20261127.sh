#!/usr/bin/env bash
# METHOD 2026-11-27a (user, 2026-11-27: "dont include any games with bad data ... fix everything
# you can so we can get some good data"). The play_audit of every 2026-11-26a game flagged 11 of
# 144 for a hand trap given to a cost (Ash / Fuwalos as Mind Shuffle-style discard fodder):
#   * rooted solver = build/combo-solver-0020 (0002-0020: 0019 --opp-on-field, Ghost Ogre only
#     onto a card still on the field; 0020 --opp-keep, hand traps the last cards given to a
#     cost); game.defender_args emits both once the binary has them. Old one kept as pre0019.
#   * every 2026-11-26a section relabelled m26a / m26a-pc, turn 1s MOVED (turn 1's survival
#     pick runs kill checks with the same defender, so none may be reused); bad_games follow.
# Run only when no games run is using the rooted binary (it is re-synced to duelpc by stamp).
set -euo pipefail
cd /home/deezer/duelroom
P=venv/bin/python
say() { echo "=== $(date '+%F %T') $*"; }

if pgrep -f "[r]espond_all_ab.py|[t]1_starters.py|[d]aemon.py games" >/dev/null; then
  echo "an A/B or games run is still using the rooted binary: wait" >&2; exit 1
fi
cp data/duelroom.db data/duelroom.db.bak-20261127
say "DB backed up"

B=vendor/ygorl/build
rm -rf $B/combo-solver-rooted-pre0019
cp -a $B/combo-solver-rooted $B/combo-solver-rooted-pre0019
cp $B/combo-solver-0020/bin/combosolver $B/combo-solver-rooted/bin/combosolver
help="$($B/combo-solver-rooted/bin/combosolver --help 2>&1)"
for f in --opp-keep --opp-on-field --opp-on-activation --self-hand-root; do
  grep -q -- "$f" <<<"$help" || { echo "rooted binary lacks $f" >&2; exit 1; }
done
say "rooted solver = 0002-0020"

for spec in "Angelechy-Meta first duelpc m26a-pc" "blue_eyes_oct_2026 first server m26a"; do
  set -- $spec
  $P tools/relabel_games.py imported "$1" "$2" "$4" --from "$3" --move-turn1
  sqlite3 data/duelroom.db "UPDATE bad_games SET device='$4' WHERE enemy='$1' AND first=$([ $2 = first ] && echo 1 || echo 0) AND device='$3'"
done
say "2026-11-26a sections relabelled"
