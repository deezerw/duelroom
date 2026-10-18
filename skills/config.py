"""locals.toml: who you actually face.

Top tournament lists are the optimised, funded, fully-teched field. Locals
run budget builds, last format's deck, and rogue piles. Every gauntlet and
every optimizer objective weights by this file rather than by global meta
share -- otherwise you tune perfectly for a room you never sit in.

The file names archetypes the way the `archetypes` command does ("Kewl
Tune"), while deck files and engine results are keyed by filename stem
("kewl-tune"). `slug` reconciles the two so neither side has to care.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALS = ROOT / "locals.toml"


def slug(name):
    """'Kewl Tune' and 'kewl-tune' both -> 'kewl-tune'."""
    return "-".join(str(name).lower().replace("-", " ").split())


def load(path=None):
    path = Path(path) if path else LOCALS
    if not path.exists():
        return {"meta": {}, "weights": {}}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def weights(path=None, normalise=True):
    """Archetype share, keyed by slug. Normalised to sum to 1 by default."""
    raw = (load(path).get("weights") or {})
    out = {slug(k): float(v) for k, v in raw.items()}
    if normalise and out:
        total = sum(out.values())
        if total:
            out = {k: v / total for k, v in out.items()}
    return out


def weight_for(deck_name, path=None):
    return weights(path).get(slug(deck_name), 0.0)


def coverage(deck_names, path=None):
    """How much of your stated locals meta the measured decks cover.

    A handtrap ranking weighted over 40% of your room is a ranking over
    40% of your room, and should say so.
    """
    w = weights(path)
    have = {slug(d) for d in deck_names}
    covered = sum(v for k, v in w.items() if k in have)
    missing = sorted(k for k in w if k not in have and w[k] > 0)
    return covered, missing
