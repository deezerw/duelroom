#!/usr/bin/env python3
"""Fail on undefined names anywhere in the project.

Two bugs in one session were a NameError that no test could see:

  skills/sweep.py: `import shutil` was never added, because the edit was
  anchored on text that had already changed. Every call to `_solve_one`
  then died in its `finally` -- breaking every sweep AND the optimiser's
  brick re-check -- while the test written for that very commit passed,
  because it only grepped the source for "rmtree(scratch".

  An hour later the identical mistake dropped `import contextlib` and
  broke importing the module at all.

A structural assertion cannot catch a missing import, and the functions
that broke are stubbed out of every other test, so nothing ever called
them. pyflakes finds both, by name and line, in under a second.

Only UNDEFINED NAMES fail this check. Unused imports, unused locals and
f-strings without placeholders are reported for information and do not
fail, so the gate stays actionable rather than becoming noise to ignore.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["skills", "daemon.py", "web", "tools"]


def main() -> int:
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pyflakes", *TARGETS],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"could not run pyflakes: {exc}")
        return 0  # a missing linter must not fail the build
    lines = [l for l in (out.stdout or "").splitlines() if l.strip()]
    fatal = [l for l in lines if "undefined name" in l]
    other = [l for l in lines if l not in fatal]
    if fatal:
        print(f"{len(fatal)} UNDEFINED NAME(S) — these are NameErrors waiting "
              f"to happen:")
        for l in fatal:
            print(f"  ! {l}")
        return 1
    print(f"no undefined names in {', '.join(TARGETS)} "
          f"({len(other)} non-fatal note(s): unused imports, unused locals, "
          f"f-strings without placeholders)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
