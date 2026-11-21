"""Patch 0011 A/B: do our negate-only pieces waiting for a live target save boards?

Traced 2026-11-21: on Angelechy's turn 2, Evolzar Lars negated a Normal Summoned
Mulcharmy Purulia (no field effect) in 5 of its 12 uses and Dragostapelia put
its counter on a hand-trap body in 10 of 73, all lost games. Patch 0011 holds
those pieces while every face-up card of the attacker is such a body
(`game.NEGATE_ONLY`, `game.field_dead`).

Same stored going-first turn-1 positions (the board holds Lars or
Dragostapelia), the games' kill check (every DEFENCES script, a loss needs all
beaten), same seeds; arm "old" = the live binary, "new" = the 0011 build.

    nice -n 19 venv/bin/python tools/experiments/negate_live_ab.py [x]   # x: kill budget multiple (1)
    venv/bin/python tools/experiments/negate_live_ab.py summary
Resumable (results.jsonl).
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from skills import db, engine, game, trainer  # noqa: E402

ENEMY, DEVICE = "Angelechy-Meta", "duelpc"
NEW_BIN = ROOT / "vendor/ygorl/build/combo-solver-0011/bin/combosolver"
OUT = ROOT / "results" / "experiments" / "negate_live_ab"
PIECES = set(game.NEGATE_ONLY)


def kill(arm, t1, ours, enemy_deck, enemy_target, scratch, ms):
    game.NEGATE_LIVE = arm == "new"
    binary = NEW_BIN if arm == "new" else game.ROOTED_BIN
    base = ["--start", str(t1["start"]), "--root", str(t1["prefix"]), "--no-ref",
            "--target", enemy_target, "--player", "1", "--turns", "2"] + game.own_turn_args(enemy_deck)
    lost, holds = True, 0
    for v in game.DEFENCES:
        k = game._solve(t1["prefix"], base + game.defence_args(ours, v)
                        + ["--goal-win", "--stop-on-win", str(game.STOP_ON_WIN)],
                        scratch, f"{arm}-h{v}", ms, t1["enemy_seed"] * 100, binary=binary)
        text = Path(k["log"]).read_text(errors="replace")
        holds += sum(int(l.split("defender:")[1].split()[0]) for l in text.splitlines()
                     if "negate(s) held" in l)
        if not (k["solution"] and game._winner(k["solution"]) == 1):
            lost = False
    return lost, holds


def main(x=1):
    from ygorl.engine.duel import default_cards
    conn = db.connect()
    cards = default_cards()
    deck, enemy_deck = engine.DECKS / "imported.ydk", engine.DECKS / f"{ENEMY}.ydk"
    dh = trainer.deck_hash(deck)
    enemy_target = game.scenarios.target_for(ENEMY)
    ours_hand = set(game.respond_codes(deck, cards, extra=trainer.prefs(conn, "imported")["interrupters"]))
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=1 AND device=? AND ok=1 "
        "ORDER BY hand_index, enemy_seed", (dh, ENEMY, DEVICE))]
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["hand"], d["eseed"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    ms = game.OTK_MS * int(x)
    todo = []
    for r in rows:
        if not (r["prefix"] and Path(r["prefix"]).exists()):
            continue
        board = game.replay(r["prefix"], until_turn=2).board
        pieces = set(game._pieces(board, 0, cards))
        on = {m["code"] for m in board["players"][0]["mzone"]}
        if on & PIECES:
            todo.append((r, pieces | ours_hand | (on & PIECES)))
    print(f"{len(todo)} positions with Lars / Dragostapelia, kill check {ms // 1000} s a defence", flush=True)
    for r, ours in todo:
        t1 = {"start": r["start"], "prefix": r["prefix"], "enemy_seed": r["enemy_seed"]}
        for arm in ("old", "new"):
            if (r["hand_index"], r["enemy_seed"], arm) in done:
                continue
            scratch = OUT / "scratch" / f"h{r['hand_index']}-e{r['enemy_seed']}-{arm}"
            try:
                lost, holds = kill(arm, t1, ours, enemy_deck, enemy_target, scratch, ms)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            rec = {"hand": r["hand_index"], "eseed": r["enemy_seed"], "arm": arm, "lost": int(lost),
                   "holds": holds, "ms": ms}
            with res.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            print(json.dumps(rec), flush=True)
    summary()


def summary():
    f = OUT / "results.jsonl"
    if not f.exists():
        print("no results yet")
        return
    by = {}
    for d in map(json.loads, f.read_text().splitlines()):
        by.setdefault((d["hand"], d["eseed"]), {})[d["arm"]] = d
    pairs = [v for v in by.values() if "old" in v and "new" in v]
    n = max(1, len(pairs))
    old = sum(v["old"]["lost"] for v in pairs)
    new = sum(v["new"]["lost"] for v in pairs)
    saved = sum(1 for v in pairs if v["old"]["lost"] and not v["new"]["lost"])
    worse = sum(1 for v in pairs if v["new"]["lost"] and not v["old"]["lost"])
    held = sum(1 for v in pairs if v["new"]["holds"])
    print(f"{len(pairs)} positions: killed old {old} ({old / n:.0%}), new {new} ({new / n:.0%}); "
          f"saved {saved}, newly lost {worse}; a negate held in {held}")
    if saved + worse:
        from scipy.stats import binomtest
        print(f"  sign p = {binomtest(saved, saved + worse).pvalue:.3f}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*(sys.argv[1:2] or [1]))
