#!/usr/bin/env bash
# Build the ROOTED combo solver (patches/0002-rooted-search.patch) beside the
# pinned one: build/combo-solver -> build/combo-solver-rooted. The pinned
# binary is never touched. ROOTED_DST overrides the destination. Needs the pinned build to exist first
# (vendor/ygorl/tools/build_combo_solver.sh). Last line: the binary path.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
build="$here/vendor/ygorl/build"
src="$build/combo-solver"
dst="$(realpath -m "${ROOTED_DST:-$build/combo-solver-rooted}")"
[ -x "$src/bin/combosolver" ] || { echo "pinned solver not built: $src" >&2; exit 1; }
rm -rf "$dst"
cp -a "$src" "$dst"
rm -f "$dst"/obj/solver/search_patched.o "$dst"/bin/combosolver-patched
sed -i "s#/build/combo-solver/#/build/$(basename "$dst")/#g" "$dst/compile.cmds"
(cd "$dst/solver-patched" && patch -p2 --no-backup-if-mismatch < "$here/patches/0002-rooted-search.patch" >&2)
# 0003: --stop-on-win, and the R2V_ARENA_UFFD=1 kernel page tracker.
(cd "$dst/solver-patched" && patch -p2 --no-backup-if-mismatch < "$here/patches/0003-stop-on-win-uffd-tracker.patch" >&2)
# 0004: --opp-smart / --opp-hold, the smarter defender.
(cd "$dst/solver-patched" && patch -p2 --no-backup-if-mismatch < "$here/patches/0004-smart-defender.patch" >&2)
# 0006: --opp-rule / --opp-skip / --opp-branches, the searched defender's moves.
(cd "$dst/solver-patched" && patch -p2 --no-backup-if-mismatch < "$here/patches/0006-searched-defender.patch" >&2)
# 0007: --no-self-target, no own-card picks for a listed card's single target.
(cd "$dst/solver-patched" && patch -p2 --no-backup-if-mismatch < "$here/patches/0007-no-self-target.patch" >&2)
# 0008: the card reader's unknown-card note off the arena (a shared set's node
# wiped by one worker's restore: SIGSEGV in 1.3% of NRPA-level-3 searches).
(cd "$dst/solver-patched" && patch -p2 --no-backup-if-mismatch < "$here/patches/0008-card-reader-off-arena.patch" >&2)
# 0009: --no-own-chain, the searcher never chains listed cards (its own-turn
# Mulcharmy draws); the defender's copies of the same code are untouched.
(cd "$dst/solver-patched" && patch -p2 --no-backup-if-mismatch < "$here/patches/0009-no-own-chain.patch" >&2)
grep "solver-patched/[a-z_]*\.cpp" "$dst/compile.cmds" | xargs -d '\n' -P "${BUILD_JOBS:-$(nproc)}" -I{} sh -c '{}'
g++ -o "$dst/bin/combosolver" "$dst"/obj/solver/*.o "$dst"/obj/core/*.o \
    "$dst"/obj/lua/*.o "$dst"/obj/lzma/*.o -lsqlite3 -lpthread -ldl
"$dst/bin/combosolver" --help 2>&1 | grep -q -- "--root" || { echo "built binary lacks --root" >&2; exit 1; }
"$dst/bin/combosolver" --help 2>&1 | grep -q -- "--stop-on-win" || { echo "built binary lacks --stop-on-win" >&2; exit 1; }
"$dst/bin/combosolver" --help 2>&1 | grep -q -- "--opp-smart" || { echo "built binary lacks --opp-smart" >&2; exit 1; }
"$dst/bin/combosolver" --help 2>&1 | grep -q -- "--opp-branches" || { echo "built binary lacks --opp-branches" >&2; exit 1; }
"$dst/bin/combosolver" --help 2>&1 | grep -q -- "--no-self-target" || { echo "built binary lacks --no-self-target" >&2; exit 1; }
echo "$dst/bin/combosolver"
