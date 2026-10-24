"""Decklist ingestion from YGOPRODeck.

The official v7 API is card data only -- there is no decklist endpoint. The
deck-search page drives an internal JSON endpoint, `/api/decks/getDecks.php`,
which is what this module uses. Discovered facets:

    _sft_category   format bucket, e.g. "Tournament Meta Decks"
    _sft_post_tag   archetype tag (vocabulary from autocompleteDeckArchetype)
    sort            Comments | Date | Deck Views | Updated
    from, to        YYYY-MM-DD
    offset          paginates in steps of 20 (`limit` is ignored)
    tournament, decksize, author, cardcode, notcardcode, range

Records carry full main/extra/side passcode arrays plus tournament name,
placement and player count. There is no absolute date field -- only relative
strings like "4 weeks ago", which `parse_relative_date` approximates.

Be polite: this is an internal endpoint, not a documented public API.
"""

import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from . import card_db, db
from .deck import Deck

BASE = "https://ygoprodeck.com"
GET_DECKS = f"{BASE}/api/decks/getDecks.php"
ARCHETYPE_VOCAB = f"{BASE}/api/autocompleteDeckArchetype.php"

PAGE_SIZE = 20          # the endpoint's fixed page size
MIN_INTERVAL = 0.4      # seconds between requests; stay well under any limit
LISTS_PER_ARCHETYPE = 50

DEFAULT_FORMAT = "Tournament Meta Decks"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Referer": f"{BASE}/deck-search/",
    "X-Requested-With": "XMLHttpRequest",
}

_last_request = 0.0


def _throttle():
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _get(url, params=None):
    _throttle()
    r = httpx.get(url, params=params, headers=HEADERS, timeout=60.0,
                  follow_redirects=True)
    if r.status_code == 400:
        return []  # the endpoint's "no matches" response
    r.raise_for_status()
    return r.json()


# --- dates ----------------------------------------------------------------

_REL = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago")
_UNIT_DAYS = {"second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24,
              "day": 1, "week": 7, "month": 30.44, "year": 365.25}


def parse_relative_date(text, now=None):
    """'4 weeks ago' -> approximate ISO date. None if unparseable.

    Approximate by construction -- the endpoint exposes nothing better. Good
    enough for format bucketing, not for ordering decks within a week.
    """
    if not text or text == "None":
        return None
    now = now or datetime.now(timezone.utc)
    m = _REL.search(text.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return (now - timedelta(days=n * _UNIT_DAYS[unit])).date().isoformat()


# --- archetype vocabulary -------------------------------------------------

def fetch_archetype_vocab():
    """All 600-odd archetype tags the deck search recognises."""
    data = _get(ARCHETYPE_VOCAB)
    return [s["value"] for s in data.get("suggestions", [])]


_ALNUM = re.compile(r"[A-Za-z0-9]")


def _tag_pattern(tag):
    """Word-boundary matcher for one archetype tag.

    Plain substring matching is badly wrong here: "Ra" hits inside
    "D(ra)gon" and "P" inside "Ten(p)ai", which buried the real archetypes
    under junk. Standard \\b does not work either, because tags like
    "@Ignister", "-Eyes Dragon" and '"C"' begin or end with punctuation --
    so the boundary is only asserted on the sides that are alphanumeric.
    """
    body = re.escape(tag)
    left = r"(?<![A-Za-z0-9])" if _ALNUM.match(tag[0]) else ""
    right = r"(?![A-Za-z0-9])" if _ALNUM.match(tag[-1]) else ""
    return re.compile(left + body + right, re.IGNORECASE)


_pattern_cache = {}


def compile_vocab(vocab):
    key = id(vocab)
    if key not in _pattern_cache:
        _pattern_cache[key] = [(t, _tag_pattern(t)) for t in vocab if t]
    return _pattern_cache[key]


def match_archetypes(deck_name, vocab):
    """Tags appearing in a deck name, longest first.

    Deck names are composites ("Azamina Mitsurugi Dark Magician"), so this
    returns every hit. The longest match is a hint at the engine, not an
    answer -- see discover_meta.
    """
    name = deck_name or ""
    hits = [t for t, pat in compile_vocab(vocab) if pat.search(name)]
    return sorted(hits, key=len, reverse=True)


# --- fetching -------------------------------------------------------------

def fetch_page(fmt=DEFAULT_FORMAT, archetype=None, since=None, sort="Updated",
               offset=0):
    params = {"_sft_category": fmt, "sort": sort, "offset": offset}
    if archetype:
        params["_sft_post_tag"] = archetype
    if since:
        params["from"] = since
    data = _get(GET_DECKS, params)
    return data if isinstance(data, list) else []


def iter_decks(fmt=DEFAULT_FORMAT, archetype=None, since=None, sort="Updated",
               max_decks=200):
    """Paginate until exhausted or max_decks reached."""
    seen, offset = 0, 0
    while seen < max_decks:
        page = fetch_page(fmt, archetype, since, sort, offset)
        if not page:
            return
        for rec in page:
            yield rec
            seen += 1
            if seen >= max_decks:
                return
        if len(page) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


# --- storage --------------------------------------------------------------

def _ids(raw):
    """main_deck/extra_deck/side_deck arrive as a JSON array of strings."""
    if not raw or raw == "None":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def record_to_deck(rec):
    return Deck(
        name=rec.get("deck_name") or "untitled",
        main=_ids(rec.get("main_deck")),
        extra=_ids(rec.get("extra_deck")),
        side=_ids(rec.get("side_deck")),
    )


def store_deck(conn, rec, archetype, fmt, tags=()):
    """Insert one scraped list. Returns deck id, or None if already present."""
    d = record_to_deck(rec)
    d.canonicalise(conn)
    if not d.main:
        return None

    scraped = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO decks
             (archetype, format, source_url, title, placement, deck_date,
              list_hash, scraped_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(list_hash, format) DO NOTHING""",
        (
            archetype,
            fmt,
            f"{BASE}/deck/{rec.get('pretty_url')}" if rec.get("pretty_url") else None,
            rec.get("deck_name"),
            rec.get("tournamentPlacement"),
            parse_relative_date(rec.get("submit_date")),
            d.list_hash,
            scraped,
        ),
    )
    if cur.rowcount == 0:
        return None
    deck_id = cur.lastrowid

    rows = []
    for section in ("main", "extra", "side"):
        for cid, n in Counter(getattr(d, section)).items():
            rows.append((deck_id, cid, section, n))
    conn.executemany(
        "INSERT OR REPLACE INTO deck_cards (deck_id, card_id, section, copies) "
        "VALUES (?,?,?,?)",
        rows,
    )
    if tags:
        conn.executemany(
            "INSERT OR IGNORE INTO deck_tags (deck_id, tag) VALUES (?,?)",
            [(deck_id, t) for t in tags],
        )
    return deck_id


# --- the two entry points -------------------------------------------------

def discover_meta(conn, fmt=DEFAULT_FORMAT, since=None, sample=200):
    """What is actually being played right now.

    Scrapes a broad sample of recent tournament lists and ranks archetype
    tags by appearances. This is the live meta measured from results, rather
    than a list you maintained by hand and forgot to update.

    Counts *every* tag in a deck name, not just one. Modern lists are
    hybrids -- "Azamina Mitsurugi Dark Magician Light and Darkness Ritual"
    is four tags and no single one is the right label. Picking a primary by
    longest match systematically favours wordy tags ("Light and Darkness
    Ritual") over the actual engine, so the primary is reported separately
    and treated as a hint, not an answer.

    Returns (tag_counts, primary_counts, pairs, examples).
    """
    vocab = fetch_archetype_vocab()
    tag_counts, primary_counts, pairs = Counter(), Counter(), Counter()
    examples, n = {}, 0

    for rec in iter_decks(fmt=fmt, since=since, sort="Updated", max_decks=sample):
        hits = match_archetypes(rec.get("deck_name"), vocab)
        if not hits:
            continue
        n += 1
        for tag in hits:
            tag_counts[tag] += 1
            examples.setdefault(tag, rec.get("deck_name"))
        primary_counts[hits[0]] += 1
        for i, a in enumerate(sorted(hits)):
            for b in sorted(hits)[i + 1:]:
                pairs[(a, b)] += 1

    return tag_counts, primary_counts, pairs, examples


def scrape_recent(conn, fmt=DEFAULT_FORMAT, since=None, max_decks=500):
    """Pull every recent tournament list in one sweep and tag it locally.

    Deliberately not one query per archetype. The site's own `_sft_post_tag`
    facet is sparse and stale -- filtering on tag "Azamina" with a 120-day
    window returns a single list, while 33 recent lists have Azamina in the
    name. Scraping broadly by date and matching tags here is both more
    current and far cheaper: ~25 requests covers every archetype at once
    instead of 662 near-empty ones.

    Returns (stored, skipped, tag_counts).
    """
    vocab = fetch_archetype_vocab()
    stored = skipped = 0
    tag_counts = Counter()
    with conn:
        for rec in iter_decks(fmt=fmt, since=since, sort="Updated",
                              max_decks=max_decks):
            tags = match_archetypes(rec.get("deck_name"), vocab)
            primary = tags[0] if tags else None
            if store_deck(conn, rec, primary, fmt, tags):
                stored += 1
                for t in tags:
                    tag_counts[t] += 1
            else:
                skipped += 1
    return stored, skipped, tag_counts


def scrape_archetype(conn, archetype, fmt=DEFAULT_FORMAT, since=None,
                     target=LISTS_PER_ARCHETYPE):
    """Pull lists via the site's own archetype facet.

    Kept for the cases where the curated tag is what you actually want, but
    `scrape_recent` is the normal path -- see the note there about the facet
    being sparse.
    """
    stored = skipped = 0
    with conn:
        for rec in iter_decks(fmt=fmt, archetype=archetype, since=since,
                              sort="Updated", max_decks=target):
            if store_deck(conn, rec, archetype, fmt, (archetype,)):
                stored += 1
            else:
                skipped += 1
    return stored, skipped


# --- inclusion analysis ---------------------------------------------------

# Four buckets, not three. An earlier three-bucket split (core >=90, flex
# 20-60, else fringe) left 60-90% uncovered, so near-universal cards were
# being labelled "fringe" -- side decks showed 87% staples in the fringe
# bucket. Anything in STAPLE is standard but not automatic: worth knowing,
# not worth searching.
CORE_THRESHOLD = 0.90
STAPLE_LOW = 0.60
FLEX_LOW, FLEX_HIGH = 0.20, 0.60


def compute_inclusion(conn, archetype, fmt=DEFAULT_FORMAT):
    """Per-card inclusion rate across the stored lists for an archetype.

    This is what shrinks the optimizer's search space: cards at 90%+ are the
    untouchable core, and the 20-60% band is the only part worth searching.
    """
    n_lists = conn.execute(
        """SELECT COUNT(*) AS n FROM decks d
             JOIN deck_tags t ON t.deck_id = d.id
            WHERE t.tag = ? AND d.format = ?""",
        (archetype, fmt),
    ).fetchone()["n"]
    if n_lists == 0:
        return 0

    computed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT card_id, section,
                  COUNT(DISTINCT deck_id) AS lists,
                  SUM(copies) * 1.0 / COUNT(DISTINCT deck_id) AS avg_copies
             FROM deck_cards
            WHERE deck_id IN (SELECT d.id FROM decks d
                                JOIN deck_tags t ON t.deck_id = d.id
                               WHERE t.tag = ? AND d.format = ?)
         GROUP BY card_id, section""",
        (archetype, fmt),
    ).fetchall()

    with conn:
        conn.execute(
            "DELETE FROM inclusion WHERE archetype = ? AND format = ?",
            (archetype, fmt),
        )
        conn.executemany(
            """INSERT INTO inclusion
                 (archetype, format, card_id, section, pct_lists,
                  avg_copies, n_lists, computed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (archetype, fmt, r["card_id"], r["section"],
                 r["lists"] / n_lists, r["avg_copies"], n_lists, computed)
                for r in rows
            ],
        )
    return n_lists


def classify(conn, archetype, fmt=DEFAULT_FORMAT, section="main"):
    """Split an archetype's cards into core / staple / flex / fringe."""
    rows = conn.execute(
        """SELECT i.*, c.name FROM inclusion i
             LEFT JOIN cards c ON c.id = i.card_id
            WHERE i.archetype = ? AND i.format = ? AND i.section = ?
         ORDER BY i.pct_lists DESC, c.name""",
        (archetype, fmt, section),
    ).fetchall()

    buckets = {"core": [], "staple": [], "flex": [], "fringe": []}
    for r in rows:
        pct = r["pct_lists"]
        if pct >= CORE_THRESHOLD:
            key = "core"
        elif pct > STAPLE_LOW:
            key = "staple"
        elif pct >= FLEX_LOW:
            key = "flex"
        else:
            key = "fringe"
        buckets[key].append(
            {
                "card_id": r["card_id"],
                "name": r["name"] or f"<{r['card_id']}>",
                "pct": pct,
                "avg_copies": r["avg_copies"],
                "n_lists": r["n_lists"],
            }
        )
    return buckets


# --- drift over time ------------------------------------------------------

# Below this many lists an archetype's inclusion rates move by more than
# a tech card ever would, purely from which lists happened to be scraped.
MIN_LISTS_FOR_DRIFT = 15



def scrape_fingerprint(conn):
    """What scrape the current `inclusion` table was computed from.

    A snapshot records inclusion rates, and `inclusion.computed_at` moves
    every time anything recomputes them -- which makes six-day-old data
    look fresh. Two snapshots of the SAME scrape then differ only by when
    they were taken, and `drift` between them reports about zero
    movement, which reads as "the meta is stable" when it means "these
    are the same data".

    That is not hypothetical: `baseline-2026-10-18` and `2026-10-18` are
    both 5599 rows from the same scrape, 3.5 hours apart.

    The fingerprint is the newest scraped list plus the list count, which
    is what actually has to change for drift to mean anything.
    """
    r = conn.execute(
        "SELECT MAX(scraped_at) AS newest, COUNT(*) AS n FROM decks"
    ).fetchone()
    return f"{(r['newest'] or 'none')}|{r['n'] or 0}"

def snapshot_inclusion(conn, label=None):
    """Freeze the current `inclusion` table under a label.

    Drift is a question about time, so it needs more than one scrape.
    Slicing a single scrape by date does not substitute: the stored lists
    cluster around when they were last updated, leaving a handful in any
    recent window.
    """
    label = label or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(inclusion_history)")}
        if "scrape" not in cols:
            conn.execute("ALTER TABLE inclusion_history ADD COLUMN scrape TEXT")
        conn.execute("DELETE FROM inclusion_history WHERE label = ?", (label,))
        conn.execute(
            """INSERT INTO inclusion_history
                 (label, archetype, format, card_id, section, pct_lists,
                  avg_copies, n_lists, captured_at, scrape)
               SELECT ?, archetype, format, card_id, section, pct_lists,
                      avg_copies, n_lists, ?, ?
                 FROM inclusion""",
            (label, now, scrape_fingerprint(conn)),
        )
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM inclusion_history WHERE label = ?",
            (label,),
        ).fetchone()["n"]
    return label, n


def snapshots(conn):
    return [
        (r["label"], r["n"], r["captured_at"])
        for r in conn.execute(
            """SELECT label, COUNT(*) AS n, MAX(captured_at) AS captured_at
                 FROM inclusion_history GROUP BY label ORDER BY captured_at""")
    ]


def same_scrape(conn, old_label, new_label):
    """True when both snapshots were taken from the SAME scrape.

    Drift across one scrape is zero by construction. Reporting it without
    saying so reads as "the meta did not move"; the truth is that no new
    lists were gathered. Returns None when either snapshot predates the
    `scrape` column and cannot be checked -- which must not be mistaken
    for "different".
    """
    got = {}
    for label in (old_label, new_label):
        try:
            r = conn.execute(
                "SELECT scrape FROM inclusion_history WHERE label = ? LIMIT 1",
                (label,)).fetchone()
        except Exception:
            return None
        if r is None:
            return None
        got[label] = r["scrape"]
    if any(v is None for v in got.values()):
        return None
    return got[old_label] == got[new_label]


def drift(conn, archetype, old_label, new_label, fmt=DEFAULT_FORMAT,
          section="main"):
    """Inclusion-rate change between two snapshots.

    Returns (rows, note). `note` is non-empty when the comparison is not
    worth reading -- too few lists on either side -- rather than letting
    a confident-looking percentage stand on five decks.
    """
    def side(label):
        return {
            r["card_id"]: r
            for r in conn.execute(
                """SELECT * FROM inclusion_history
                    WHERE label = ? AND archetype = ? AND format = ?
                      AND section = ?""",
                (label, archetype, fmt, section))
        }

    old, new = side(old_label), side(new_label)
    if not old or not new:
        return [], f"no snapshot for {old_label if not old else new_label}"

    n_old = next(iter(old.values()))["n_lists"]
    n_new = next(iter(new.values()))["n_lists"]
    note = ""
    if min(n_old, n_new) < MIN_LISTS_FOR_DRIFT:
        note = (f"only {min(n_old, n_new)} lists on one side "
                f"(need {MIN_LISTS_FOR_DRIFT}); treat this as noise")

    rows = []
    for cid in set(old) | set(new):
        a = old.get(cid, {"pct_lists": 0.0})["pct_lists"]
        b = new.get(cid, {"pct_lists": 0.0})["pct_lists"]
        if a == b:
            continue
        src = new.get(cid) or old.get(cid)
        rows.append({
            "card_id": cid,
            "was": a,
            "now": b,
            "delta": b - a,
            "n_old": n_old,
            "n_new": n_new,
        })
    rows.sort(key=lambda r: -abs(r["delta"]))
    return rows, note


def identify_archetype(conn, deck, fmt=DEFAULT_FORMAT, min_overlap=0.5):
    """Guess which scraped archetype a deck belongs to, from its cards.

    Matches against each archetype's CORE cards (>=90% inclusion) rather
    than its whole pool: the core is what makes a deck that deck, while
    the flex slots are mostly shared staples that would match everything.

    Returns [(archetype, overlap, n_core)] best first. The caller decides
    whether the top score is good enough -- a 50% overlap on a 4-card
    core is two staples and means nothing.
    """
    have = set(deck.main) | set(deck.extra)
    rows = conn.execute(
        """SELECT archetype, card_id, pct_lists FROM inclusion
            WHERE format = ? AND section = 'main' AND pct_lists >= ?""",
        (fmt, CORE_THRESHOLD),
    ).fetchall()
    core = {}
    for r in rows:
        core.setdefault(r["archetype"], set()).add(r["card_id"])

    scored = []
    for arch, cards in core.items():
        if len(cards) < 3:          # too thin a core to identify anything
            continue
        hit = len(cards & have)
        scored.append((arch, hit / len(cards), len(cards)))
    scored.sort(key=lambda t: (-t[1], -t[2]))
    return [s for s in scored if s[1] >= min_overlap] or scored[:1]

# Tournament placement, best first. `export` used to take whichever list
# was stored FIRST (`ORDER BY d.id LIMIT 1`), which is an artefact of
# scrape order and nothing else -- that is how `branded.ydk` became a
# Branded/Blitzclique hybrid with one Fallen of Albaz and no Branded
# Fusion, averaging 1.0 interruptions over 40 hands while sitting in the
# board library as a meta deck.
PLACEMENT_RANK = {
    "winner": 0, "1st place": 0, "champion": 0,
    "runner-up": 1, "2nd place": 1, "finalist": 1,
    "3rd place": 2, "4th place": 3,
    "top 4": 4, "top 8": 5, "top 16": 6, "top 32": 7, "top 64": 8,
}


def placement_rank(placement):
    """Sort key for a placement string; unknown or missing sorts last."""
    if not placement:
        return 99
    key = str(placement).strip().lower()
    if key in PLACEMENT_RANK:
        return PLACEMENT_RANK[key]
    # "Top N" for an N nobody listed yet.
    if key.startswith("top "):
        tail = key[4:].strip()
        if tail.isdigit():
            return 4 + max(0, min(20, int(tail).bit_length() - 2))
    return 98


def best_list(conn, tag):
    """The best-placing stored list for `tag`, most recent as the tiebreak.

    Returns a row with id/title/placement, or None. Replaces
    `ORDER BY d.id LIMIT 1`, which ranked by scrape order.
    """
    rows = conn.execute(
        """SELECT d.id, d.title, d.placement, d.deck_date FROM decks d
             JOIN deck_tags t ON t.deck_id = d.id
            WHERE t.tag = ?""",
        (tag,),
    ).fetchall()
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda r: (placement_rank(r["placement"]),
                       -(_date_key(r["deck_date"])), r["id"]),
    )[0]


def _date_key(value):
    """A sortable integer from a date string; 0 when absent or unparsable."""
    if not value:
        return 0
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits[:8]) if len(digits) >= 8 else 0
