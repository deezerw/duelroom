"""Has a new TCG banlist come out? (daily check, `daemon.py banlist`)

The ENGINE plays by the Project Ignis list it was built with
(third_party/LFLists/0TCG.lflist.conf, the submodule). Upstream publishes the
next list there first, so the check reads upstream's 0TCG.lflist.conf over
HTTPS -- the submodule is never touched -- and compares its header ("!2026.10
TCG") with the engine's. A list not seen before is recorded in meta
`banlist_new` with what changed and which of our decks break it, for the web
banner; `daemon.py banlist ack` clears the banner.

Adopting it is deliberately NOT automatic: the engine's list, CardScripts and
BabelCDB move together (CLAUDE.md, "Bump BabelCDB and CardScripts together"),
then `daemon.py setup` and the gate -- and every stored measurement is
against the old list. The card mirror's own banlist (YGOPRODeck, used by
`check`) is refreshed here too; it is version-checked and costs nothing when
upstream has not moved.
"""

import json
from datetime import datetime, timezone

import httpx

from . import card_db, db, engine
from .deck import Deck

UPSTREAM = "https://raw.githubusercontent.com/ProjectIgnis/LFLists/master/0TCG.lflist.conf"
LOCAL = engine.UPSTREAM_THIRD_PARTY / "LFLists" / "0TCG.lflist.conf"
DECKS = engine.DECKS
WORDS = {0: "Forbidden", 1: "Limited", 2: "Semi-Limited", 3: "Unlimited"}


def parse(text):
    """(name, {code: limit}) of an EDOPro .lflist.conf; codes absent = 3."""
    name, limits = None, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("!") and name is None:
            name = line[1:].strip()
        elif line[:1].isdigit():
            parts = line.split()
            if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
                limits[int(parts[0])] = int(parts[1])
    return name, limits


def fetch_upstream():
    r = httpx.get(UPSTREAM, timeout=30, follow_redirects=True,
                  headers={"User-Agent": card_db.USER_AGENT})
    r.raise_for_status()
    return parse(r.text)


def changes(old, new):
    """[(code, old limit, new limit)] for every card whose limit moved."""
    return sorted((c, old.get(c, 3), new.get(c, 3)) for c in set(old) | set(new)
                  if old.get(c, 3) != new.get(c, 3))


def deck_hits(limits, decks_dir=DECKS):
    """{deck: [(code, copies, limit)]} for decks running more copies of a
    card than `limits` allows (main + extra + side, as the TCG counts)."""
    out = {}
    for path in sorted(decks_dir.glob("*.ydk")):
        d = Deck.from_ydk(path)
        counts = {}
        for c in [*d.main, *d.extra, *d.side]:
            counts[int(c)] = counts.get(int(c), 0) + 1
        bad = [(c, n, limits[c]) for c, n in sorted(counts.items())
               if c in limits and n > limits[c]]
        if bad:
            out[path.stem] = bad
    return out


def check(conn, upstream=None, now=None):
    """Compare upstream with the engine's list; record a new one. Returns
    the notice dict when upstream is a list we have not seen, else None."""
    local_name, local = parse(LOCAL.read_text(encoding="utf-8"))
    name, new = upstream if upstream is not None else fetch_upstream()
    with conn:
        db.set_meta(conn, "banlist_checked_at", now or datetime.now(timezone.utc)
                    .isoformat(timespec="seconds"))
        db.set_meta(conn, "banlist_upstream", name)
    if not name or name == local_name or name == db.get_meta(conn, "banlist_seen"):
        return None
    note = {"name": name, "engine": local_name,
            "found_at": db.get_meta(conn, "banlist_checked_at"),
            "changes": changes(local, new), "decks": deck_hits(new)}
    with conn:
        db.set_meta(conn, "banlist_new", json.dumps(note))
        db.set_meta(conn, "banlist_seen", name)
    return note


def notice(conn):
    raw = db.get_meta(conn, "banlist_new")
    return json.loads(raw) if raw else None


def ack(conn):
    with conn:
        conn.execute("DELETE FROM meta WHERE key='banlist_new'")
