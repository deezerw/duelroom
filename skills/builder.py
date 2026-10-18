"""Card search and list editing for the deckbuilder.

Two things here are deliberate, and both exist because the easy version
of a deckbuilder lies.

**Ranking is by a lower confidence bound, not by the raw inclusion rate.**
A card in 100% of two scraped lists is not more established than a card in
85% of forty, but sorting on ``pct_lists`` says it is -- and the top of a
search box is exactly where a reader stops looking. Ranking on the Wilson
lower bound puts 34/40 (0.708) above 2/2 (0.342), which is the honest
order, and the raw rate is still displayed with its denominator so the
reader can see what it rests on.

**Legality is never computed here, or in the browser.** ``Deck.validate``
is the one implementation, and the builder asks it over HTTP on every
change. A second copy of the banlist in JavaScript would drift from the
CLI's answer the first time a list updated, and the builder would cheerfully
show a legal deck that the engine refuses.
"""

import re

from . import card_db, stats
from .deck import EXTRA_FRAMES, SECTIONS, Deck

# Cards whose section is forced by their frame.
TOKEN_FRAMES = {"token", "skill"}

LIMIT_WORD = {0: "forbidden", 1: "limited", 2: "semi-limited"}


def is_extra(frame):
    return (frame or "") in EXTRA_FRAMES


def play_index(conn, archetype=None, fmt=None):
    """card_id -> how much the current meta plays it.

    When `archetype` is given, that archetype's own inclusion wins for any
    card it plays, because "87% of Branded lists" is the number a Branded
    builder needs -- not whichever archetype happens to play the card most.
    Everything else falls back to the best-supported archetype.
    """
    q = "SELECT archetype, card_id, section, pct_lists, n_lists FROM inclusion"
    args = []
    if fmt:
        q += " WHERE format = ?"
        args.append(fmt)

    want = _slug(archetype) if archetype else None
    out = {}
    for r in conn.execute(q, args):
        n = r["n_lists"] or 0
        pct = r["pct_lists"] or 0.0
        # Wilson lower bound on the inclusion rate: a thin sample is
        # ranked by what it actually supports, not by its point estimate.
        lo = stats.wilson(round(pct * n), n)[1]
        own = want is not None and _slug(r["archetype"]) == want
        rec = {"pct": pct, "n": n, "archetype": r["archetype"],
               "section": r["section"], "own": own, "rank": lo}
        prev = out.get(r["card_id"])
        if prev is None:
            out[r["card_id"]] = rec
        elif (own, lo) > (prev["own"], prev["rank"]):
            out[r["card_id"]] = rec
    return out


def _slug(name):
    return "-".join(str(name).lower().replace("-", " ").split())


def _tier(name_low, q):
    """0 exact name, 1 anywhere in the name, 2 card text only.

    Prefix matches are deliberately NOT their own tier. Splitting them out
    sorts "Dragon's Mind" (33% of three lists) above every dragon the meta
    actually plays, purely because it starts with the word -- which defeats
    the point of ranking by meta presence at all.
    """
    if not q:
        return 1
    if name_low == q:
        return 0
    return 1 if q in name_low else 2


def search(conn, q="", archetype=None, section=None, limit=60, include_text=True):
    """Rank the whole pool against a query.

    An empty query is not an empty answer: it returns what the meta plays,
    which is the useful default for a blank search box.
    """
    q = (q or "").strip().lower()
    play = play_index(conn, archetype)

    where, args = [], []
    if q:
        like = f"%{q}%"
        if include_text:
            where.append("(LOWER(name) LIKE ? OR LOWER(text) LIKE ?)")
            args += [like, like]
        else:
            where.append("LOWER(name) LIKE ?")
            args.append(like)
    sql = "SELECT id,name,type,frame,race,attribute,level,atk,def,linkval," \
          "banlist_tcg FROM cards"
    if where:
        sql += " WHERE " + " AND ".join(where)

    rows = []
    for r in conn.execute(sql, args):
        frame = r["frame"] or ""
        if frame in TOKEN_FRAMES:
            continue  # not deckable
        ex = is_extra(frame)
        if section == "extra" and not ex:
            continue
        if section == "main" and ex:
            continue
        p = play.get(r["id"])
        name_low = (r["name"] or "").lower()
        rows.append((
            _tier(name_low, q),
            0 if (p and p["own"]) else 1,   # the deck's own archetype first
            -(p["rank"] if p else 0.0),
            name_low,
            _card(r, p),
        ))
    rows.sort(key=lambda t: t[:4])
    return [t[4] for t in rows[:limit]], len(rows)


def _card(r, p):
    status = r["banlist_tcg"]
    return {
        "id": r["id"],
        "name": r["name"],
        "type": r["type"],
        "frame": r["frame"],
        "race": r["race"],
        "attribute": r["attribute"],
        "level": r["level"],
        "atk": r["atk"],
        "def": r["def"],
        "link": r["linkval"],
        "extra": is_extra(r["frame"]),
        "limit": {"Banned": 0, "Limited": 1, "Semi-Limited": 2}.get(status, 3),
        "status": status,
        "play": p,
    }


def decorate(conn, ids, archetype=None):
    """[(card record)] for a concrete list, with meta presence attached."""
    play = play_index(conn, archetype)
    out = []
    for cid in ids:
        row = card_db.resolve(conn, cid)
        if row is None:
            out.append({"id": cid, "name": f"<unknown {cid}>", "unknown": True,
                        "extra": False, "limit": 3, "play": None})
            continue
        rec = _card(row, play.get(row["id"]))
        rec["canonical"] = row["id"]
        out.append(rec)
    return out


def build_deck(name, sections):
    """A Deck from {"main":[ids], ...}, cards routed by frame if misplaced."""
    return Deck(name=name,
                main=list(sections.get("main") or []),
                extra=list(sections.get("extra") or []),
                side=list(sections.get("side") or []))


def misrouted(conn, sections):
    """Cards sitting in a section their frame forbids.

    The browser routes on the frame it was handed, so this should never
    fire -- which is exactly why it is checked rather than assumed.
    """
    bad = []
    for sec in SECTIONS:
        for cid in sections.get(sec) or []:
            row = card_db.resolve(conn, cid)
            if row is None:
                continue
            ex = is_extra(row["frame"])
            if sec == "main" and ex:
                bad.append((row["name"], "extra-deck card in the main deck"))
            elif sec == "extra" and not ex:
                bad.append((row["name"], "main-deck card in the extra deck"))
    return bad


def safe_name(name, fallback="untitled"):
    """Deck names become filenames. Nothing else may."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip("-. ")
    return safe or fallback
