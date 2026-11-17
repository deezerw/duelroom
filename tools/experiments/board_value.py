"""Would a learned board value beat the solver's "target cards placed"?
(Part 2 of the duelpc plan, 2026-11-17.)

Dataset: every stored going-first turn-1 board (its prefix replay) with the
real win rate of its 10 games. The solver's real choice is between boards of
ONE hand, so the test that counts is within-hand: Spearman(score, win rate
minus the enemy hand's mean) per hand, averaged, bootstrap 95% interval.
The learned model (ridge on field-card presence + counts) is scored on enemy
hands it never saw.

Measured on imported vs Angelechy-Meta (240 boards, 2400 games, server):
  target cards placed (solver today)   +0.14  [+0.03, +0.25]
  interruption pieces                  +0.18  [+0.05, +0.29]
  monsters on field                    +0.09  [-0.03, +0.22]
  learned board value                  +0.07  [-0.04, +0.18]
No evidence a learned value would choose better boards; every signal is
weak. Across DIFFERENT hands "share of target placed" ranks best (+0.40),
but that is the hand's quality, not a choice the solver makes. The data is
thin for this question: one board per (hand, enemy hand), each shaped by
that enemy hand's interruptions. A real test needs many boards per hand
against a fixed enemy hand (duelpc-sized: ~7 h for 480 boards x 10 hands).

    venv/bin/python tools/experiments/board_value.py   (rebuilds data first)
"""
import json
import sys
from pathlib import Path
from collections import Counter
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from skills import db, game, trainer
c = db.connect()
goals = {r["hand_index"]: [int(str(t).split("@")[0]) for t in trainer.split_goal(json.loads(r["best_goal"]))[0]]
         for r in c.execute("SELECT hand_index, best_goal FROM trainer_hands WHERE best_goal IS NOT NULL "
                            "AND deck_hash=(SELECT deck_hash FROM game_turn1 LIMIT 1)")}
out = []
for t in c.execute("SELECT * FROM game_turn1 WHERE enemy='Angelechy-Meta' AND first=1 AND device='server' AND ok=1"):
    res = c.execute("SELECT outcome FROM game_results WHERE deck_hash=? AND enemy=? AND hand_index=? AND enemy_seed=? "
                    "AND first=1 AND device='server'", (t["deck_hash"], t["enemy"], t["hand_index"], t["enemy_seed"])).fetchall()
    b = game.replay(t["prefix"]).board
    me = b["players"][0]
    field = [m["code"] for m in me["mzone"]] + [s["code"] for s in me["szone"]]
    have = Counter(field)
    want = Counter(goals.get(t["hand_index"], []))
    overlap = sum(min(have[k], v) for k, v in want.items())
    out.append({"hand": t["hand_index"], "seed": t["enemy_seed"], "reached": t["reached"],
                "pieces": len(json.loads(t["pieces"] or "[]")), "overlap": overlap, "target_n": sum(want.values()),
                "mz": [m["code"] for m in me["mzone"]], "sz": [s["code"] for s in me["szone"]],
                "hand_left": me["hand"], "gy": len(me["grave"]), "ban": len(me["banished"]),
                "enemy_hand": json.loads(t["enemy_hand"] or "[]"),
                "n": len(res), "wins": sum(r[0] == "win" for r in res), "lost_t2": sum(r[0] == "lost_t2" for r in res)})

import numpy as np
from collections import defaultdict
from scipy.stats import spearmanr
from sklearn.linear_model import Ridge
D = out
y = np.array([d["wins"] / d["n"] for d in D])
seeds = sorted({d["seed"] for d in D}); hands = sorted({d["hand"] for d in D})
codes = sorted({c for d in D for c in d["mz"] + d["sz"]})
def feats(d):
    f = [len(d["mz"]), len(d["sz"]), d["pieces"], len(d["hand_left"]), d["gy"] / 10]
    s = set(d["mz"] + d["sz"])
    return f + [1.0 if c in s else 0.0 for c in codes] + [1.0 if d["seed"] == s_ else 0.0 for s_ in seeds]
X = np.array([feats(d) for d in D])
def within_seed(score):
    rs = []
    for s in seeds:
        idx = [i for i, d in enumerate(D) if d["seed"] == s]
        if np.std(score[idx]) == 0 or np.std(y[idx]) == 0:
            continue
        rs.append(spearmanr(score[idx], y[idx]).correlation)
    return np.mean(rs), len(rs), rs
def boot(score, B=2000, rng=np.random.default_rng(0)):
    _, _, rs = within_seed(score); rs = np.array(rs)
    m = [rng.choice(rs, len(rs)).mean() for _ in range(B)]
    return np.percentile(m, [2.5, 97.5])
base = {
    "solver today: target cards placed": np.array([d["overlap"] for d in D], float),
    "share of target placed": np.array([d["overlap"] / max(1, d["target_n"]) for d in D]),
    "goal reached (0/1)": np.array([d["reached"] for d in D], float),
    "interruption pieces": np.array([d["pieces"] for d in D], float),
    "monsters on field": np.array([len(d["mz"]) for d in D], float),
}
# out-of-fold model: leave one HAND out (the model never saw that hand's boards)
pred = np.zeros(len(D))
for h in hands:
    tr = [i for i, d in enumerate(D) if d["hand"] != h]; te = [i for i, d in enumerate(D) if d["hand"] == h]
    m = Ridge(alpha=3.0).fit(X[tr], y[tr], sample_weight=[D[i]["n"] for i in tr])
    pred[te] = m.predict(X[te])
base["learned board value (held-out hands)"] = pred
print(f"{len(D)} positions, mean win rate {y.mean():.2f}; within-enemy-hand Spearman vs real win rate:")
for k, s in base.items():
    m, n, _ = within_seed(s); lo, hi = boot(s)
    print(f"  {k:40} {m:+.2f}  [{lo:+.2f}, {hi:+.2f}]  ({n} enemy hands)")
m = Ridge(alpha=3.0).fit(X, y, sample_weight=[d["n"] for d in D])
w = sorted(zip(m.coef_[5:5 + len(codes)], codes))
print("most negative cards:", [(c, round(x, 2)) for x, c in w[:6]])
print("most positive cards:", [(c, round(x, 2)) for x, c in w[-6:]])
print("counts (mz, sz, pieces, hand, gy/10):", [round(x, 3) for x in m.coef_[:5]])

print("\n--- the solver's real choice: boards of ONE hand, enemy-hand difficulty removed ---")
seed_mean = {s: np.mean([y[i] for i, d in enumerate(D) if d["seed"] == s]) for s in seeds}
yr = np.array([y[i] - seed_mean[d["seed"]] for i, d in enumerate(D)])
def within_hand(score):
    rs = []
    for h in hands:
        idx = [i for i, d in enumerate(D) if d["hand"] == h]
        if np.std(score[idx]) == 0 or np.std(yr[idx]) == 0:
            continue
        rs.append(spearmanr(score[idx], yr[idx]).correlation)
    return np.array(rs)
# learned value WITHOUT seed terms and out-of-fold by enemy hand: it must score a board
# against an enemy hand it never saw, with that hand's difficulty removed from the label
Xb = X[:, :5 + len(codes)]
predb = np.zeros(len(D))
for s in seeds:
    tr = [i for i, d in enumerate(D) if d["seed"] != s]; te = [i for i, d in enumerate(D) if d["seed"] == s]
    m = Ridge(alpha=3.0).fit(Xb[tr], yr[tr], sample_weight=[D[i]["n"] for i in tr])
    predb[te] = m.predict(Xb[te])
for k, s in [("solver today: target cards placed", base["solver today: target cards placed"]),
             ("interruption pieces", base["interruption pieces"]),
             ("monsters on field", base["monsters on field"]),
             ("learned board value (held-out enemy hands)", predb)]:
    rs = within_hand(s); rng = np.random.default_rng(1)
    bm = [rng.choice(rs, len(rs)).mean() for _ in range(2000)]
    print(f"  {k:44} {rs.mean():+.2f}  [{np.percentile(bm, 2.5):+.2f}, {np.percentile(bm, 97.5):+.2f}]  ({len(rs)} hands)")
