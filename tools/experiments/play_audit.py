"""Is a batch of games clean? Every misplay pattern found 2026-11-20, counted.

Run it on a SMOKE batch before any long run (user, 2026-11-20: "make the
data as good as possible before doing long long runs"): each line should read
0 or near it, and a non-zero one is a bug to fix before the hours are spent.

  own-turn waste   a Maxx "C"-style draw card / Nibiru / Droll used on its
                   owner's own turn, when an opponent's turn still follows
                   (patch 0009 should make this 0; the game's last turn is free)
  self-negate      a chain link negated by its OWN controller's card (Ash onto
                   our own search; Sulliek/Trivikarma chained onto our link to
                   negate THEIR monster are not this)
  gifts            the enemy's monsters on our field when our turn begins;
                   `usable` = ones offering us an activation there; the gate
                   counts only those AVOIDABLE at the same goal progress
  free left        an interruption Trap / marked Quick-Play still in hand at
                   the end of its owner's turn (free sets should make this 0)
  dead targets     trained goals still asking for a card the deck can never
                   make work (drill.never_live)
  pieces           counted on the board at the start of the opponent's turn,
                   and how many of those fired there (piece_audit.py's table)
  hand discards    End Phase discards for hand size

    venv/bin/python tools/experiments/play_audit.py <enemy> first|second [device] [--since ISO] [--gate]
    --gate: exit 1 if a must-be-zero line is over GATE_SHARE of the games (day.sh uses it)
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DECK = "imported"


# every problem a game can be quarantined for (user, 2026-11-27: "if you find a problem with
# games then i dont want any of it in the data set"): the gate's lines AND trap-to-a-cost
PROBLEMS = ("own_turn_waste", "self_negate", "free_left", "hand_discards", "trap_to_cost",
            "avoidable_gift")
GATE_SHARE = 0.02
DEBUG = bool(__import__("os").environ.get("AUDIT_DEBUG"))     # --gate: a must-be-zero line over this share of the games fails


def _avoidable(t1_dir, deck):
    """A usable gift is a misplay only if some line of this turn 1 (the first
    solve and its re-solves, game._regift) reached the same goal progress
    without one: Angelechy Opposition's cost is otherwise forced (2026-11-21:
    the 3 usable gifts of the smoke batch's first 16 games had none)."""
    from skills import game
    lines = [p for d in [t1_dir / "out-t1", *sorted(t1_dir.glob("regift*/out-t1"))]
             for p in (sorted(d.glob("*.yrp")) if d.exists() else [])]
    if not lines:
        return False
    sols = [p for p in lines if p.name.startswith("solution")]
    pool = sols or [p for p in lines if game._approach_progress(p) == max(game._approach_progress(q) for q in lines)]
    return any(game.usable_gifts(p, 1, deck, 2) == 0 for p in pool)


def main(enemy, side, device="server", since=None, gate=False, quiet=False):
    """Prints the audit (unless `quiet`) and returns its counts as a dict
    (tools/ai_metrics.py logs them)."""
    import ygorl.engine.constants as C
    import ygorl.engine.messages as M
    from ygorl.engine.duel import default_cards
    from ygorl.engine.replay import Replay

    from skills import card_db, db, drill, engine, game, impact, quarantine, trainer

    first = side == "first"
    cards = default_cards()
    conn = db.connect()
    nm = lambda x: (card_db.resolve(conn, x) or {"name": str(x)})["name"]  # noqa: E731
    deck = engine.DECKS / f"{DECK}.ydk"
    enemy_deck = engine.DECKS / f"{enemy}.ydk"
    dh = trainer.deck_hash(deck)
    me = 0 if first else 1            # our engine seat in this layout
    say = (lambda *a, **k: None) if quiet else print
    dead_codes = {0: set(game.own_turn_dead(deck if me == 0 else enemy_deck)),
                  1: set(game.own_turn_dead(enemy_deck if me == 0 else deck))}
    prefs = trainer.prefs(conn, DECK)["interrupters"]
    free = {me: set(game.free_sets(deck, cards, prefs)), 1 - me: set(game.free_sets(enemy_deck, cards))}
    # a Trap that can be activated FROM THE HAND (Dominus: "you can activate this card from
    # your hand") is live where it is: holding it is not a free card left (2026-11-26: these
    # alone failed tonight's gate)
    for p_ in free:
        free[p_] = {c for c in free[p_] if not re.search(r"activate this card from your hand",
                                                          game.card_text(c), re.I)}
    q = ("SELECT * FROM game_results WHERE deck_hash=? AND enemy=? AND first=? AND device=? "
         "AND outcome != 'no_t1'" + (" AND created >= ?" if since else ""))
    games = conn.execute(q, (dh, enemy, int(first), device, *([since] if since else []))).fetchall()
    who = lambda p: "us" if p == me else "them"  # noqa: E731
    c_waste, c_selfneg, c_free, c_disc, c_costtrap = Counter(), Counter(), Counter(), Counter(), Counter()
    gifts = usable = avoidable = 0
    marked = trainer.prefs(conn, DECK if first else enemy)["interrupters"]
    counted, fired = Counter(), Counter()
    outcomes = Counter(g["outcome"] for g in games)
    flagged = []          # (game row, [problems]): every game with a must-be-zero hit
    for g in games:
        before = (sum(c_waste.values()), sum(c_selfneg.values()), sum(c_free.values()),
                  sum(c_disc.values()), sum(c_costtrap.values()), avoidable)
        src = impact.source(DECK, enemy, g)
        t1 = conn.execute("SELECT prefix FROM game_turn1 WHERE deck_hash=? AND enemy=? AND hand_index=? "
                          "AND board=? AND enemy_seed=? AND first=? AND device=?",
                          (dh, enemy, g["hand_index"], g["board"], g["enemy_seed"], int(first),
                           device)).fetchone()
        if not src or not (t1 and t1["prefix"] and Path(t1["prefix"]).exists()):
            continue
        start = game.replay(t1["prefix"], until_turn=2).board
        opp = 0 if first else 0     # the board facing turn 2's attacker: player 0's in both layouts
        # the board owner's own "counts as an interruption" cards count too (as game.run's)
        pieces = set(game._pieces(start, opp, cards, marked))
        counted.update(pieces)
        if not first:
            n = game.gifts(start, 1, deck)
            gifts += bool(n)
            u = bool(n) and game.usable_gifts(t1["prefix"], 1, deck, 2) > 0
            usable += u
            if u and _avoidable(Path(t1["prefix"]).parent.parent, deck):
                avoidable += 1
        rep = Replay.from_yrp(Path(src))
        state = {"turn": 0, "tp": None, "chain": [], "fired": set(), "solving": None,
                 "prompt": {}}     # player -> its last card prompt: (min, own non-hand-trap choices)
        last_turn = 3 if first else 2        # the game's final searched turn
        hands = {}

        class O:
            def on_start(self, core):
                pass

            def on_buffer(self, core, buf):
                for raw in M.split_messages(buf):
                    m = M.decode_message(raw)
                    k = type(m).__name__
                    if k in ("SelectCard", "SelectUnselectCard", "SelectTribute"):
                        opts = m.cards if k != "SelectUnselectCard" else m.selectable
                        state["prompt"][m.player] = (max(1, m.min), sum(
                            1 for o in opts if o.loc.controller == m.player
                            and not game.is_hand_trap(o.code, cards)))
                    if k in ("Chaining", "ChainSolved", "ChainEnd", "NewTurn"):
                        state["prompt"].clear()   # a cost's prompt follows its Chaining: no stale one
                    if k == "NewTurn":
                        state["turn"] += 1
                        state["tp"] = m.player
                    elif k == "ChainEnd":
                        state["chain"].clear()
                        state["solving"] = None
                    elif k == "ChainSolving":
                        state["solving"] = m.chain_count
                    elif k == "ChainSolved":
                        state["solving"] = None
                    elif k in ("ChainNegated", "ChainDisabled"):
                        ch, sv = state["chain"], state["solving"]
                        n = m.chain_count
                        # the negator: the link resolving above the victim (Ash), or
                        # the nearest link above it (Lars style, reported at the victim)
                        if n <= len(ch):
                            above = [ch[sv - 1]] if sv and n < sv <= len(ch) else ch[n:]
                            if above and above[0][1] == ch[n - 1][1]:
                                c_selfneg[(who(ch[n - 1][1]), f"{nm(above[0][0])} onto {nm(ch[n - 1][0])}")] += 1
                    elif k == "Chaining":
                        ch = state["chain"]
                        del ch[m.chain_count - 1:]
                        p = m.loc.controller
                        if (p == state["tp"] and m.loc.location & C.LOCATION_HAND
                                and m.code in dead_codes[p] and state["turn"] < last_turn):
                            c_waste[(who(p), nm(m.code))] += 1
                        if state["turn"] == 2 and p == opp and m.code in pieces:
                            state["fired"].add(m.code)
                        ch.append((m.code, p))
                    elif k == "Move" and m.previous.location & C.LOCATION_HAND \
                            and m.reason & C.REASON_RULE and m.reason & C.REASON_DISCARD:
                        c_disc[(who(m.previous.controller), nm(m.code))] += 1
                    elif (k == "Move" and m.previous.location & C.LOCATION_HAND
                          and m.reason & C.REASON_COST and m.previous.controller != state["tp"]
                          and game.is_hand_trap(m.code, cards)):
                        # the ANSWERING side paid a cost with a hand trap (a 0-ATK card is the
                        # scripted defender's first pick: patch 0020 --opp-keep, not live yet) --
                        # unless it is the card's OWN activation cost (Ash discards itself): held
                        # until the next chain link says which
                        ch = state["chain"]
                        # forced (2026-11-27): the cost prompt offered fewer of its other
                        # cards than the cost needed -- patch 0020 gives a hand trap only then
                        need, other = state["prompt"].get(m.previous.controller, (1, 1))
                        if DEBUG:
                            print("  cost", nm(m.code), "game", g["hand_index"], g["enemy_seed"], g["run"],
                                  "need", need, "other own choices", other, file=sys.stderr)
                        if other < need:
                            pass
                        elif not (ch and ch[-1][0] == m.code):     # not its own activation's cost
                            c_costtrap[(who(m.previous.controller), nm(m.code))] += 1

        rep.duel().replay(rep.responses, observer=O())
        fired.update(state["fired"])
        # free cards left in hand at the end of a turn an opponent's turn follows
        # (going first, their turn-2 leftovers matter only if the game reached turn 3)
        for p, turn in (((0, 1),) + (((1, 2),) if g["outcome"] != "lost_t2" else ())
                        if first else ((0, 1),)):
            pos = start if turn == 1 else None
            if turn == 2:
                # the position turn 3 STARTED from (the game's closed turn 2): replaying the
                # final line read the hand at the END of our turn 3, where cards it added there
                # (a Verdict returned on our turn) were counted as left on turn 2 (2026-11-27)
                t3 = Path(src).parent.parent / "t2" / "prefix.yrpX"
                try:
                    pos = game.replay(t3 if t3.exists() else src, until_turn=3).board
                except Exception:  # noqa: BLE001 -- a game that ended on turn 2
                    continue
            hand = (pos["players"][p].get("hand") or []) if pos else []
            on = {m["code"] for z in ("mzone", "szone") for m in (pos["players"][p].get(z) or []) if m} if pos else set()
            fzone = bool(pos) and any(m and m.get("sequence") == 5 for m in pos["players"][p].get("szone") or [])
            full = bool(pos) and sum(1 for m in pos["players"][p].get("szone") or []
                                     if m and m.get("sequence", 9) < 5) >= 5
            if full:
                continue                    # all five Spell / Trap zones taken: nothing could go down
            for c in hand:
                # a Field Spell with the field zone taken (or its 1-per-turn copy already out)
                # could not be activated: not left by choice
                if c in free[p] and not (game._field_spell(c) and (fzone or c in on)):
                    c_free[(who(p), nm(c))] += 1
        after = (sum(c_waste.values()), sum(c_selfneg.values()), sum(c_free.values()),
                 sum(c_disc.values()), sum(c_costtrap.values()), avoidable)
        probs = [k for k, a, b in zip(PROBLEMS, before, after) if b > a]
        if probs:
            flagged.append((g, probs))

    goals = [r for r in conn.execute("SELECT hand_index, best_goal FROM trainer_hands WHERE deck_hash=? "
                                     "AND best_goal IS NOT NULL", (dh,))]
    from skills.deck import Deck
    d = Deck.from_ydk(deck)
    never = {str(c) for c in drill.never_live([*d.main, *d.extra])}
    dead_t = [(r["hand_index"], t) for r in goals for t in trainer.split_goal(json.loads(r["best_goal"]))[0]
              if t.split("@")[0] in never]

    n = len(games)
    say(f"== {DECK} vs {enemy} going {side} [{device}]{' since ' + since if since else ''}: {n} games "
          f"{dict(outcomes)}")
    def line(label, cnt, extra=""):
        tot = sum(cnt.values())
        say(f"  {label:15} {tot:4}{extra}  " + ", ".join(f"{w} {c} {k}" for (w, c), k in cnt.most_common(4)))
    line("own-turn waste", c_waste)
    line("self-negate", c_selfneg)
    if not first:
        say(f"  {'gifts':15} {gifts:4} games with a monster given (usable by us: {usable}, "
              f"avoidable at the same goal progress: {avoidable})")
    line("free left", c_free)
    line("hand discards", c_disc)
    line("trap to a cost", c_costtrap)
    say(f"  {'dead targets':15} {len(dead_t):4} in stored goals {dead_t[:4]} (the games strip them: live_goal)")
    bad = {"own-turn waste": sum(c_waste.values()), "self-negate": sum(c_selfneg.values()),
           "free left": sum(c_free.values()), "hand discards": sum(c_disc.values()),
           # must-be-zero since patch 0020 went live (METHOD 2026-11-27a)
           "trap to a cost": sum(c_costtrap.values())}
    if not first:
        bad["avoidable usable gifts"] = avoidable
    say(f"  pieces (start of turn 2, the {'our' if first else 'enemy'} board): counted -> fired there")
    for code, k in counted.most_common(8):
        flag = "   <-- never fires" if k >= 5 and fired[code] == 0 else ""
        say(f"    {nm(code)[:34]:34} {k:4} -> {fired[code]:4} ({fired[code] / k:.0%}){flag}")
    result = {"games": n, "outcomes": dict(outcomes),
              "own_turn_waste": sum(c_waste.values()), "self_negate": sum(c_selfneg.values()),
              "free_left": sum(c_free.values()), "hand_discards": sum(c_disc.values()),
              "gifts": gifts if not first else None, "usable_gifts": usable if not first else None,
              "avoidable_gifts": avoidable if not first else None,
              "pieces_counted": sum(counted.values()), "pieces_fired": sum(fired.values()),
              "flagged": len(flagged)}
    quarantine.record(conn, enemy, first, device, flagged)
    say(f"  quarantined     {len(flagged):4} games kept out of training (bad_games)")
    if gate:
        over = {k: v for k, v in bad.items() if n and v > GATE_SHARE * n}
        if not n:
            say("GATE: no games to audit")
            raise SystemExit(2)
        say(f"GATE: {'FAILED ' + str(over) if over else 'passed'} (limit {GATE_SHARE:.0%} of {n} games)")
        if over:
            raise SystemExit(1)
    return result


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else None
    if since:
        args.remove(since)
    if len(args) < 2:
        print(__doc__)
        raise SystemExit(1)
    main(*args, since=since, gate="--gate" in sys.argv)
