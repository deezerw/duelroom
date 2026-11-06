"""Merged card database for the engine.

BabelCDB is not one file. `cards.cdb` holds the long-standing pool, and each
recent set ships as its own `release-*.cdb`. ygorl resolves exactly one path
(`paths.cards_cdb()`), so cards from a newly released set are invisible to
both the Python side and the C++ solver -- which symlinks that same file
into its workdir.

That bites immediately, because the whole point of this project is scraping
the *current* meta. An "Ars Magna Elfnote" list pulled from tournament
results in October 2026 contains six cards that live in `release-betb.cdb`:

    4063756   Medicurius the Power Patron of Illusions
    36270527  Ars Magna of Infinity and Finity
    37279096  Ars Magna - "Citrinitas"
    62368221  Ars Magna of Purification and Corruption
    90875418  Theorealize Medius
    99753860  Ars Magna of Unification and Separation

The failure mode is nasty: the solver **segfaults** (exit -11) and reports
"the target player's turn did not end", "enumerator coverage: NO decision
observed", and three more red herrings before the real line, "codes missing
from cards.cdb". All 12 hands failed in 0.9 s each while every other deck
took 75-170 s. Silent-ish, fast, and easy to mistake for a deck that bricks.

The fix is a merged database in a shadow `third_party/` tree: real
directories for BabelCDB, symlinks for everything else, and
`YGORL_THIRD_PARTY` pointed at it. The submodule is never modified, so
`git pull` in BabelCDB stays clean.

Pre-release sets are excluded by default -- they are not tournament legal in
the formats we analyse and their scripts can be provisional.

ONE EXCEPTION, found 2026-11-06 on an "Angelechy-Meta" list: a card can be
out and in tournament lists (YGOPRODeck gives it its official passcode) while
BabelCDB still carries it ONLY as a pre-release card under a placeholder
passcode (Angelechy Verdict: official 48340269, BabelCDB 101402096). The list
then fails preflight although the card is fully scripted. `build` therefore
brings in exactly those cards: a pre-release card whose NAME the YGOPRODeck
mirror knows under a different, official passcode gets a copy of its rows
under that passcode, and the shadow CardScripts gets `c<official>.lua`
pointing at its script. The core loads a script by the card's own code
(an alias only counts within 10, for alternate art) and `GetID()` returns
the code it was loaded under, so the copy behaves as the card. Nothing else
from the pre-release sets comes in.
"""

import shutil
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "ygorl"
UPSTREAM = VENDOR / "third_party"
SHADOW = ROOT / "vendor" / "third_party_merged"

LINKED = ("ygopro-core", "LFLists")
MIRROR = ROOT / "data" / "duelroom.db"     # the YGOPRODeck card mirror (card_db)
TABLES = ("datas", "texts")


def extra_cdbs(include_prerelease=False):
    """Set databases to fold into the base, oldest-first."""
    babel = UPSTREAM / "BabelCDB"
    out = sorted(babel.glob("release-*.cdb"))
    if include_prerelease:
        out += sorted(babel.glob("prerelease-*.cdb"))
    return out


def build(include_prerelease=False, force=False):
    """Create (or refresh) the shadow third_party tree. Returns its path."""
    base = UPSTREAM / "BabelCDB" / "cards.cdb"
    if not base.exists():
        raise FileNotFoundError(f"{base} missing — submodules not initialised")

    SHADOW.mkdir(parents=True, exist_ok=True)
    for name in LINKED:
        link = SHADOW / name
        if link.is_symlink() or link.exists():
            if not force:
                continue
            link.unlink()
        link.symlink_to(UPSTREAM / name)

    babel = SHADOW / "BabelCDB"
    babel.mkdir(exist_ok=True)
    merged = babel / "cards.cdb"

    # Everything else in BabelCDB (other .cdb files, .conf) is linked through
    # so the directory still looks like the real one to anything that reads it.
    for f in (UPSTREAM / "BabelCDB").iterdir():
        if f.name == "cards.cdb" or f.name.startswith("."):
            continue
        link = babel / f.name
        if not (link.is_symlink() or link.exists()):
            link.symlink_to(f)

    _link_scripts(SHADOW / "CardScripts", force)

    shutil.copy2(base, merged)
    added = {}
    conn = sqlite3.connect(merged)
    try:
        for extra in extra_cdbs(include_prerelease):
            conn.execute("ATTACH DATABASE ? AS src", (str(extra),))
            n = 0
            for table in TABLES:
                try:
                    # INSERT OR REPLACE: later databases win, matching
                    # CardDB.load's documented "later databases override".
                    cur = conn.execute(
                        f"INSERT OR REPLACE INTO main.{table} "
                        f"SELECT * FROM src.{table}"
                    )
                    n = max(n, cur.rowcount)
                except sqlite3.Error:
                    pass  # not every set db carries every table
            conn.commit()
            conn.execute("DETACH DATABASE src")
            added[extra.name] = n
        released = official_prereleases(conn)
        if released:
            added[f"{len(released)} released pre-release card(s)"] = len(released)
        _script_aliases(SHADOW / "CardScripts", released)
        total = conn.execute("SELECT COUNT(*) FROM datas").fetchone()[0]
    finally:
        conn.close()
    return SHADOW, added, total


def _link_scripts(shadow_scripts, force=False):
    """The shadow CardScripts: a REAL directory of symlinks to every upstream
    entry, so released pre-release cards can get a script under their
    official passcode without touching the submodule. (It used to be one
    symlink to upstream; that is replaced here.)"""
    up = UPSTREAM / "CardScripts"
    if shadow_scripts.is_symlink():
        shadow_scripts.unlink()
    shadow_scripts.mkdir(exist_ok=True)
    for f in up.iterdir():
        if f.name.startswith("."):
            continue
        link = shadow_scripts / f.name
        if link.is_symlink() and (force or not link.exists()):
            link.unlink()
        if not (link.is_symlink() or link.exists()):
            link.symlink_to(f)
    # Aliases from an earlier build: rewritten by _script_aliases.
    for old in shadow_scripts.glob("c*.lua"):
        if old.is_symlink() and "pre-release" in str(old.readlink()):
            old.unlink()


def official_prereleases(conn, mirror=None):
    """Copy every pre-release card YGOPRODeck already lists under an OFFICIAL
    passcode into the merged db (open as `conn`) under that passcode.
    Returns [(official, placeholder, name)] (module docstring)."""
    mirror = Path(mirror or MIRROR)
    if not mirror.exists():
        return []
    m = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
    try:
        official = {}
        for code, name in m.execute("SELECT id, name FROM cards"):
            official.setdefault(name, code)
    finally:
        m.close()
    done = []
    for pre in sorted((UPSTREAM / "BabelCDB").glob("prerelease-*.cdb")):
        conn.execute("ATTACH DATABASE ? AS pre", (str(pre),))
        try:
            rows = conn.execute("SELECT d.id, t.name FROM pre.datas d "
                                "JOIN pre.texts t ON t.id = d.id").fetchall()
            for placeholder, name in rows:
                code = official.get(name)
                if not code or code == placeholder:
                    continue
                if conn.execute("SELECT 1 FROM main.datas WHERE id=?",
                                (code,)).fetchone():
                    continue    # BabelCDB has the real card already: it wins
                for table in TABLES:
                    cols = [r[1] for r in conn.execute(f"PRAGMA pre.table_info({table})")]
                    sel = ", ".join("?" if c == "id" else c for c in cols)
                    conn.execute(f"INSERT OR REPLACE INTO main.{table} ({', '.join(cols)}) "
                                 f"SELECT {sel} FROM pre.{table} WHERE id=?",
                                 (code, placeholder))
                done.append((code, placeholder, name))
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE pre")
    return done


def _script_aliases(shadow_scripts, released):
    """`c<official>.lua` -> the placeholder's script, at the CardScripts
    root (searched first). A card with no script stays without one, and
    preflight's script check reports it."""
    for code, placeholder, _name in released:
        src = UPSTREAM / "CardScripts" / "pre-release" / f"c{placeholder}.lua"
        link = shadow_scripts / f"c{code}.lua"
        if src.exists() and not link.exists():
            link.symlink_to(src)


def source_revision():
    try:
        out = subprocess.run(
            ["git", "-C", str(UPSTREAM / "BabelCDB"), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def ready():
    return (SHADOW / "BabelCDB" / "cards.cdb").exists()


def missing_codes(codes, cdb=None):
    """Which of these passcodes the engine's database does not know.

    Call this before spending solver time. A deck containing an unknown card
    does not fail cleanly -- it segfaults the solver after a cascade of
    misleading diagnostics.
    """
    cdb = Path(cdb) if cdb else (SHADOW if ready() else UPSTREAM) / "BabelCDB" / "cards.cdb"
    conn = sqlite3.connect(f"file:{cdb}?mode=ro", uri=True)
    try:
        known = set()
        for chunk in (list(codes)[i:i + 400] for i in range(0, len(list(codes)), 400)):
            q = ",".join("?" * len(chunk))
            known |= {
                r[0] for r in conn.execute(
                    f"SELECT id FROM datas WHERE id IN ({q})", chunk
                )
            }
        # alias: alternate artworks resolve to a known original
        return sorted(set(codes) - known)
    finally:
        conn.close()


def check_deck(deck, cdb=None):
    """[(code, section)] for cards the engine cannot load. Empty means safe."""
    bad = []
    for section in ("main", "extra", "side"):
        codes = getattr(deck, section)
        for code in missing_codes(set(codes), cdb):
            bad.append((code, section))
    return bad
