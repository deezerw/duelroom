#!/usr/bin/env python
"""Find code that reads record fields which never exist.

Three bugs in this project had the same shape: code reads a key off a
solver record, the key is never present, the default silently stands in,
and the result looks confident and is meaningless. Examples --
`line["interruptions"]` (always None; counts live in the summary) made
"best board first" return file order while claiming to be sorted;
`_zone_rank` is stripped before the file is written, so a check against
a saved spec could never fire.

None of them raised. Reading the code does not reveal them; comparing
what the code reads against what the data actually contains does.

    venv/bin/python tools/audit_fields.py
"""

import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Modules that parse solver/engine records, checked against those records.
# Scrapers are excluded: they read API payloads, which are a different shape.
PARSERS = [
    "skills/gate.py", "skills/drill.py", "skills/scenarios.py",
    "skills/breaking.py", "skills/optimizer.py", "skills/report.py",
]

# Keys that are intentionally in-memory only.
EXPECTED_ABSENT = {"_zone_rank", "_mean_actions", "_extra", "_built_targets"}


def keys_read():
    out = {}
    for rel in PARSERS:
        path = ROOT / rel
        if not path.exists():
            continue
        for m in re.finditer(r'\.get\(\s*"([a-z_0-9]+)"', path.read_text()):
            out.setdefault(m.group(1), set()).add(rel)
    return out


def keys_present():
    found = set()

    def walk(obj, depth=0):
        if depth > 8:
            return
        if isinstance(obj, dict):
            found.update(obj.keys())
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, depth + 1)

    for f in glob.glob(str(ROOT / "results" / "*.jsonl")):
        for line in Path(f).read_text().splitlines():
            if line.strip():
                try:
                    walk(json.loads(line))
                except json.JSONDecodeError:
                    pass
    for f in glob.glob(str(ROOT / "results" / "*.json")):
        try:
            walk(json.loads(Path(f).read_text()))
        except Exception:
            pass
    return found


def main():
    read, present = keys_read(), keys_present()
    if not present:
        print("no records in results/ — run a solve first, nothing to check against")
        return 0
    missing = {k: v for k, v in read.items()
               if k not in present and k not in EXPECTED_ABSENT}
    if not missing:
        print(f"clean: every field read by {len(PARSERS)} parser modules "
              f"appears in the stored records")
        return 0
    print("read by record parsers but NEVER present in any record:\n")
    for k, where in sorted(missing.items()):
        print(f"  {k:20} read in {', '.join(sorted(where))}")
    print("\nEach is a silent default. Check whether the value is meant to come "
          "from somewhere else (a summary file, a different nesting level).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
