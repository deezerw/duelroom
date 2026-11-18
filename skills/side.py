"""Side cards: does swapping card Y for card X help, against what? (STAGE13.md)

A side config is the main deck with swaps (`out` -> `in`, n copies). It is
measured as a PAIRED experiment on the games harness (game.py): the same
trained hands, the same enemy hands, the same seeds; the only difference is
the swapped cards, put exactly where the old ones were:

- in each trained hand that held Y, X takes its place;
- in the deck order each hand was trained on (`trainer.hand_start`), the
  same copies of Y become X -- the tail first, which is the hand, then
  from the bottom up -- so every other draw, mill and shuffle is the one
  the main deck had. A Tearlaments line that milled Y now mills X, which
  is exactly what siding Y out does in a real game.

Every hand is played again (its turn 1 is re-searched: a line may have
searched or milled Y), so the comparison costs a full games run per
config; `runs` defaults to 2 and is compared with runs 0..1 of the main
deck's games, which used the same seeds (eseed * 100 + run).

A goal target naming Y is dropped when no copy of Y is left in the deck,
or when it is a held card ("Y@hand") of a hand that no longer holds Y;
those hands are counted (`goals_changed`), since their turn 1 aims at a
different board than the main deck's did.

The config lives as its own deck (assets/decks/side/<name>.ydk) with its
own deck hash, so its games are stored and summarised like any deck's,
and never mix with the main deck's.
"""

import json
from pathlib import Path

from . import engine, stats, trainer
from .deck import Deck

SIDE_DIR = engine.ROOT / "assets" / "decks" / "side"
RUNS = 2


def config_name(base, swaps):
    """`imported~-2x14558127+2x15693423` -- readable, unique per swap set."""
    parts = sorted(f"-{n}x{o}+{n}x{i}" for o, i, n in swaps)
    return f"{base}~{''.join(parts)}"


def _swap_list(codes, out_code, in_code, n, from_tail=True):
    """`codes` with up to n copies of out_code replaced by in_code, the tail
    first. Returns (new list, how many were replaced)."""
    codes = list(codes)
    idx = [i for i, c in enumerate(codes) if c == out_code]
    if from_tail:
        idx.reverse()
    for i in idx[:n]:
        codes[i] = in_code
    return codes, min(n, len(idx))


def _target_code(t):
    head = str(t).split("@")[0].split(":")[0]
    try:
        return int(head)
    except ValueError:
        return None


def deck_file(name):
    """A config (its name holds "~") lives under SIDE_DIR, a deck in assets/decks."""
    return (SIDE_DIR if "~" in name else engine.ROOT / "assets" / "decks") / f"{name}.ydk"


def make(conn, base_name, swaps, base_path=None):
    """Write the config deck and its trained hands. `swaps`: [(out, in, n)].
    Returns {name, path, deck_hash, hands, hands_touched, goals_changed}.
    Idempotent: the same swaps give the same name and the same rows."""
    base_path = Path(base_path or engine.ROOT / "assets" / "decks" / f"{base_name}.ydk")
    d = Deck.from_ydk(base_path)
    main = [int(c) for c in d.main]
    for o, i, n in swaps:
        have = main.count(int(o))
        if have < n:
            raise ValueError(f"{base_name} has {have} of {o}, cannot side out {n}")
        main, _ = _swap_list(main, int(o), int(i), n, from_tail=False)
    name = config_name(base_name, swaps)
    SIDE_DIR.mkdir(parents=True, exist_ok=True)
    path = SIDE_DIR / f"{name}.ydk"
    Deck(name=name, main=main, extra=list(d.extra), side=list(d.side)).write_ydk(path)
    dh_base, dh = trainer.deck_hash(base_path), trainer.deck_hash(path)
    gone = {int(o) for o, _i, _n in swaps if int(o) not in main}

    touched = changed = n_hands = 0
    rows = conn.execute("SELECT * FROM trainer_hands WHERE deck_hash=? AND "
                        "best_goal IS NOT NULL ORDER BY hand_index", (dh_base,)).fetchall()
    with conn:
        for r in rows:
            hand = [int(c) for c in json.loads(r["hand"])]
            rec = json.loads(r["best_record"]) if r["best_record"] else None
            order = trainer.hand_start(rec)["order"] if rec else None
            new_hand, lost = list(hand), set()
            for o, i, n in swaps:
                new_hand, k = _swap_list(new_hand, int(o), int(i), n)
                if k:
                    lost.add(int(o))
                if order:
                    # the same copies: the hand's (the tail) first, then
                    # the rest from the bottom of the deck up
                    order, _ = _swap_list(order, int(o), int(i), n)
            if order and sorted(order) != sorted(main):
                raise RuntimeError(f"hand {r['hand_index']}: the swapped order is not "
                                   f"the config's main deck")
            goal = json.loads(r["best_goal"])
            keep = [t for t in goal
                    if not (_target_code(t) in gone
                            or (_target_code(t) in lost and str(t).endswith("@hand")))]
            touched += new_hand != hand
            changed += keep != goal
            if rec:
                rec["start"]["decks"]["a"]["main"] = order
            conn.execute(
                "INSERT OR REPLACE INTO trainer_hands (deck_hash, deck, hand_index, "
                "hand_seed, hand, best_key, best_goal, best_record, best_gen, "
                "best_source, best_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (dh, name, r["hand_index"], r["hand_seed"], json.dumps(new_hand),
                 r["best_key"], json.dumps(keep), json.dumps(rec) if rec else None,
                 r["best_gen"], f"side config of {base_name}", r["best_at"]))
            n_hands += 1
        # The deck's trainer choices (interrupters named, values, avoids)
        # carry over: game.run reads them by deck name.
        conn.execute("DELETE FROM trainer_prefs WHERE deck=?", (name,))
        conn.execute("INSERT INTO trainer_prefs SELECT ?, kind, code, zone, added FROM "
                     "trainer_prefs WHERE deck=?", (name, base_name))
        conn.execute("DELETE FROM card_values WHERE deck=?", (name,))
        conn.execute("INSERT INTO card_values SELECT ?, code, zone, value, added FROM "
                     "card_values WHERE deck=?", (name, base_name))
    return {"name": name, "path": path, "deck_hash": dh, "hands": n_hands,
            "hands_touched": touched, "goals_changed": changed}


def compare(conn, base_name, name, enemy, first=True, device="server", runs=RUNS):
    """Config `name` minus `base_name` (the main deck, or another config), paired per (hand, enemy hand) over runs
    0..runs-1: win-rate change (and killed-on-turn-2 change going first)
    with a 95% interval resampling hands and enemy hands. Only pairings
    both have played count. Returns None until any exist."""
    dh_base, dh = trainer.deck_hash(deck_file(base_name)), trainer.deck_hash(deck_file(name))

    def cells(h):
        out = {}
        for g in conn.execute(
                "SELECT hand_index, enemy_seed, outcome FROM game_results WHERE "
                "deck_hash=? AND enemy=? AND first=? AND device=? AND run < ?",
                (h, enemy, int(bool(first)), device, runs)):
            out.setdefault((g["hand_index"], g["enemy_seed"]), []).append(g["outcome"])
        return out
    a, b = cells(dh_base), cells(dh)
    both = [k for k in b if k in a and len(a[k]) == len(b[k]) == runs]
    if not both:
        return None

    def diff(what):
        d = {k: [sum(o == what for o in b[k]) / runs - sum(o == what for o in a[k]) / runs]
             for k in both}
        return stats.crossed_bootstrap(d, bounds=(-1.0, 1.0))

    out = {"pairings": len(both), "win": diff("win"),
           "base_win": sum(o == "win" for k in both for o in a[k]) / (runs * len(both)),
           "side_win": sum(o == "win" for k in both for o in b[k]) / (runs * len(both))}
    if first:
        out["lost_t2"] = diff("lost_t2")
    lo, hi = out["win"][1], out["win"][2]
    out["verdict"] = "helps" if lo > 0 else "hurts" if hi < 0 else "can't tell yet"
    return out
