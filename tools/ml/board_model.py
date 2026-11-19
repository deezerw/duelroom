"""Which of a position's turn-1 boards survives their turn 2? (Stage 11 step 3)

Offline only: reads tools/ml/board_data.py's boards and scores ways of
PICKING one board per position, on hands the model never saw (folds split
by hand: one hand's positions are near-copies of each other). A pick is
judged by the real kill-check label of the board it picks:

  full      the trained goal's board (what the games play today)
  pieces    the board with the most interruption pieces
  random    any board (expected value)
  model     gradient-boosted trees on the board's cards by zone
  oracle    a surviving board whenever one exists (the ceiling)

Only positions where the board matters (some boards lost, some lived)
can tell the picks apart; all positions are reported too. A model is worth
a paired A/B (step 4) only if it beats `full` here with an interval clear
of zero. The board's numbers stay the kill check's -- the model only picks.

    venv/bin/python tools/ml/board_model.py
"""

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BOARDS = ROOT / "results" / "ml" / "boards" / "boards.jsonl"
ZONES = ("mzone", "szone", "hand", "grave", "banished", "extra_faceup")
FOLDS = 6


RECHECKS = BOARDS.with_name("rechecks.jsonl")


def load():
    """One row per distinct board per position: (pos, sig) -> record.

    A board re-checked at 16x (`board_data.py recheck`) takes that label:
    a survival at 4x may only have been the search's clock. Its 4x label
    stays as `lost4`."""
    rows = {}
    for d in map(json.loads, BOARDS.read_text().splitlines()):
        if not d.get("ok") or "lost" not in d:
            continue
        k = (d["hand"], d["eseed"], d["sig"])
        r = rows.setdefault(k, {**d, "variants": []})
        r["variants"].append(d["variant"])
    if RECHECKS.exists():
        for c in map(json.loads, RECHECKS.read_text().splitlines()):
            r = rows.get((c["hand"], c["eseed"], c["sig"]))
            if r is not None:
                r["lost4"], r["lost"] = r["lost"], c["lost16"]
    return list(rows.values())


def _code(x):
    # board_summary entries are codes, or dicts carrying one
    if isinstance(x, dict):
        return int(x.get("code") or 0)
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def features(rows):
    """Bag of card codes per zone (our side) + zone counts + pieces."""
    vocab = {}
    for r in rows:
        me = r["board"]["players"][0]
        for z in ZONES:
            for x in me.get(z) or []:
                if _code(x):
                    vocab.setdefault((z, _code(x)), len(vocab))
    base = len(vocab)
    names = [f"{z}:{c}" for (z, c), _ in sorted(vocab.items(), key=lambda kv: kv[1])] \
        + [f"n_{z}" for z in ZONES] + ["pieces", "reached"]
    X = []
    for r in rows:
        me = r["board"]["players"][0]
        v = [0.0] * (base + len(ZONES) + 2)
        for i, z in enumerate(ZONES):
            items = [x for x in me.get(z) or [] if _code(x)]
            for x in items:
                v[vocab[(z, _code(x))]] += 1
            v[base + i] = len(items)
        v[base + len(ZONES)] = len(r["pieces"])
        v[base + len(ZONES) + 1] = r["reached"]
        X.append(v)
    return X, names


def picks(rows, scores):
    """Per position: the lost label of each method's pick."""
    by = defaultdict(list)
    for r, s in zip(rows, scores):
        by[(r["hand"], r["eseed"])].append((r, s))
    out = {}
    for pos, bs in by.items():
        # The games play the full goal ONCE: its expected label over the
        # full-goal turn 1s (a board made twice counts twice), not the
        # better of its seeds.
        def made_by(name):
            xs = [r["lost"] for r, _ in bs for v in r["variants"] if v == name]
            return sum(xs) / len(xs) if xs else None
        out[pos] = {
            "full": made_by("full"),
            "free": made_by("free"),       # the goal + the hand's free cards (Stage 15)
            "pieces": max(bs, key=lambda b: (len(b[0]["pieces"]), -b[1]))[0]["lost"],
            "random": sum(r["lost"] for r, _ in bs) / len(bs),
            "model": min(bs, key=lambda b: b[1])[0]["lost"],     # lowest P(lost)
            "oracle": min(r["lost"] for r, _ in bs),
            "mixed": 0 < sum(r["lost"] for r, _ in bs) < len(bs),
            "n": len(bs),
        }
    return out


def boot_diff(res, a, b, reps=2000, seed=0):
    """95% interval of mean(a) - mean(b) over positions, resampling hands."""
    hands = sorted({p[0] for p in res})
    by = defaultdict(list)
    for p, v in res.items():
        if v[a] is not None and v[b] is not None:
            by[p[0]].append(v[a] - v[b])
    rng = random.Random(seed)
    ds = []
    for _ in range(reps):
        xs = [x for h in (rng.choice(hands) for _ in hands) for x in by[h]]
        if xs:
            ds.append(sum(xs) / len(xs))
    ds.sort()
    allx = [x for v in by.values() for x in v]
    return sum(allx) / max(1, len(allx)), ds[int(.025 * len(ds))], ds[int(.975 * len(ds))]


def main():
    from sklearn.ensemble import HistGradientBoostingClassifier

    rows = load()
    X, _names = features(rows)
    y = [r["lost"] for r in rows]
    hands = sorted({r["hand"] for r in rows})
    random.Random(0).shuffle(hands)
    fold = {h: i % FOLDS for i, h in enumerate(hands)}
    scores = [0.0] * len(rows)
    for f in range(FOLDS):
        tr = [i for i, r in enumerate(rows) if fold[r["hand"]] != f]
        te = [i for i, r in enumerate(rows) if fold[r["hand"]] == f]
        if not te or len({y[i] for i in tr}) < 2:
            continue
        m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05,
                                           max_leaf_nodes=15, l2_regularization=1.0)
        m.fit([X[i] for i in tr], [y[i] for i in tr])
        for i, p in zip(te, m.predict_proba([X[i] for i in te])[:, 1]):
            scores[i] = p
    res = picks(rows, scores)
    re = [r for r in rows if "lost4" in r]
    if re:
        print(f"{len(re)} survivor(s) re-checked at 16x: "
              f"{sum(r['lost'] for r in re)} were killed after all (labels corrected)")
    print(f"{sum(1 for r in rows if r.get('kill'))} board(s) carry the rich kill label")
    print(f"{len(rows)} distinct boards, {len(res)} positions, "
          f"{sum(v['mixed'] for v in res.values())} where the board decides; "
          f"held-out by hand, {FOLDS} folds")
    for subset, keep in (("all positions", lambda v: True),
                         ("where the board decides", lambda v: v["mixed"])):
        sub = {p: v for p, v in res.items() if keep(v)}
        if not sub:
            continue
        print(f"\n{subset} ({len(sub)}): lost on turn 2 with the board picked by")
        for m in ("full", "free", "pieces", "random", "model", "oracle"):
            vals = [v[m] for v in sub.values() if v[m] is not None]
            if vals:
                print(f"  {m:7} {sum(vals) / len(vals):.0%}  ({len(vals)} positions)")
        for m in ("pieces", "free", "model"):
            d, lo, hi = boot_diff(sub, m, "full")
            print(f"  {m} - full: {d:+.1%} (95% {lo:+.1%} to {hi:+.1%}); negative = fewer losses")


if __name__ == "__main__":
    main()
