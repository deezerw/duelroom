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
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "ml"))


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


class _Spec:
    """What a PointObserver needs of the net (Prior.observer), without the net: a
    forked worker never touches torch's CUDA state."""

    def __init__(self, prior):
        self.vocab, self.event_length = prior.vocab, prior.event_length
        self.net = type("N", (), {"cfg": prior.net.cfg})()

    def observer(self, cards=None):
        from skills.net_prior import Prior
        return Prior.observer(self, cards)


_W = {}


def _encode_one(prefix):
    try:
        return encode_position(_W["spec"], prefix, _W["cards"]), None
    except Exception as exc:  # noqa: BLE001 -- an unreadable position is skipped, counted
        return None, f"{type(exc).__name__}: {exc}"


def encode_all(spec, prefixes, cards, workers, log=print):
    """encode_position over `prefixes`, in order, on `workers` processes (fork: the
    replays are CPU work, one position at a time on one core before)."""
    import multiprocessing as mp
    _W["spec"], _W["cards"] = spec, cards
    if workers > 1:
        with mp.get_context("fork").Pool(workers) as pool:
            got = pool.map(_encode_one, prefixes, chunksize=4)
    else:
        got = [_encode_one(p) for p in prefixes]
    for p, (o, err) in zip(prefixes, got):
        if err:
            log(f"  {p}: {err}")
    return [o for o, _e in got]


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
    ap.add_argument("--features", default="context+globals+history",
                    help="trunk outputs fed to the head: context, or context+globals+history")
    ap.add_argument("--eras", default="current", choices=("current", "all"),
                    help="current: leave out positions from before the 2026-11-20 fixes")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                    help="processes replaying the positions (same result for any count)")
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
    if args.eras != "all":
        # bc_train's era filter, BEFORE the budget is chosen (the old eras' 1x kill checks
        # were the most common budget, and nothing was left after it): no position from
        # before the 2026-11-20 fixes (board_data's 2026-11-19 boards are one of those eras)
        from bc_train import SKIP_ERAS
        n = len(rows)
        rows = [r for r in rows if r.get("device") not in SKIP_ERAS
                and not (r["source"] == "board_data" and "board_kill" in SKIP_ERAS)]
        print(f"  {len(rows)} of {n} positions after the pre-2026-11-20 eras", flush=True)
    budgets = Counter(r["otk_ms"] for r in rows)
    otk = args.otk_ms or budgets.most_common(1)[0][0]
    rows = [r for r in rows if r["otk_ms"] == otk and Path(r["prefix"]).exists()]
    print(f"budgets {dict(budgets)}; using {otk // 1000} s: {len(rows)} positions", flush=True)
    # on the CPU first: the encoding workers are forked before CUDA is touched
    prior = net_prior.Prior(str(Path(args.bc_dir) / "policy.pt"), device="cpu")
    cards = default_cards()
    t0 = time.time()
    encoded = encode_all(_Spec(prior), [r["prefix"] for r in rows], cards, args.workers,
                         log=lambda m: print(m, flush=True))
    print(f"  encoded {len(rows)} positions on {args.workers} worker(s) ({time.time() - t0:.0f} s)",
          flush=True)
    prior.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    prior.net.to(prior.device)
    feats, keep = [], []
    for i, (r, o) in enumerate(zip(rows, encoded)):
        if o is None:
            continue
        with torch.no_grad():
            f = prior.net.features(collate([o], prior.device), None)
        feats.append(torch.cat([getattr(f, k)[0].float().cpu()
                                for k in args.features.split("+")]))
        keep.append(r)
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
    report = {"otk_ms": otk, "features": args.features, "positions": len(keep),
              "train": int(tr.sum()), "heldout": int(tem.sum())}
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
                "features": args.features, "heldout_groups": sorted(held)}, out / "value_head.pt")
    torch.save({"X": X, "y": y, "heldout": te, "lost": torch.tensor(lost)}, out / "features.pt")
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1), flush=True)


if __name__ == "__main__":
    main()
