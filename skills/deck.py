"""Deck model, .ydk parsing/writing, and legality checks."""

import hashlib
from collections import Counter
from pathlib import Path

from . import card_db

SECTIONS = ("main", "extra", "side")

MAIN_MIN, MAIN_MAX = 40, 60
EXTRA_MAX = 15
SIDE_MAX = 15

# Extra-deck frame types, used to route cards to the right section when a
# .ydk is malformed and when validating an assembled list.
# Extra-deck frames. The four pendulum hybrids belong here too: a Fusion
# Pendulum monster such as Starving Venom Wing Dragon is an extra-deck
# card. Leaving them out made `validate` report "main-deck card in the
# extra deck" on real tournament lists -- the scraped decks hold 3
# fusion_pendulum and 2 xyz_pendulum cards in their extra sections -- and
# miss the same card sitting illegally in a main deck.
# ritual_pendulum stays out: Ritual monsters are main-deck cards.
EXTRA_FRAMES = {
    "fusion", "synchro", "xyz", "link",
    "fusion_pendulum", "synchro_pendulum", "xyz_pendulum",
}


class Deck:
    def __init__(self, name="untitled", main=None, extra=None, side=None):
        self.name = name
        self.main = list(main or [])
        self.extra = list(extra or [])
        self.side = list(side or [])

    # --- construction -----------------------------------------------------

    @classmethod
    def from_ydk(cls, path):
        path = Path(path)
        sections = {s: [] for s in SECTIONS}
        current = "main"
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("!side"):
                current = "side"
                continue
            if low.startswith("#extra"):
                current = "extra"
                continue
            if low.startswith("#main"):
                current = "main"
                continue
            if line.startswith("#"):
                continue  # "#created by ..." and friends
            try:
                sections[current].append(int(line))
            except ValueError:
                continue  # tolerate junk lines rather than refusing the file
        return cls(name=path.stem, **sections)

    def to_ydk(self):
        out = ["#created by duelroom", "#main"]
        out += [str(c) for c in self.main]
        out.append("#extra")
        out += [str(c) for c in self.extra]
        out.append("!side")
        out += [str(c) for c in self.side]
        return "\n".join(out) + "\n"

    def write_ydk(self, path):
        Path(path).write_text(self.to_ydk(), encoding="utf-8")

    # --- identity ---------------------------------------------------------

    def counts(self, section):
        return Counter(getattr(self, section))

    @property
    def list_hash(self):
        """Stable hash of the card multiset. Used to dedup scraped lists."""
        parts = []
        for s in SECTIONS:
            items = sorted(Counter(getattr(self, s)).items())
            parts.append(f"{s}:" + ",".join(f"{c}x{n}" for c, n in items))
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def __len__(self):
        return len(self.main)

    def __repr__(self):
        return (
            f"<Deck {self.name!r} main={len(self.main)} "
            f"extra={len(self.extra)} side={len(self.side)}>"
        )

    # --- normalisation ----------------------------------------------------

    def canonicalise(self, conn):
        """Resolve alternate-art passcodes to canonical ids, in place.

        Two lists that differ only by which printing they reference are the
        same deck; without this they hash differently and inflate the
        inclusion counts.
        """
        unresolved = []
        for s in SECTIONS:
            resolved = []
            for cid in getattr(self, s):
                row = card_db.resolve(conn, cid)
                if row is None:
                    unresolved.append(cid)
                    resolved.append(cid)
                else:
                    resolved.append(row["id"])
            setattr(self, s, resolved)
        return unresolved

    # --- legality ---------------------------------------------------------

    def validate(self, conn, region="tcg"):
        """Return a list of human-readable problems. Empty means legal."""
        problems = []

        if not MAIN_MIN <= len(self.main) <= MAIN_MAX:
            problems.append(
                f"main deck has {len(self.main)} cards (must be {MAIN_MIN}-{MAIN_MAX})"
            )
        if len(self.extra) > EXTRA_MAX:
            problems.append(f"extra deck has {len(self.extra)} cards (max {EXTRA_MAX})")
        if len(self.side) > SIDE_MAX:
            problems.append(f"side deck has {len(self.side)} cards (max {SIDE_MAX})")

        # Copy limits apply across main + extra + side combined.
        total = Counter(self.main) + Counter(self.extra) + Counter(self.side)
        for cid, n in total.items():
            row = card_db.resolve(conn, cid)
            name = row["name"] if row else f"unknown card {cid}"
            if row is None:
                problems.append(f"{name}: not in the card database")
                continue
            try:
                limit = card_db.banlist_limit(conn, cid, region)
            except ValueError as exc:
                # The mirror holds a status we cannot interpret. Report it
                # as a problem rather than letting it pass as legal.
                problems.append(f"{name}: {exc}")
                continue
            if n > limit:
                status = "forbidden" if limit == 0 else f"limit {limit}"
                problems.append(f"{name}: {n} copies but {status} on the {region} list")
            elif n > 3:
                # Only reachable if the banlist ever reports a limit above
                # three. Reported separately from the line above so the
                # same violation is never listed twice.
                problems.append(f"{name}: {n} copies (hard cap is 3)")

        # Section routing
        for cid in set(self.main):
            row = card_db.resolve(conn, cid)
            if row and (row["frame"] or "") in EXTRA_FRAMES:
                problems.append(f"{row['name']}: extra-deck card in the main deck")
        for cid in set(self.extra):
            row = card_db.resolve(conn, cid)
            if row and (row["frame"] or "") not in EXTRA_FRAMES:
                problems.append(f"{row['name']}: main-deck card in the extra deck")

        return problems

    # --- reporting --------------------------------------------------------

    def named(self, conn, section="main"):
        """[(name, copies)] sorted by copies desc then name."""
        out = []
        for cid, n in self.counts(section).items():
            row = card_db.resolve(conn, cid)
            out.append((row["name"] if row else f"<{cid}>", n))
        return sorted(out, key=lambda t: (-t[1], t[0]))
