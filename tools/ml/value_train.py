"""Stage 16 step 4: a value head on the BC net's trunk, trained on outcomes only.

Input: tools/ml/value_data.py's positions (the start of THEIR turn 2, labelled
by HARDNESS from the enemy's side: +1 killed at once ... -1 untouched), one
kill budget at a time (labels at different budgets never mix: --otk-ms).
Each position is replayed through the BC net's PointObserver from the duel's
start to the enemy's first decision of turn 2; the frozen trunk's `context`
there (board + history, what the actor reads) feeds ygorl's ValueHead (tanh,
the label's [-1, 1] range). No card-value feature anywhere: the board is the
input, the outcome the target (STAGE16 rule).

Held out by (deck, hand) group, as bc_train.py. Reported on the held-out
positions: Pearson / Spearman of value vs hardness, AUC for "killed", and the
within-enemy-hand Spearman (same enemy opening, different boards -- the
question the 2026-11-17 board-value attempts failed: does the net rank the
boards of ONE situation?).

    (duelpc, ~/duelroom-ml) python tools/ml/value_train.py results/ml/value/positions.jsonl \
        results/ml/bc results/ml/value_net [--otk-ms 320000] [--epochs 200]
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def encode_position(prior, prefix, cards):
    """The trunk input at the enemy's (player 1's) first decision once turn 2
    has begun, the observer having seen every point from the duel's start."""
    from ygorl.engine.branch import actions_for_response
    from ygorl.engine.duel import DuelSession
    from ygorl.engine.replay import Replay
    from ygorl.solver.demo import PASSIVE_KINDS, canonical_response

    rep = Replay.from_yrp(Path(prefix))
    session = DuelSession(rep.duel(None, cards=cards))
    obs = prior.observer(cards)
    try:
        for raw in rep.responses:
            p = session.point
            if p is None:
                return None
            for idx in actions_for_response(p.state, canonical_response(p.decision, raw)) or ():
                p = session.point
                obs.observe(p)
                obs.on_decision(p, idx)
                session.act(idx)
        for _ in range(200):
            p = session.point
            if p is None or session.done:
                return None
            if p.player == 1 and p.turn >= 2:
                obs.observe(p)
                return obs.encode(p, session.core)
            kinds = [a.kind for a in p.actions]
            idx = next((kinds.index(k) for k in PASSIVE_KINDS if k in kinds), 0)
            obs.observe(p)
            obs.on_decision(p, idx)
            session.act(idx)
        return None
    finally:
        session.close()


def spearman(xs, ys):
    from scipy.stats import spearmanr
    return float(spearmanr(xs, ys).statistic) if len(xs) > 2 else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("positions")
    ap.add_argument("bc_dir")
    ap.add_argument("out")
    ap.add_argument("--otk-ms", type=int, default=None, help="the kill budget (default: the most common)")
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--heldout", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    import numpy as np
    import torch
    from collections import Counter
    from scipy.stats import pearsonr
    from sklearn.metrics import roc_auc_score
    from ygorl.engine.duel import default_cards
    from ygorl.nets.batch import collate
    from ygorl.train.critic import ValueHead

    from skills import net_prior

    rows = [json.loads(l) for l in Path(args.positions).read_text().splitlines()]
    budgets = Counter(r["otk_ms"] for r in rows)
    otk = args.otk_ms or budgets.most_common(1)[0][0]
    rows = [r for r in rows if r["otk_ms"] == otk and Path(r["prefix"]).exists()]
    print(f"budgets {dict(budgets)}; using {otk // 1000} s: {len(rows)} positions", flush=True)
    prior = net_prior.Prior(str(Path(args.bc_dir) / "policy.pt"))
    cards = default_cards()
    t0 = time.time()
    feats, keep = [], []
    for i, r in enumerate(rows):
        try:
            o = encode_position(prior, r["prefix"], cards)
        except Exception as exc:  # noqa: BLE001 -- an unreadable position is skipped, counted
            print(f"  {r['prefix']}: {type(exc).__name__}: {exc}", flush=True)
            o = None
        if o is None:
            continue
        with torch.no_grad():
            f = prior.net.features(collate([o], prior.device), None)
        feats.append(f.context[0].float().cpu())
        keep.append(r)
        if (i + 1) % 200 == 0:
            print(f"  encoded {i + 1}/{len(rows)} ({time.time() - t0:.0f} s)", flush=True)
    X = torch.stack(feats)
    y = torch.tensor([r["hardness"] for r in keep], dtype=torch.float32)
    groups = sorted({r["group"] for r in keep})
    rng = random.Random(args.seed)
    rng.shuffle(groups)
    held = set(groups[:max(1, int(len(groups) * args.heldout))])
    te = torch.tensor([r["group"] in held for r in keep])
    tr = ~te
    print(f"{len(keep)} encoded ({len(rows) - len(keep)} skipped); train {int(tr.sum())} / held-out "
          f"{int(te.sum())} positions ({len(groups) - len(held)} / {len(held)} groups)", flush=True)

    torch.manual_seed(args.seed)
    head = ValueHead(X.shape[1], hidden=128)
    opt = torch.optim.AdamW(head.parameters(), lr=1e-3, weight_decay=1e-2)
    for ep in range(args.epochs):
        head.train()
        perm = torch.randperm(int(tr.sum()))
        Xt, yt = X[tr][perm], y[tr][perm]
        for k in range(0, len(Xt), 64):
            loss = torch.nn.functional.mse_loss(head(Xt[k:k + 64]), yt[k:k + 64])
            opt.zero_grad()
            loss.backward()
            opt.step()
    head.eval()
    with torch.no_grad():
        pred = head(X).numpy()
    yn = y.numpy()
    tem = te.numpy()
    lost = np.array([r["lost"] for r in keep])
    report = {"otk_ms": otk, "positions": len(keep), "train": int(tr.sum()), "heldout": int(tem.sum())}
    for name, m in (("train", ~tem), ("heldout", tem)):
        rep = {"pearson": float(pearsonr(pred[m], yn[m]).statistic) if m.sum() > 2 else None,
               "spearman": spearman(pred[m], yn[m])}
        if 0 < lost[m].sum() < m.sum():
            rep["auc_killed"] = float(roc_auc_score(lost[m], pred[m]))
        # within one enemy opening on one of our hands: the boards' ranking
        by = {}
        for i in np.flatnonzero(m):
            by.setdefault((keep[i]["group"], keep[i]["eseed"]), []).append(i)
        within = [spearman(pred[ix], yn[ix]) for ix in by.values()
                  if len(ix) >= 3 and len(set(yn[ix])) > 1]
        within = [w for w in within if w == w]
        rep["within_situation_spearman"] = float(np.mean(within)) if within else None
        rep["situations"] = len(within)
        report[name] = rep
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    torch.save({"head": head.state_dict(), "ctx_dim": X.shape[1], "otk_ms": otk,
                "heldout_groups": sorted(held)}, out / "value_head.pt")
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1), flush=True)


if __name__ == "__main__":
    main()
