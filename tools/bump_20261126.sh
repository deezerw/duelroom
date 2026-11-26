#!/usr/bin/env bash
# METHOD 2026-11-26a (user chose 2026-11-26: "Bump with both"). Phase 1's bump:
#   * rooted solver rebuilt with 0002-0018 (0016 --opp-hand-hide, unused unless asked;
#     0017 --opp-on-activation: Veiler / Imperm only onto a monster's on-field effect;
#     0018 --self-hand-root); the old one kept as combo-solver-rooted-pre0016
#   * game.RESPOND_ALL (every card answers), game.T1_PICK_ALWAYS + T1_FALLBACKS 6
#   * already in code since 25c: Field Spells activated at end of turn, Super Poly dead
#     with an empty hand, marked interrupters counted as pieces
#   * every 2026-11-25c section (games and stored turn 1s) relabelled m25c / m25c-pc,
#     turn 1s MOVED (the change touches turn 1, so none may be reused)
# Run only when no A/B is using the rooted binary (it is re-synced to duelpc by stamp).
set -euo pipefail
cd /home/deezer/duelroom
P=venv/bin/python
say() { echo "=== $(date '+%F %T') $*"; }

if pgrep -f "[r]espond_all_ab.py|[t]1_starters.py|[d]aemon.py games" >/dev/null; then
  echo "an A/B or games run is still using the rooted binary: wait" >&2; exit 1
fi
cp data/duelroom.db data/duelroom.db.bak-20261126
say "DB backed up"

B=vendor/ygorl/build
rm -rf $B/combo-solver-rooted-pre0016
cp -a $B/combo-solver-rooted $B/combo-solver-rooted-pre0016
tools/build_rooted_solver.sh | tail -1
say "rooted solver rebuilt (0002-0018)"

for spec in "Angelechy-Meta first server m25c" "Angelechy-Meta first duelpc m25c-pc" \
            "Angelechy-Meta second server m25c" "Angelechy-Meta second duelpc m25c-pc" \
            "blue_eyes_oct_2026 first server m25c" "blue_eyes_oct_2026 second server m25c"; do
  set -- $spec
  $P tools/relabel_games.py imported "$1" "$2" "$4" --from "$3" --move-turn1
done
say "2026-11-25c sections relabelled"
