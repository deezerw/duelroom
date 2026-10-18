#!/usr/bin/env python
"""Fail if the docs describe commands the CLI does not have.

CLAUDE.md drifted to documenting three commands that never existed
(`goldfish`, `scenarios`) while omitting twenty-six that do. A reader
following it would type things that are not there, which is the same
failure as a wrong number: confident, specific, and false.

    venv/bin/python tools/check_docs.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# handled by the REPL loop rather than the dispatch table
BUILTIN = {"quit", "q", "exit", "help"}


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

    if fails:
        print(f"{len(fails)} documentation mismatch(es):\n")
        for f in sorted(set(fails)):
            print(f"  ! {f}")
        return 1
    print(f"docs reference only real commands ({len(real)} available)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
