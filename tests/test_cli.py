"""A smoke walk over the read-only CLI commands.

`tests/test_web.py` walks every GET route, and that walk has caught two
real 500s this week. The CLI had no equivalent, and `daemon.run`'s
dispatcher wraps every command in `except Exception: traceback.print_exc()`
-- so a broken command prints a traceback and the process still exits 0.

That is exactly how `report`, the project's headline command, stayed
broken through a commit: the survival fix (#49) widened `surv_rows` from a
3-tuple to a 5-tuple and left a second, stale unpack site behind, so
`report` raised ValueError on every run while the suite stayed green.

These call the `cmd_*` functions DIRECTLY rather than through the
dispatcher, because the dispatcher is what hides the failure.
"""

import io
import contextlib

import pytest

import daemon
from skills import db


# Excluded, with reasons -- not an oversight:
#   cards, meta, images     network calls to YGOPRODeck
#   setup                   rebuilds the merged card database
#   solve ladder gate       minutes to hours of solver
#   boards optimize second
#   gauntlet floodgates
#   pilot                   interactive
#   import export           writes files
#   snapshot                writes a snapshot row
#   threats                 rebuilds the special-summon cache (a file write)
READ_ONLY = [
    ("decks", []),
    ("mydeck", []),
    ("engine", []),
    ("doctor", []),
    ("archetypes", []),
    ("locals", []),
    ("targets", []),
    ("draws", []),
    ("hand", []),
    ("check", []),
    ("ydke", []),
    ("report", []),
    ("matrix", []),
    ("drill", []),
    ("rescore", []),
    ("flex", ["Branded"]),
    ("drift", ["Branded"]),
]


@pytest.mark.parametrize("name,args", READ_ONLY, ids=[c for c, _ in READ_ONLY])
def test_read_only_command_does_not_raise(name, args):
    fn = daemon.COMMANDS.get(name)
    assert fn is not None, f"{name} is not a registered command"
    conn = db.connect()
    buf = io.StringIO()
    # Output is irrelevant here; raising is the failure.
    with contextlib.redirect_stdout(buf):
        fn(conn, args)


def test_every_command_is_either_walked_or_excluded():
    """No command may be silently absent from this file.

    A new command that is neither walked nor named in the exclusion list
    would get no smoke coverage and nobody would notice.
    """
    walked = {c for c, _ in READ_ONLY}
    excluded = {
        "cards", "meta", "images", "setup", "solve", "ladder", "gate",
        "boards", "optimize", "second", "gauntlet", "floodgates", "pilot",
        "import", "export", "snapshot", "threats",
    }
    registered = set(daemon.COMMANDS)
    missing = registered - walked - excluded
    assert not missing, (
        f"{sorted(missing)} are registered but neither walked nor "
        "excluded -- add them to READ_ONLY or to the exclusion set with a "
        "reason")
