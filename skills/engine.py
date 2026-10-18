"""Engine integration layer.

duelroom does not implement a Yu-Gi-Oh! rules engine. It drives
[ygorl](https://github.com/n0tevi1/ygorl), which wraps edo9300/ygopro-core
(the EDOPro core) with Project Ignis card scripts and data, plus
[ygo-combo-solver](https://github.com/96jonesa/ygo-combo-solver) as a
subprocess for turn-1 line search.

Everything in this module is about locating that stack, pinning its
versions, and exposing the two calls the rest of duelroom needs. The search
itself lives in ygorl; reimplementing it was never the plan and would be
roughly 30k lines of C++.

Setup (once):
    cd vendor && git clone https://github.com/n0tevi1/ygorl.git
    cd ygorl && git submodule update --init --recursive --depth 1
    sudo apt install libsqlite3-dev
    ../../venv/bin/pip install --no-build-isolation .
    bash tools/build_combo_solver.sh

The upstream pins Python to >=3.11,<3.12. That bound is conservative --
the extension builds and runs clean on 3.12.3, which is what this project
uses. `vendor/ygorl/pyproject.toml.orig` holds the unmodified file.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "ygorl"
UPSTREAM_THIRD_PARTY = VENDOR / "third_party"

# Point the engine at the merged card database when it has been built.
# BabelCDB ships recent sets as separate release-*.cdb files that ygorl does
# not load, and a deck containing one of those cards segfaults the solver
# rather than failing cleanly. See skills/carddb.py.
_SHADOW = ROOT / "vendor" / "third_party_merged"
THIRD_PARTY = _SHADOW if (_SHADOW / "BabelCDB" / "cards.cdb").exists() \
    else UPSTREAM_THIRD_PARTY
SOLVER_BIN = VENDOR / "build" / "combo-solver" / "bin" / "combosolver"
PYTHON = ROOT / "venv" / "bin" / "python"
# The console script, not `python -m ygorl.cli`: cli.py defines main()
# but has no __main__ guard, so -m runs the module and exits silently.
YGORL_BIN = ROOT / "venv" / "bin" / "ygorl"

# The solver's per-hand time budget. NOT a free knob, and NOT upstream's 10 s.
#
# Upstream measured false bricks at 5 s (+17 to +19 points on long-combo
# decks) and settled on 10 s. Measured here, 10 s is still too low: on
# Snake-Eye into Snake-Eyes Flamberge Dragon it reports 3/12 bricks where
# 60 s and 120 s both report 2/12. The extra "brick" solves in 9 actions
# given 28.5 s, and its Ash survival flips from dead to 2/4 -- so the
# handtrap matrix is budget-sensitive too, not only the brick rate.
#
# 60 s is the converged budget for that deck. It is NOT a universal answer:
# run `gate.budget_ladder` per deck and use where the brick count settles.
# A cheaper run is a wrong run, not a faster one.
SOLVE_MS = 60_000

# Leave a core or two for the rest of the machine; the solver is CPU-only
# and runs one thread per worker.
DEFAULT_WORKERS = max(1, (os.cpu_count() or 4) - 2)


class EngineNotReady(RuntimeError):
    pass


def _export():
    """Set the ygorl env vars in THIS process, not only for subprocesses.

    ygorl resolves its data paths at call time from the environment, so
    anything that imports ygorl directly (Workdir.create, Replay,
    convert_line) needs them set here too. Without it the in-process
    calls raise "third_party/ data not found" while subprocesses work
    fine -- a confusing split.
    """
    os.environ.setdefault("YGORL_THIRD_PARTY", str(THIRD_PARTY))
    os.environ.setdefault("YGORL_COMBO_SOLVER", str(SOLVER_BIN))


_export()


def env():
    """Environment for any ygorl subprocess."""
    e = dict(os.environ)
    e["YGORL_THIRD_PARTY"] = str(THIRD_PARTY)
    # ygorl derives the solver build directory from third_party's parent, so
    # pointing it at the shadow tree also moves where it looks for the
    # binary. Name it explicitly.
    e["YGORL_COMBO_SOLVER"] = str(SOLVER_BIN)
    return e


def check():
    """Return a list of setup problems. Empty means ready."""
    problems = []
    if not VENDOR.exists():
        problems.append(f"ygorl not cloned at {VENDOR}")
    if not (_SHADOW / "BabelCDB" / "cards.cdb").exists():
        problems.append("merged card database not built (carddb.build()); "
                        "recent-set cards will segfault the solver")
    if not (THIRD_PARTY / "BabelCDB" / "cards.cdb").exists():
        problems.append("card database missing (git submodule update --init --recursive)")
    if not (THIRD_PARTY / "CardScripts").exists():
        problems.append("CardScripts missing (git submodule update --init --recursive)")
    if not YGORL_BIN.exists():
        problems.append("ygorl console script missing from the venv")
    if not SOLVER_BIN.exists():
        problems.append(f"combo solver not built (bash {VENDOR}/tools/build_combo_solver.sh)")
    try:
        import ygorl  # noqa: F401
    except ImportError:
        problems.append("ygorl not installed into the venv")
    return problems


def require():
    problems = check()
    if problems:
        raise EngineNotReady("; ".join(problems))


# --- version pinning ------------------------------------------------------

def _git_rev(path):
    # Only report a revision for an actual repository root. The shadow
    # third_party tree is a plain directory, and `git rev-parse` there walks
    # up and returns duelroom's own HEAD -- a version pin that looks valid
    # and means nothing.
    path = Path(path)
    if not (path / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def versions():
    """Component revisions, recorded on every run.

    These move independently and a mismatch changes results silently rather
    than erroring, so every `runs` row carries them.
    """
    return {
        "ygorl": _git_rev(VENDOR),
        "ygopro_core": _git_rev(UPSTREAM_THIRD_PARTY / "ygopro-core"),
        "card_scripts": _git_rev(UPSTREAM_THIRD_PARTY / "CardScripts"),
        "babel_cdb": _git_rev(UPSTREAM_THIRD_PARTY / "BabelCDB"),
        "lflists": _git_rev(UPSTREAM_THIRD_PARTY / "LFLists"),
        "combo_solver": "e0c7221802a2",  # pinned by tools/build_combo_solver.sh
        "cards_merged": THIRD_PARTY is _SHADOW,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


# --- the two calls duelroom needs ----------------------------------------

def duel(deck_a, deck_b, seed=0, agent_a="greedy", agent_b="greedy",
         first="a", max_turns=200, replay=None, timeout=900):
    """Play one headless game. Deterministic in `seed`.

    Used for Stage 4+ gauntlets. The solitaire work goes through
    `solve_blocking` instead -- it needs no opponent at all.
    """
    require()
    # Resolved: subprocesses run with cwd=VENDOR, so relative paths from
    # the caller would be looked up in the wrong directory.
    cmd = [
        str(YGORL_BIN), "duel",
        str(Path(deck_a).resolve()), str(Path(deck_b).resolve()),
        "--seed", str(seed), "--agent-a", agent_a, "--agent-b", agent_b,
        "--first", first, "--max-turns", str(max_turns),
    ]
    if replay:
        cmd += ["--save-replay", str(replay)]
    out = subprocess.run(cmd, capture_output=True, text=True, env=env(),
                         cwd=str(VENDOR), timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"duel failed: {out.stderr.strip()[-500:]}")
    return _parse_duel(out.stdout)


def _parse_duel(text):
    result = {"raw": text}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in (
            "winner", "reason", "turns", "lp", "decisions"
        ):
            result[parts[0]] = parts[1].strip()
    for k in ("turns", "decisions"):
        if k in result:
            try:
                result[k] = int(result[k])
            except ValueError:
                pass
    return result


def solve_blocking(decks, hands=8, solve_ms=SOLVE_MS, workers=None, pieces=3,
                   seed=0, first_hand=0, out=None, summary=None, timeout=None):
    """Turn-1 solitaire line search towards the best blocking board.

    This is the core of the whole project. For each opening hand ygorl reads
    the deck's own card scripts, plans targets (interrupting traps and
    quick-play spells set, hand traps kept, plus the deck's field
    interrupters), solves for a line to each, verifies every line by fresh
    replay in the rules core, and keeps the one whose end board carries the
    most interruptions.

    No opponent and no AI: searching your own turn to its best board *is*
    optimal piloting. Results are appended as they finish, so a rerun
    resumes rather than restarting.

    Returns the parsed summary dict.
    """
    require()
    decks = [decks] if isinstance(decks, (str, Path)) else list(decks)
    preflight(decks)
    workers = workers or DEFAULT_WORKERS
    out = Path(out) if out else ROOT / "results" / "blocking_demos.jsonl"
    summary = Path(summary) if summary else ROOT / "results" / "blocking_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "nice", "-n", "19", str(PYTHON), "tools/solve_blocking.py",
        *[str(Path(d).resolve()) for d in decks],
        "--hands", str(hands), "--solve-ms", str(solve_ms),
        "--workers", str(workers), "--pieces", str(pieces),
        "--seed", str(seed), "--first-hand", str(first_hand),
        "--out", str(out), "--summary", str(summary),
    ]
    subprocess.run(cmd, env=env(), cwd=str(VENDOR), timeout=timeout, check=True)
    return json.loads(summary.read_text()) if summary.exists() else {}


def preflight(decks):
    """Refuse decks the engine cannot load, before spending solver time.

    An unknown passcode does not produce a clean error. The solver
    segfaults and buries the real cause under five unrelated diagnostics,
    and every hand "fails" in about a second -- which reads like a deck
    that simply bricks. One elfnote run burned 12 hands that way. Cheap to
    check, expensive to miss.
    """
    from . import carddb
    from .deck import Deck

    problems = []
    for d in decks:
        path = Path(d)
        bad = carddb.check_deck(Deck.from_ydk(path))
        if bad:
            codes = ", ".join(f"{c} ({sec})" for c, sec in bad)
            problems.append(f"{path.stem}: {len(bad)} card(s) not in the "
                            f"engine database — {codes}")
    if problems:
        raise EngineNotReady(
            "deck(s) the engine cannot load:\n  " + "\n  ".join(problems)
            + "\n(try rebuilding the merged database: carddb.build())"
        )


def summarise(summary):
    """Flatten a solve_blocking summary into per-deck rows."""
    rows = []
    for name, d in (summary.get("decks") or {}).items():
        inter = d.get("interruptions") or []
        hands = d.get("hands") or 0
        solved = d.get("solved") or 0
        rows.append({
            "deck": name,
            "hands": hands,
            "solved": solved,
            "solve_rate": solved / hands if hands else 0.0,
            "mean_interruptions": sum(inter) / len(inter) if inter else 0.0,
            "interruptions": inter,
            "s_per_hand": (d.get("wall_s") or 0) / hands if hands else 0.0,
        })
    return sorted(rows, key=lambda r: -r["mean_interruptions"])
