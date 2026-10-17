"""YGOPRODeck card database mirror.

The official API serves card data only -- there is no decklist endpoint (that
is meta_scrape.py's problem). Their rate limit is 20 requests/second and
exceeding it blocks the IP for an hour, so the correct access pattern is one
bulk fetch of the whole pool, cached locally, refreshed only when the upstream
database version changes.
"""

import json
from datetime import datetime, timezone

import httpx

from . import db

API_BASE = "https://db.ygoprodeck.com/api/v7"
CARDINFO = f"{API_BASE}/cardinfo.php"
CHECK_VER = f"{API_BASE}/checkDBVer.php"

TIMEOUT = httpx.Timeout(120.0, connect=15.0)
USER_AGENT = "duelroom/0.1 (personal deck analysis; local cache)"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def remote_version():
    """Upstream database version, or None if the check fails."""
    try:
        r = httpx.get(CHECK_VER, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        return str(data.get("database_version") or data.get("last_update") or "")
    return None


def needs_refresh(conn):
    """True if the local mirror is empty or behind upstream."""
    n = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
    if n == 0:
        return True, "local cache empty"
    remote = remote_version()
    if remote is None:
        return False, "version check unavailable, keeping cache"
    local = db.get_meta(conn, "cards_db_version")
    if local != remote:
        return True, f"upstream {remote} != local {local}"
    return False, f"up to date at {remote}"


def fetch_all():
    """One bulk GET of the entire card pool."""
    r = httpx.get(
        CARDINFO,
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    r.raise_for_status()
    payload = r.json()
    if "data" not in payload:
        raise RuntimeError(f"unexpected cardinfo payload: {list(payload)[:5]}")
    return payload["data"]


def _row(card, fetched):
    ban = card.get("banlist_info") or {}
    return (
        card["id"],
        card.get("name"),
        card.get("type"),
        card.get("frameType"),
        card.get("race"),
        card.get("attribute"),
        card.get("level"),
        card.get("atk"),
        card.get("def"),
        card.get("linkval"),
        card.get("scale"),
        card.get("archetype"),
        card.get("desc"),
        ban.get("ban_tcg"),
        ban.get("ban_ocg"),
        fetched,
    )


def store(conn, cards):
    """Replace the local mirror with a freshly fetched pool."""
    fetched = _now()
    rows = [_row(c, fetched) for c in cards]

    # Alternate passcodes: a .ydk may reference any printing's id, so every
    # id in card_images must resolve back to the canonical card.
    #
    # Best-effort only. This yields ~172 aliases, which is YGOPRODeck's
    # alt-artwork list -- NOT the same set as EDOPro's passcode aliases. The
    # authoritative source is the `alias` column of cards.cdb (BabelCDB),
    # which Stage 2 pulls in anyway; fold it in here once it exists.
    aliases = []
    for c in cards:
        for img in c.get("card_images") or []:
            alt = img.get("id")
            if alt and alt != c["id"]:
                aliases.append((alt, c["id"]))

    with conn:
        conn.execute("DELETE FROM card_aliases")
        conn.executemany(
            """INSERT INTO cards
               (id,name,type,frame,race,attribute,level,atk,def,linkval,
                scale,archetype,text,banlist_tcg,banlist_ocg,last_fetched)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, type=excluded.type, frame=excluded.frame,
                 race=excluded.race, attribute=excluded.attribute,
                 level=excluded.level, atk=excluded.atk, def=excluded.def,
                 linkval=excluded.linkval, scale=excluded.scale,
                 archetype=excluded.archetype, text=excluded.text,
                 banlist_tcg=excluded.banlist_tcg,
                 banlist_ocg=excluded.banlist_ocg,
                 last_fetched=excluded.last_fetched""",
            rows,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO card_aliases (alias_id, card_id) VALUES (?,?)",
            aliases,
        )
    return len(rows), len(aliases)


def sync(conn, force=False):
    """Refresh the mirror if upstream has moved. Returns a status string."""
    if not force:
        refresh, why = needs_refresh(conn)
        if not refresh:
            n = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
            return f"{n} cards cached ({why})"
    cards = fetch_all()
    n_cards, n_alias = store(conn, cards)
    version = remote_version()
    with conn:
        if version:
            db.set_meta(conn, "cards_db_version", version)
        db.set_meta(conn, "cards_synced_at", _now())
    return f"synced {n_cards} cards, {n_alias} alternate passcodes (version {version})"


# --- lookup helpers -------------------------------------------------------

def resolve(conn, card_id):
    """Map any printing's passcode to the canonical card row."""
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if row:
        return row
    alias = conn.execute(
        "SELECT card_id FROM card_aliases WHERE alias_id = ?", (card_id,)
    ).fetchone()
    if alias:
        return conn.execute(
            "SELECT * FROM cards WHERE id = ?", (alias["card_id"],)
        ).fetchone()
    return None


def by_name(conn, name):
    return conn.execute(
        "SELECT * FROM cards WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def banlist_limit(conn, card_id, region="tcg"):
    """Max legal copies of a card: 0, 1, 2, or 3."""
    row = resolve(conn, card_id)
    if row is None:
        return 3
    status = row["banlist_tcg"] if region == "tcg" else row["banlist_ocg"]
    return {"Banned": 0, "Limited": 1, "Semi-Limited": 2}.get(status, 3)
