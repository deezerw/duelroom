"""Put the repo root on `sys.path` for every test module.

`venv/bin/pytest tests/ -q` -- the command BACKLOG.md and the docstring of
`test_regressions.py` both tell you to run -- collected nothing but an
ImportError on `import daemon`. Only `venv/bin/python -m pytest` worked,
because that form puts the current directory on `sys.path` and the console
script does not. `test_regressions.py` and `test_web.py` hid the problem by
each doing the insert themselves; `test_cli.py` did not, so the whole run
aborted at collection.

A conftest here is loaded before any test module is imported, so the path is
right whichever way pytest is invoked.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
