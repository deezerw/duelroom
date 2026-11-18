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

# The tests run on PINNED deck copies (tests/fixtures/decks), copied fresh into
# a temp dir for the session: the user deleting decks in the UI broke 33 tests
# that read assets/decks directly (2026-11-18), and nothing a test writes may
# land among the user's decks. Set before any skills module is imported.
import atexit
import os
import shutil
import tempfile

_decks = Path(tempfile.mkdtemp(prefix="duelroom-test-decks-"))
shutil.copytree(Path(__file__).resolve().parent / "fixtures" / "decks", _decks,
                dirs_exist_ok=True)
os.environ["DUELROOM_DECKS"] = str(_decks)
atexit.register(shutil.rmtree, _decks, True)
