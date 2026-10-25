#!/usr/bin/env python
"""Fail if the docs describe a CLI, a module or a constant that is not there.

CLAUDE.md drifted to documenting three commands that never existed
(`goldfish`, `scenarios`) while omitting twenty-six that do. A reader
following it would type things that are not there, which is the same
failure as a wrong number: confident, specific, and false.

The same drift hit the architecture diagram and the configuration table,
undetected, because this script only ever checked commands. As of
2026-10-25 it checks three more things:

  * every `skills/*.py` the docs name exists -- the diagram listed
    `skills/solver.py` and `skills/scoring.py`, neither of which has ever
    existed, and the config table sent readers to `scoring.py` for "board
    weights: negates, card advantage, ATK, GY resources" when board
    quality is not a weighted score at all;
  * every `skills/*.py` that exists is named -- ten were missing,
    including `gate.py` (a whole stage) and `config.py` (which holds the
    weights every ranking inherits);
  * every CONSTANT in the configuration table is defined at module level
    in the file that row names -- eight were not, among them a
    `card_db.CACHE_DAYS` describing a timer the cache does not use.

    venv/bin/python tools/check_docs.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# handled by the REPL loop rather than the dispatch table
BUILTIN = {"quit", "q", "exit", "help"}
# Named in the diagram as deliberate counter-examples ("there is no X"),
# so finding them absent is the point rather than a failure.
PHANTOMS = {"solver.py", "scoring.py"}


def _skills_modules(text):
    """Module files the docs name, and the ones that exist."""
    named = set(re.findall(r"skills/([a-z_]+\.py)", text))
    on_disk = {f.name for f in (ROOT / "skills").glob("*.py")
               if f.name != "__init__.py"}
    return named, on_disk


def _config_table(text):
    """(file, constant) pairs from the Key Configuration table."""
    pairs = []
    for row in re.findall(r"^\| `([a-z_]+\.py)` \| (.+?) \|\s*$", text, re.M):
        fname, cells = row
        for const in re.findall(r"`([A-Z][A-Z_0-9]+)`", cells):
            pairs.append((fname, const))
    return pairs


def main():
    src = (ROOT / "daemon.py").read_text()
    block = src[src.index("COMMANDS = {"):src.index("\n}", src.index("COMMANDS = {"))]
    real = set(re.findall(r'"([a-z]+)":', block)) | BUILTIN

    fails = []
    for doc in ("CLAUDE.md", "README.md"):
        text = (ROOT / doc).read_text()
        for cmd in set(re.findall(r"^\s*(?:duelroom> )?([a-z]+)[ <\n]", text, re.M)):
            # only judge words that look like command references
            if cmd in real or len(cmd) < 3:
                continue
            if re.search(rf"^\s*{cmd}\s+[<\[]", text, re.M) or \
               re.search(rf"^\s*duelroom> {cmd}\b", text, re.M):
                fails.append(f"{doc}: documents `{cmd}`, which the CLI does not have")

    # The architecture diagram and the configuration table.
    claude = (ROOT / "CLAUDE.md").read_text()
    named, on_disk = _skills_modules(claude)
    for mod in sorted(named - on_disk - PHANTOMS):
        fails.append(f"CLAUDE.md: names skills/{mod}, which does not exist")
    for mod in sorted(on_disk - named):
        fails.append(f"CLAUDE.md: skills/{mod} exists and is not documented")
    for mod in sorted(PHANTOMS & on_disk):
        fails.append(f"CLAUDE.md: says skills/{mod} does not exist, but it does")

    for fname, const in _config_table(claude):
        path = ROOT / "skills" / fname
        if not path.exists():
            fails.append(f"CLAUDE.md: config table row `{fname}` has no such file")
            continue
        if not re.search(rf"^{const}\s*(?::[^=]+)?=", path.read_text(), re.M):
            fails.append(
                f"CLAUDE.md: config table claims {fname}::{const}, "
                "which is not defined at module level")

    if fails:
        print(f"{len(fails)} documentation mismatch(es):\n")
        for f in sorted(set(fails)):
            print(f"  ! {f}")
        return 1
    print(f"docs reference only real commands ({len(real)} available), "
          f"all {len(on_disk)} skills modules, and "
          f"{len(_config_table(claude))} real constants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
