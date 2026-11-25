#!/usr/bin/env bash
# The PINNED solver + only the arena half of patch 0003 (the R2V_ARENA_UFFD=1 kernel
# page tracker): build/combo-solver -> build/combo-solver-uffd. The search code is
# untouched; with R2V_ARENA_UFFD unset it IS the pinned solver. engine.SOLVER_BIN uses
# it. Last line: the binary path.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
build="$here/vendor/ygorl/build"
src="$build/combo-solver"
dst="$build/combo-solver-uffd"
[ -x "$src/bin/combosolver" ] || { echo "pinned solver not built: $src" >&2; exit 1; }
rm -rf "$dst"
cp -a "$src" "$dst"
rm -f "$dst"/obj/solver/search_patched.o "$dst"/bin/combosolver-patched
sed -i "s#/build/combo-solver/#/build/combo-solver-uffd/#g" "$dst/compile.cmds"
# 0003's hunks up to main.cpp are arena.cpp + arena.h; the rest is search (stop-on-win).
sed '/^--- combo-solver-rooted\/solver-patched\/main.cpp/,$d' "$here/patches/0003-stop-on-win-uffd-tracker.patch" \
  | (cd "$dst/solver-patched" && patch -p2 --no-backup-if-mismatch >&2)
grep "solver-patched/[a-z_]*\.cpp" "$dst/compile.cmds" | xargs -d '\n' -P "${BUILD_JOBS:-$(nproc)}" -I{} sh -c '{}'
g++ -o "$dst/bin/combosolver" "$dst"/obj/solver/*.o "$dst"/obj/core/*.o \
    "$dst"/obj/lua/*.o "$dst"/obj/lzma/*.o -lsqlite3 -lpthread -ldl
grep -q "R2V_ARENA_UFFD" "$dst/bin/combosolver" || { echo "built binary lacks the uffd tracker" >&2; exit 1; }
echo "$dst/bin/combosolver"
