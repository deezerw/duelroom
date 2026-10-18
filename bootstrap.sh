#!/usr/bin/env bash
# Set up duelroom's engine from a fresh clone.
#
# The repo carries source and decklists only. The rules engine, card
# scripts and card database are large third-party trees that live in
# vendor/ and are gitignored, so a clone cannot run a single solver
# command until this has been run.
#
# Every step below exists because skipping it fails in a way that is not
# obvious from the error. Those are called out inline.
#
#   ./bootstrap.sh            # full setup, safe to re-run
#   ./bootstrap.sh --check    # report what is missing, change nothing
#
set -euo pipefail

# Pure bash: `dirname` is coreutils, and if it is missing or shadowed
# the substitution fails quietly and ROOT ends up wrong, which is worse
# than not starting at all.
_self="${BASH_SOURCE[0]}"
ROOT="$(cd "${_self%/*}" 2>/dev/null && pwd)" || {
    echo "cannot determine the repository root from ${_self}" >&2; exit 1; }
VENDOR="$ROOT/vendor/ygorl"
VENV="$ROOT/venv"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
ok()   { printf '  ok %s\n' "$*"; }

missing=0
need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        warn "missing command: $1 ${2:+($2)}"
        missing=1
    fi
}

say "checking prerequisites"
need git
need g++ "C++17 toolchain, for the rules core"
need cmake "3.20+"
need make
need python3
# The solver links against sqlite3 directly. Without the dev package it
# fails at compile time with a missing sqlite3.h, several hundred lines
# into an otherwise healthy build.
if [[ ! -f /usr/include/sqlite3.h ]]; then
    warn "missing sqlite3.h — apt install libsqlite3-dev"
    missing=1
else
    ok "sqlite3 headers"
fi
if [[ $missing -eq 1 ]]; then
    echo
    echo "Install the above, then re-run. On Debian/Ubuntu:"
    echo "  sudo apt install -y git g++ cmake make python3-venv libsqlite3-dev"
    exit 1
fi
ok "toolchain present"

if [[ $CHECK_ONLY -eq 1 ]]; then
    say "check mode — reporting state only"
    [[ -d "$VENV" ]]                                   && ok "venv"            || warn "no venv"
    [[ -d "$VENDOR/.git" ]]                            && ok "ygorl cloned"    || warn "ygorl not cloned"
    [[ -f "$VENDOR/third_party/ygopro-core/ocgapi.h" ]] && ok "submodules"     || warn "submodules not fetched"
    [[ -f "$VENDOR/build/combo-solver/bin/combosolver" ]] && ok "solver built" || warn "solver not built"
    [[ -f "$ROOT/vendor/third_party_merged/BabelCDB/cards.cdb" ]] && ok "merged card db" || warn "merged card db not built"
    exit 0
fi

say "python environment"
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install -r "$ROOT/requirements.txt" 2>/dev/null || \
    "$VENV/bin/pip" -q install httpx pytest
ok "venv ready"

say "fetching the engine"
if [[ ! -d "$VENDOR/.git" ]]; then
    mkdir -p "$ROOT/vendor"
    git clone --depth 1 https://github.com/n0tevi1/ygorl.git "$VENDOR"
fi
# --recursive is load-bearing. ygopro-core nests Lua as its OWN
# submodule, and without it the build fails with "ygopro-core sources
# not found" even though third_party/ygopro-core looks populated.
git -C "$VENDOR" submodule update --init --recursive --depth 1 --jobs 4
ok "ygorl and submodules"

say "building the python extension"
# Upstream pins python >=3.11,<3.12. That bound is conservative: the
# extension builds and runs clean on 3.12. Relax it rather than
# installing a second interpreter, keeping the original for reference.
if grep -q 'requires-python = ">=3.11,<3.12"' "$VENDOR/pyproject.toml"; then
    cp "$VENDOR/pyproject.toml" "$VENDOR/pyproject.toml.orig"
    sed -i 's/requires-python = ">=3.11,<3.12"/requires-python = ">=3.11,<3.13"/' \
        "$VENDOR/pyproject.toml"
    ok "relaxed the python pin (original kept as pyproject.toml.orig)"
fi
"$VENV/bin/pip" -q install "scikit-build-core>=0.10" "pybind11>=2.13"
"$VENV/bin/pip" install --no-build-isolation "$VENDOR"
ok "ygorl installed"

say "building the combo solver"
( cd "$VENDOR" && bash tools/build_combo_solver.sh )
ok "solver built"

say "merging the card database"
# BabelCDB is not one file: recent sets ship as separate release-*.cdb
# that ygorl never loads. A deck holding one of those cards SEGFAULTS
# the solver after five misleading diagnostics rather than erroring.
"$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$ROOT')
from skills import carddb
path, added, total = carddb.build()
print(f'  merged {len(added)} set database(s), {total} cards')
"

say "verifying"
"$VENV/bin/python" "$ROOT/daemon.py" engine
echo
echo "Next:"
echo "  $VENV/bin/python daemon.py meta        # scrape the current meta"
echo "  $VENV/bin/python daemon.py locals auto # weight it for your room"
echo "  $VENV/bin/python daemon.py import ydke://...   # add your deck"
echo "  $VENV/bin/python daemon.py doctor      # confirm everything is sound"
