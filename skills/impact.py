"""Impact score per card, from the games (STAGE9).

A card's impact against one enemy deck is how much more often you win the
games where you USED it -- summoned it, or activated it, on any of your
turns or in response on theirs -- than the games where the SAME opening
hand, against the SAME enemy hand, got through without it:

    impact = sum_s w_s (win%_used,s - win%_not,s) / sum_s w_s
    w_s    = n_used,s * n_not,s / (n_used,s + n_not,s)

over strata s = (your hand, their hand). A raw "won when used" rate
measures the hands a card turns up in, not the card: Engraver is in the
good hands, so it would look strong whatever it did. Your hand alone is
not enough either -- measured 2026-11-02 on imported vs angelechy, Ash
Blossom scored -67 by hand and +1 by (hand, enemy hand): you hold Ash for
their strong hands, so "used Ash" meant "faced a strong hand". Only a
stratum that played some games with the card and some without says
anything, and w_s is the weight a difference of two proportions earns
from those sizes (Mantel-Haenszel). The interval uses Agresti-Caffo
(+1 win, +1 loss per side), so 5/5 against 0/5 is not "+100 +- 0".

What it cannot see, said where it is shown:
- A card never used has no score -- not a score of 0. A dead card in hand
  ("Trivikarma") is "not measured", not "bad".
- A card used in EVERY game of every stratum holding it cannot be compared
  either; it is listed as "every game". Turn 1 is solved once per (hand,
  enemy hand) and reused by every run, so a card used only on turn 1 always
  lands here: its variation lives on turns 2 and 3.
- Most cards score above 0: a game where more of your deck came out is a
  game that went well. Read the scores against each other.
- It is per enemy deck. And it is correlation inside a hand, not a
  controlled swap: a card that only comes out when the enemy's hand trap
  missed shares the credit for that miss.
"""

import json
import math
import time
from pathlib import Path

from . import game

MIN_SIDE = 5        # games on EACH side (used / not) before a score is shown
_KINDS = ("Summoning", "SpSummoning", "FlipSummoning", "Chaining")


def deck_codes(deck_path):
    """Every card code of a decklist (main + extra), canonical and as written."""
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards

    d, cards = load_ydk(Path(deck_path)), default_cards()
    codes = set(d.main) | set(d.extra)
    return codes | {cards.canonical(c) for c in codes}


def used_cards(path, player=0):
    """{code: first turn} of the cards `player` summoned or activated in
    the game replay at `path` (every turn it plays)."""
    import ygorl.engine.messages as M
    from ygorl.engine.replay import Replay

    rep = Replay.from_yrp(Path(path))
    used, turn = {}, [0]

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                k = type(m).__name__
                if k == "NewTurn":
                    turn[0] += 1
                elif k in _KINDS and m.code and m.loc.controller == player:
                    used.setdefault(m.code, turn[0])

    rep.duel().replay(rep.responses, observer=_Obs())
    return used


def source(name, enemy, g):
    """The replay a game is read from: its deciding line, or -- a game
    with no line for its lethal turn -- the position that turn started
    from, as /watch shows it (after turn 2 going first, after their turn 1
    going second)."""
    if g["line"] and Path(g["line"]).exists():
        return g["line"]
    d = game.games_dir(name, enemy, g["first"], g["device"]) / f"h{g['hand_index']}" / f"e{g['enemy_seed']}"
    before = (d / f"run{g['run']}" / "t2" / "prefix.yrpX" if g["first"]
              else d / "t1" / "t1" / "prefix.yrpX")
    return str(before) if before.exists() else None


def record(conn, dh, name, enemy, g):
    """Read one game and cache what you used. Returns the dict or None."""
    src = source(name, enemy, g)
    used = used_cards(src, player=0 if g["first"] else 1) if src else None
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO game_used (deck_hash, enemy, hand_index, "
            "board, enemy_seed, first, device, run, line, used) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (dh, enemy, g["hand_index"], g["board"], g["enemy_seed"], g["first"],
             g["device"], g["run"],
             g["line"] or "", None if used is None else
             json.dumps({str(k): v for k, v in used.items()})))
    return used


def fill(conn, dh, name, enemy, budget_s=None, say=None, first=True, device="server"):
    """Cache every game not read yet (or re-recorded since). With
    `budget_s`, stop after that long. Returns how many are still unread."""
    todo = conn.execute(
        "SELECT g.* FROM game_results g LEFT JOIN game_used u USING "
        "(deck_hash, enemy, hand_index, board, enemy_seed, first, device, run) "
        "WHERE g.deck_hash=? AND g.enemy=? AND g.first=? AND g.device=? AND g.outcome != 'no_t1' "
        "AND (u.deck_hash IS NULL OR u.line != COALESCE(g.line, ''))",
        (dh, enemy, int(bool(first)), device)).fetchall()
    t0 = time.monotonic()
    for i, g in enumerate(todo):
        if budget_s is not None and time.monotonic() - t0 > budget_s:
            return len(todo) - i
        try:
            record(conn, dh, name, enemy, g)
        except Exception as exc:    # one unreadable replay never hides the rest
            if say:
                say(f"  !! hand {g['hand_index'] + 1} game {g['run'] + 1}: {exc}")
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO game_used (deck_hash, enemy, hand_index, "
                    "board, enemy_seed, first, device, run, line, used) "
                    "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (dh, enemy, g["hand_index"], g["board"], g["enemy_seed"],
                     g["first"], g["device"], g["run"], g["line"] or ""))
    return 0


def compare(strata, min_side=MIN_SIDE):
    """Win rate with a thing vs without it, compared only inside each
    stratum: `strata` is [(wins with, wins without), ...], lists of bools.

    {impact, ci, compared, n_yes, yes_win, no_win, status}: the
    Mantel-Haenszel risk difference with an Agresti-Caffo 95% interval
    (module docstring). `yes_win` / `no_win` are raw rates over the
    compared games only (over every "yes" game when nothing compares)."""
    num = den = var = 0.0
    compared = n_yes = n_y = n_n = w_yes = w_y = w_n = 0
    for u, n in strata:
        n_yes, w_yes = n_yes + len(u), w_yes + sum(u)
        if not u or not n:
            continue
        pu, pn = sum(u) / len(u), sum(n) / len(n)
        w = len(u) * len(n) / (len(u) + len(n))
        num += w * (pu - pn)
        den += w
        au, an = (sum(u) + 1) / (len(u) + 2), (sum(n) + 1) / (len(n) + 2)
        var += w * w * (au * (1 - au) / (len(u) + 2) + an * (1 - an) / (len(n) + 2))
        compared += len(u) + len(n)
        n_y, w_y = n_y + len(u), w_y + sum(u)
        n_n, w_n = n_n + len(n), w_n + sum(n)
    if not den:
        status, impact, ci = "every game", None, None
    else:
        impact = num / den
        ci = 1.96 * math.sqrt(var) / den
        status = "scored" if min(n_y, n_n) >= min_side else "few games"
    return {"impact": impact, "ci": ci, "compared": compared, "n_yes": n_yes,
            # both over the COMPARED games: the raw rates beside the
            # score, on the same footing as it
            "yes_win": w_y / n_y if n_y else w_yes / n_yes if n_yes else None,
            "no_win": w_n / n_n if n_n else None, "status": status}


def scores(conn, dh, enemy, min_side=MIN_SIDE, own=None, first=True, device="server"):
    """One row per card you used in any game against `enemy`:

    {code, games, used, impact, ci, compared, used_win, not_win, turn, status}

    `used_win` / `not_win` are raw win rates over the compared games only
    (all games it was used in, for "every game").
    `impact` and `ci` (95%) are in win-rate points (0.12 = +12 points);
    `status` is "scored", "few games" (under `min_side` comparable games on
    a side), or "every game" (no hand ever went without it).

    `own`: your deck's card codes -- only those are scored. A card of
    theirs can be yours for a moment (Angelechy Enlisted moves itself to
    your field and its next trigger is yours), and controller alone would
    credit it to you."""
    from collections import Counter, defaultdict

    games = []
    for r in conn.execute(
            "SELECT g.hand_index, g.enemy_seed, g.outcome, u.used FROM game_results g JOIN "
            "game_used u USING (deck_hash, enemy, hand_index, board, enemy_seed, first, device, run) "
            "WHERE g.deck_hash=? AND g.enemy=? AND g.first=? AND g.device=? AND g.outcome != 'no_t1' "
            "AND u.used IS NOT NULL AND u.line = COALESCE(g.line, '')",
            (dh, enemy, int(bool(first)), device)):
        used = {int(k): v for k, v in json.loads(r["used"]).items()}
        if own is not None:
            used = {k: v for k, v in used.items() if k in own}
        games.append(((r["hand_index"], r["enemy_seed"]), r["outcome"] == "win", used))
    by_hand = defaultdict(list)
    for h, won, used in games:
        by_hand[h].append((won, used))
    cards = {c for _h, _w, u in games for c in u}

    out = []
    for c in cards:
        turns = Counter(used[c] for rows in by_hand.values()
                        for _won, used in rows if c in used)
        r = compare([([won for won, used in rows if c in used],
                       [won for won, used in rows if c not in used])
                      for rows in by_hand.values()], min_side)
        used_n, used_win, not_win = r.pop("n_yes"), r.pop("yes_win"), r.pop("no_win")
        out.append({"code": c, "games": len(games), "used": used_n, **r,
                    "used_win": used_win, "not_win": not_win,
                    "turn": turns.most_common(1)[0][0] if turns else None})
    order = {"scored": 0, "few games": 1, "every game": 2}
    out.sort(key=lambda r: (order[r["status"]], -(r["impact"] or 0), -r["used"]))
    return out
