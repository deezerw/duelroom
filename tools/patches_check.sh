#!/usr/bin/env bash
# Is the patch stack whole? Applies every patch in tools/build_rooted_solver.sh to a fresh copy of
# the pinned build and builds it in a throwaway folder, then deletes it: run before an upstream
# bump or after adding a patch (BACKLOG "patch-stack upkeep"). Prints the stack's last patch.
# Niced; the live rooted binary is never touched. Exit non-zero = the stack is broken.
set -euo pipefail
cd /home/deezer/duelroom
chk=vendor/ygorl/build/combo-solver-patches-check
trap 'rm -rf "$chk"' EXIT
ROOTED_DST=$chk BUILD_JOBS=${BUILD_JOBS:-3} nice -n 15 tools/build_rooted_solver.sh 2>&1 \
  | grep -v '^patching file' | tail -1
echo "stack OK through $(grep -o 'patches/[0-9]*-[a-z-]*\.patch' tools/build_rooted_solver.sh | tail -1)"
