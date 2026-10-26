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


def test_doctor_does_not_re_enter_the_test_suite():
    """`doctor` must not shell out to pytest from inside pytest.

    The guard block runs `venv/bin/pytest tests/ -q` as a subprocess, and
    this file calls `cmd_doctor` -- so each doctor started a suite that
    started a doctor. It looked harmless only because the inner run died
    instantly on `import daemon` (no conftest); once collection worked the
    recursion took the machine's load average to 13.

    Asserting on the printed line rather than on the environment check,
    because the line is what proves the subprocess was skipped.
    """
    conn = db.connect()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        daemon.COMMANDS["doctor"](conn, [])
    out = buf.getvalue()
    assert "regression tests" in out
    assert "already running under pytest" in out, (
        "doctor ran the suite recursively -- see the GUARDS block in "
        "cmd_doctor")


def _deck_names():
    from skills import engine
    return sorted(p.stem for p in (engine.ROOT / "assets" / "decks").glob("*.ydk"))


@pytest.mark.parametrize("deck", _deck_names())
def test_report_does_not_raise_on_any_deck(deck):
    """`report` was walked only with no arguments, i.e. only ever against
    the active deck, and the active deck happened to miss both crashes
    living in it:

      * `cmd_report` unpacked `identify_archetype`'s rows as 3-tuples
        after a fourth field was added, so every deck with an
        ALTERNATIVE archetype guess raised ValueError -- 6 of the 12
        deck files. The active deck has one guess, so it passed.
      * `blind` was bound inside `if live:` but read from the
        UNMEASURED loop, so a deck with no live handtrap and at least
        one unmeasured one raised UnboundLocalError. `Blitzclique` did.

    The dispatcher catches both, prints a traceback and exits 0, which
    is why nothing noticed. A deck file is cheap to walk; this is the
    same argument that put the CLI smoke walk here in the first place.
    """
    fn = daemon.COMMANDS["report"]
    conn = db.connect()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(conn, [deck])


def test_report_prints_a_blind_spot_note_once_per_deck():
    """The blind-spot loop sat INSIDE the unmeasured loop, so it printed
    once per unmeasured row and not at all when there were none. branded
    printed the Maxx "C" note twice; kewl-tune, despia, elfnote,
    fiendsmith, sky-striker and light-and-darkness-ritual -- every deck
    whose handtraps were all live -- printed it zero times, which is
    where a blind spot most needs saying."""
    conn = db.connect()
    for deck in ("branded", "kewl-tune"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            daemon.COMMANDS["report"](conn, [deck])
        n = buf.getvalue().count('! Maxx "C": lines convert')
        assert n == 1, f"{deck} printed the blind-spot note {n} times"
