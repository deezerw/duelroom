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

# ...and on a SNAPSHOT of the database (sqlite's backup API: consistent under
# WAL while the live app writes). The web tests press job buttons; on the live
# database each press queued a real job the test then cancelled -- 111 runs of
# cancelled gate/ladder/optimize/solve rows by 2026-11-19, and a worker that
# polled in between would have started hours of solver on the server.
import sqlite3 as _sqlite3

_db_dir = Path(tempfile.mkdtemp(prefix="duelroom-test-db-"))
_live_db = Path(__file__).resolve().parent.parent / "data" / "duelroom.db"
_test_db = _db_dir / "duelroom.db"
if _live_db.exists():
    _src, _dst = _sqlite3.connect(_live_db), _sqlite3.connect(_test_db)
    _src.backup(_dst)
    _dst.close()
    _src.close()
os.environ["DUELROOM_DB"] = str(_test_db)
atexit.register(shutil.rmtree, _db_dir, True)
