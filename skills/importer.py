"""Get a deck into duelroom from wherever the user built it.

Three sources, because people build decks in different places:

* a local ``.ydk`` file (EDOPro, YGOPRO, anything that exports one)
* a ``ydke://`` URI -- the universal clipboard format, understood by
  EDOPro, Master Duel importers and most online builders
* a YGOPRODeck deck URL

YDKE is the one that matters most: it is what "copy deck" produces almost
everywhere, so it is the shortest path from "I built a list" to "the tool
is measuring it".
"""

import base64
import re
import struct
from pathlib import Path

import httpx

from .deck import Deck

DECKS = Path(__file__).resolve().parent.parent / "assets" / "decks"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    )
}


class ImportError_(ValueError):
    pass


# --- ydke:// --------------------------------------------------------------

def _b64_to_codes(chunk):
    """base64 -> list of little-endian uint32 passcodes."""
    if not chunk:
        return []
    pad = "=" * (-len(chunk) % 4)
    raw = base64.b64decode(chunk + pad)
    if len(raw) % 4:
        raise ImportError_("ydke section is not a whole number of 4-byte codes")
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


def _codes_to_b64(codes):
    if not codes:
        return ""
    return base64.b64encode(struct.pack(f"<{len(codes)}I", *codes)).decode()


def from_ydke(uri, name="imported"):
    """Parse ``ydke://<main>!<extra>!<side>!``."""
    body = uri.strip()
    if body.startswith("ydke://"):
        body = body[len("ydke://"):]
    parts = body.split("!")
    if len(parts) < 3:
        raise ImportError_(
            "ydke URI needs three '!'-separated sections (main, extra, side)"
        )
    main, extra, side = (_b64_to_codes(p) for p in parts[:3])
    return Deck(name=name, main=main, extra=extra, side=side)


def to_ydke(deck):
    """Render a deck back out as a ydke:// URI, for pasting elsewhere."""
    return "ydke://" + "!".join(
        _codes_to_b64(list(section))
        for section in (deck.main, deck.extra, deck.side)
    ) + "!"


# --- ygoprodeck deck page -------------------------------------------------

_SECTION = re.compile(
    r'id="(main_deck|extra_deck|side_deck)"(.*?)(?=id="(?:main_deck|extra_deck|side_deck)"|$)',
    re.S,
)
_CARD = re.compile(r"/card/\?search=(\d+)")
_TITLE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]+)"', re.I)
_H1 = re.compile(r"<title>\s*(.*?)\s*</title>", re.I | re.S)


def from_ygoprodeck(url, name=None):
    """Scrape one deck page.

    The page lists every copy as its own ``/card/?search=<passcode>``
    link inside a main/extra/side container, so order and duplicates
    survive.
    """
    r = httpx.get(url, headers=HEADERS, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    body = r.text

    sections = {"main": [], "extra": [], "side": []}
    for m in _SECTION.finditer(body):
        key = m.group(1).replace("_deck", "")
        sections[key] = [int(c) for c in _CARD.findall(m.group(2))]

    if not sections["main"]:
        raise ImportError_(
            f"no cards found at {url} — is it a deck page? "
            "(expected https://ygoprodeck.com/deck/<slug>)"
        )
    if name is None:
        # From the PAGE, not the URL slug. YGOPRODeck resolves a deck by
        # the numeric id at the end and ignores the slug, so
        # /deck/kewl-tune-734601 happily serves a Blitzclique deck. Naming
        # from the slug therefore mislabels any URL whose slug is stale or
        # mistyped -- which is exactly what a hand-edited link is.
        m = _TITLE.search(body) or _H1.search(body)
        title = m.group(1).strip() if m else ""
        title = re.sub(r"\s*[-|]\s*YGOPRODECK.*$", "", title, flags=re.I).strip()
        if not title:
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            title = re.sub(r"-\d+$", "", slug) or "imported"
        name = title
    return Deck(name=name, **sections)


# --- front door -----------------------------------------------------------

def load(source, name=None):
    """Import from a .ydk path, a ydke:// URI, or a YGOPRODeck URL."""
    text = str(source).strip()
    if text.startswith("ydke://"):
        return from_ydke(text, name or "imported")
    if text.startswith(("http://", "https://")):
        return from_ygoprodeck(text, name)
    path = Path(text)
    if path.exists():
        d = Deck.from_ydk(path)
        if name:
            d.name = name
        return d
    raise ImportError_(
        f"don't know how to import {text!r} — expected a .ydk path, a "
        "ydke:// URI, or a https://ygoprodeck.com/deck/... URL"
    )


def save(deck, name=None, decks_dir=None):
    """Write into assets/decks so every other command can find it."""
    decks_dir = Path(decks_dir or DECKS)
    decks_dir.mkdir(parents=True, exist_ok=True)
    if name:
        deck.name = name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", deck.name).strip("-") or "imported"
    deck.name = safe
    path = decks_dir / f"{safe}.ydk"
    deck.write_ydk(path)
    return path
