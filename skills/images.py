"""Local card-image cache.

YGOPRODeck's terms forbid hotlinking their images: you download once and
serve your own copy. That is also the only way a deckbuilder stays usable,
since a list view would otherwise fire a hundred cross-origin requests at
someone else's CDN every time it rendered.

Three things here are deliberate.

**Downloads land atomically.** Every file is written to ``<id>.jpg.part``
and renamed only after the whole body arrives. A half-written JPEG that
keeps its final name is indistinguishable from a cached one, so the next
run skips it and the card is permanently broken -- the cache equivalent of
a confident wrong number.

**A 404 and a timeout are recorded differently.** Some passcodes have no
artwork at all (tokens, unreleased ids), and retrying those forever is
waste; a network failure, by contrast, must be retried. Only the first is
written to the ``missing`` ledger.

**Meta-played cards are fetched first.** The full pool at 10 requests a
second is about twenty-five minutes, but the ~1,100 cards the scrape has
actually seen in a tournament list are done in under two, which is when
the builder becomes useful.
"""

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cardimg"

BASE = "https://images.ygoprodeck.com/images"
SIZES = {
    "small": (f"{BASE}/cards_small", "list rows and search results"),
    "full": (f"{BASE}/cards", "the full card, for reading the text"),
}

# Their documented API limit is 20 req/s and exceeding it blocks the
# address for an hour. Images are a different host, but there is no
# reason to find out the hard way for a job that is already fast enough.
RPS = 10.0
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
USER_AGENT = "duelroom/0.1 (personal deck analysis; local image cache)"


def dir_for(size):
    if size not in SIZES:
        raise ValueError(f"unknown image size {size!r}")
    return CACHE / size


def path_for(size, card_id):
    return dir_for(size) / f"{int(card_id)}.jpg"


def have(size, card_id):
    p = path_for(size, card_id)
    return p.exists() and p.stat().st_size > 0


def _missing_path(size):
    return dir_for(size) / "missing.json"


def load_missing(size):
    """Passcodes upstream has no artwork for. Not retried."""
    p = _missing_path(size)
    if not p.exists():
        return set()
    try:
        return {int(x) for x in json.loads(p.read_text())}
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()


def save_missing(size, ids):
    dir_for(size).mkdir(parents=True, exist_ok=True)
    _missing_path(size).write_text(json.dumps(sorted(int(i) for i in ids)))


def wanted(conn, scope="small-all"):
    """(size, [card ids]) for a scope, meta-played cards first."""
    try:
        size, which = scope.split("-", 1)
    except ValueError:
        raise ValueError(f"scope must be <size>-<all|meta>, got {scope!r}")
    if size not in SIZES or which not in ("all", "meta"):
        raise ValueError(f"unknown scope {scope!r}")

    meta = [r["card_id"] for r in conn.execute(
        "SELECT card_id, MAX(pct_lists) AS p FROM inclusion "
        "GROUP BY card_id ORDER BY p DESC")]
    if which == "meta":
        return size, meta
    seen = set(meta)
    rest = [r["id"] for r in conn.execute("SELECT id FROM cards ORDER BY id")
            if r["id"] not in seen]
    return size, meta + rest


def status(conn):
    """What is cached right now, per size. Counts files, not intentions."""
    out = {}
    total = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
    meta = conn.execute(
        "SELECT COUNT(DISTINCT card_id) AS n FROM inclusion").fetchone()["n"]
    for size in SIZES:
        d = dir_for(size)
        files = list(d.glob("*.jpg")) if d.exists() else []
        out[size] = {
            "cached": len(files),
            "bytes": sum(f.stat().st_size for f in files),
            "missing_upstream": len(load_missing(size)),
            "pool": total,
            "meta": meta,
        }
    return out


def fetch(conn, scope="small-all", limit=None, log=print):
    """Download what the scope asks for and is not already on disk.

    Returns a summary dict. Safe to interrupt and re-run: everything
    already present is skipped, and nothing half-written keeps its name.
    """
    size, ids = wanted(conn, scope)
    base, _ = SIZES[size]
    out = dir_for(size)
    out.mkdir(parents=True, exist_ok=True)

    gone = load_missing(size)
    todo = [i for i in ids if not have(size, i) and i not in gone]
    skipped = len(ids) - len(todo)      # counted BEFORE the limit truncates
    if limit:
        todo = todo[:limit]

    log(f"{scope}: {len(ids)} in scope, {skipped} already cached or "
        f"known-absent, {len(todo)} to fetch at {RPS:.0f}/s "
        f"(~{len(todo) / RPS / 60:.0f} min)")

    done = failed = absent = 0
    interval = 1.0 / RPS
    next_at = time.monotonic()
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
                      follow_redirects=True) as client:
        for n, cid in enumerate(todo, 1):
            now = time.monotonic()
            if now < next_at:
                time.sleep(next_at - now)
            next_at = time.monotonic() + interval

            dest = path_for(size, cid)
            part = dest.with_suffix(".jpg.part")
            try:
                r = client.get(f"{base}/{cid}.jpg")
            except httpx.HTTPError as e:
                failed += 1
                log(f"  {cid}: network error ({type(e).__name__}) — will retry")
                continue
            if r.status_code == 404:
                absent += 1
                gone.add(cid)
                continue
            if r.status_code != 200 or not r.content:
                failed += 1
                log(f"  {cid}: HTTP {r.status_code} — will retry")
                continue
            # Atomic: a truncated body never keeps the final name.
            part.write_bytes(r.content)
            part.rename(dest)
            done += 1

            if n % 100 == 0 or n == len(todo):
                log(f"progress {n}/{len(todo)} — {done} fetched, "
                    f"{absent} absent upstream, {failed} to retry")
                save_missing(size, gone)

    save_missing(size, gone)
    return {"scope": scope, "size": size, "attempted": len(todo),
            "fetched": done, "absent": absent, "failed": failed}


def main(argv):
    from . import db
    scope = argv[0] if argv else "small-all"
    limit = int(argv[1]) if len(argv) > 1 else None
    conn = db.connect()
    r = fetch(conn, scope, limit, log=lambda m: print(m, flush=True))
    print(json.dumps(r))
    # A run that failed on every card must not read as success.
    return 1 if r["failed"] and r["fetched"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
