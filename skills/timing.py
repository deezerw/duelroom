"""Best time to use: what each of your interruptions did best on their turn.

A first, correlational reading of the stored games ("Effect Veiler: best on
<card>"). For each of your cards and each enemy card it stopped on THEIR
turn (turn 2 going first, turn 1 going second; stops.Reader's stops), the
win rate when it stopped that card against the games where you had it and it
stopped nothing -- compared inside each (your hand, their hand), as
`impact.compare` does.

"Had it": in your opening hand (a hand trap), or a piece of your turn-1 board
going first (`game_turn1.pieces`). Going second your board is not there yet:
only the opening hand counts.

A HINT, not a rating: the solver picks its targets for a reason, so a target
that "wins more" may only show up in positions that were winning anyway. The
causal rating replays each choice at the same window and re-searches the
attacker (patch 0006 --opp-branches; the user's end-game goal).
"""

import json
from collections import defaultdict

from . import impact, stops


def scores(conn, dh, enemy, own=None, first=True, device="server",
           min_side=impact.MIN_SIDE):
    """[{code, had, used, targets: [{code, n, impact, ci, status, yes_win,
    no_win}]}] -- one row per card of yours that stopped something on their
    turn, targets best first."""
    turn = 2 if first else 1
    hands = {r["hand_index"]: json.loads(r["hand"]) for r in conn.execute(
        "SELECT hand_index, hand FROM trainer_hands WHERE deck_hash=?", (dh,))}
    pieces = {}
    if first:
        pieces = {(r["hand_index"], r["enemy_seed"]): set(json.loads(r["pieces"] or "[]"))
                  for r in conn.execute(
                      "SELECT hand_index, enemy_seed, pieces FROM game_turn1 WHERE deck_hash=? "
                      "AND enemy=? AND first=1 AND device=? AND ok=1", (dh, enemy, device))}
    games = []
    for r in conn.execute(
            "SELECT g.hand_index, g.enemy_seed, g.outcome, s.stops FROM game_results g "
            "JOIN game_stops s USING (deck_hash, enemy, hand_index, board, enemy_seed, first, device, run) "
            "WHERE g.deck_hash=? AND g.enemy=? AND g.first=? AND g.device=? AND g.outcome != 'no_t1' "
            "AND s.stops IS NOT NULL AND s.line = COALESCE(g.line, '')",
            (dh, enemy, int(bool(first)), device)):
        pos = (r["hand_index"], r["enemy_seed"])
        had = set(hands.get(r["hand_index"], [])) | pieces.get(pos, set())
        hits = defaultdict(set)
        for t, _kind, by, code in json.loads(r["stops"])["stops"]:
            if t == turn and by != stops.FIELD and (own is None or by in own):
                hits[by].add(code)
        games.append((pos, r["outcome"] == "win", had, hits))

    out = []
    for card in sorted({c for *_x, hits in games for c in hits}):
        mine = [g for g in games if card in g[2] or card in g[3]]
        targets = []
        for target in sorted({t for *_x, hits in mine for t in hits.get(card, ())}):
            strata = defaultdict(lambda: ([], []))
            for pos, won, _had, hits in mine:
                if target in hits.get(card, ()):
                    strata[pos][0].append(won)
                elif not hits.get(card):            # had it, stopped nothing
                    strata[pos][1].append(won)
            r = impact.compare(list(strata.values()), min_side)
            targets.append({"code": target, "n": r.pop("n_yes"), **r})
        order = {"scored": 0, "few games": 1, "every game": 2}
        targets.sort(key=lambda t: (order[t["status"]], -(t["impact"] or 0), -t["n"]))
        out.append({"code": card, "had": len(mine),
                    "used": sum(1 for g in mine if g[3].get(card)), "targets": targets})
    out.sort(key=lambda r: -r["used"])
    return out


# --- measured: fire or hold at the same window --------------------------------

MEASURED_FOR = ("imported", "Angelechy-Meta")    # use_timing.py's DECK, ENEMY


def measured_file(first=True):
    """use_timing.py's results: going first (their kill check), or going
    second (`use_timing.py second`: their turn 1, judged by your break)."""
    from .engine import ROOT
    d = ROOT / "results" / "experiments" / "use_timing"
    return d / ("results.jsonl" if first else "results_second.jsonl")


def measured(path=None, min_windows=3, first=True):
    """The causal rating (tools/experiments/use_timing.py): per (your card,
    their trigger), their kill rate when you fired it at a window against
    when you held it at the SAME window, paired. [{piece, trigger, n,
    fired, held, gain, ci}], biggest gain first; gain = held - fired (points
    of their kill rate you remove by firing). Windows where the line and
    its branch both fired (piece vs piece) are not fire-or-hold: left out.
    None when nothing is measured yet."""
    import math

    path = path or measured_file(first)
    if not path.exists():
        return None
    pair = defaultdict(list)
    level = None
    for d in map(json.loads, path.read_text().splitlines()):
        fired = d["chosen"] or d["alt"]
        if not fired or (d["chosen"] and d["alt"]):
            continue
        own = d.get("control_lost")      # the line's reply, re-searched from the window
        if own is None:
            continue
        level = d.get("level", level)
        lost_fire = own if d["chosen"] else d["branch_lost"]
        lost_hold = d["branch_lost"] if d["chosen"] else own
        pair[(fired, d["trigger"])].append((lost_fire, lost_hold))
    rows = []
    for (piece, trig), xs in pair.items():
        n = len(xs)
        diffs = [h - f for f, h in xs]
        mean = sum(diffs) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in diffs) / (n - 1)) if n > 1 else 1.0
        rows.append({"piece": piece, "trigger": trig, "n": n,
                     "fired": sum(f for f, _ in xs) / n, "held": sum(h for _, h in xs) / n,
                     "gain": mean, "ci": 1.96 * max(sd, 0.5 / math.sqrt(n)) / math.sqrt(n),
                     "few": n < min_windows})
    rows.sort(key=lambda r: (r["few"], -r["gain"], -r["n"]))
    return {"rows": rows, "level": level, "windows": sum(r["n"] for r in rows)}
