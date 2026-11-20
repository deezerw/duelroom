"""Stage 16 step 3(a): does the BC net, as the search's prior, find kills a
uniform prior does not -- on hands it never trained on?

Positions: the kill lines among tools/ml/demos.py's demonstrations whose
(deck, hand) group bc_train.py HELD OUT (rebuilt exactly: sorted groups,
random.Random(seed).shuffle, the first `heldout` share -- asserted against
report.json's counts). From k decisions before each line's end, skills/mcts.py
runs `sims` simulations twice -- uniform prior, then the net's -- and plays its
principal line; found = the line's searcher wins. Both seats are in the tree
(the defender is searched, not scripted), the same for both arms.

    (duelpc, ~/duelroom-ml) python tools/ml/prior_ab.py results/ml/demos/demos.jsonl \
        results/ml/bc [--positions 40] [--depths 8,16,30] [--sims 800]
    python tools/ml/prior_ab.py summary
Resumable: results/ml/prior_ab/results.jsonl, one line per (line, depth, arm).
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "results" / "ml" / "prior_ab"


def group_of(raw):
    """bc_train.py's group key of a demonstration (its deck hash or name, and hand)."""
    solver, deck = raw.get("solver") or {}, raw.get("deck") or {}
    return f"{solver.get('deck_hash') or deck.get('name')}:{raw.get('hand_index')}"


def heldout_groups(raws, report, seed=0, share=0.2):
    groups = sorted({group_of(r) for r in raws})
    want = report["groups"]["train"] + report["groups"]["heldout"]
    if len(groups) != want:
        raise SystemExit(f"{len(groups)} groups in the demos, bc_train saw {want}: "
                         f"not the split the net was trained on")
    rng = random.Random(seed)
    rng.shuffle(groups)
    held = set(groups[:max(1, int(len(groups) * share))])
    assert len(held) == report["groups"]["heldout"]
    return held


def winner_value(session):
    """mcts value for player 0 once the duel is over, else None."""
    if not session.done:
        return None
    w = session.tracker.engine_winner
    return 0.0 if w is None else (1.0 if w == 0 else -1.0)


def run_one(raw, depth, arm, sims, prior):
    import os

    from skills import mcts, net_prior
    # the merged card scripts: set by the caller on duelpc (as for bc_train.py),
    # from skills.engine here
    if "YGORL_THIRD_PARTY" not in os.environ:
        from skills import engine
        os.environ.update(engine.env())
    from ygorl.engine.duel import DuelSession, default_cards
    from ygorl.solver.demo import Demonstration

    raw = dict(raw)
    raw.pop("key", None)
    d = Demonstration.from_json(raw)
    acts = d.lines[0].actions
    if len(acts) <= depth:
        return None
    cards = default_cards()
    make = lambda: DuelSession(d.replay(0).duel(None, cards=cards, snapshots=True))  # noqa: E731
    t0 = time.time()
    calls0 = getattr(prior, "calls", 0)
    if arm == "net":
        session, obs = net_prior.observer_through(prior, make, acts[:-depth], cards)
        search = mcts.Search(session, prior=prior, goal=winner_value, observer=obs, seed=0)
    else:
        session = make()
        for a in acts[:-depth]:
            session.act(a)
        search = mcts.Search(session, goal=winner_value, seed=0)
    try:
        # who made the kill: the stored line's winner (its last decision is often
        # the DEFENDER's pass during the final attack)
        probe = make()
        try:
            for a in acts:
                probe.act(a)
            searcher = probe.tracker.engine_winner
        finally:
            probe.close()
        if searcher is None:
            return None
        search.run(sims)
        for a in search.principal_line():
            if session.point is None:
                break
            session.act(a)
        found = bool(session.done and session.tracker.engine_winner == searcher)
        return {"found": int(found), "seconds": round(time.time() - t0, 2),
                "steps": search.steps, "net_calls": getattr(prior, "calls", 0) - calls0 if arm == "net" else 0}
    finally:
        session.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("demos")
    ap.add_argument("bc_dir")
    ap.add_argument("--positions", type=int, default=40)
    ap.add_argument("--depths", default="8,16,30")
    ap.add_argument("--sims", type=int, default=800)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    from skills import net_prior

    raws = [json.loads(l) for l in Path(args.demos).read_text().splitlines()]
    report = json.loads((Path(args.bc_dir) / "report.json").read_text())
    held = heldout_groups(raws, report)
    kills = [r for r in raws if group_of(r) in held and (r.get("solver") or {}).get("kind") == "kill"]
    kills = random.Random(args.seed).sample(kills, min(args.positions, len(kills)))
    print(f"{len(held)} held-out groups, {len(kills)} kill lines from them", flush=True)
    prior = net_prior.Prior(str(Path(args.bc_dir) / "policy.pt"))
    OUT.mkdir(parents=True, exist_ok=True)
    res = OUT / "results.jsonl"
    done = ({(d["line"], d["depth"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())}
            if res.exists() else set())
    for i, raw in enumerate(kills):
        line = raw.get("key") or f"{group_of(raw)}#{i}"
        for depth in map(int, args.depths.split(",")):
            for arm in ("uniform", "net"):
                if (line, depth, arm) in done:
                    continue
                try:
                    r = run_one(raw, depth, arm, args.sims, prior)
                except Exception as exc:  # noqa: BLE001 -- one position never ends the run
                    print(f"{line} d{depth} {arm}: failed: {type(exc).__name__}: {exc}", flush=True)
                    continue
                if r is None:
                    continue
                rec = {"line": line, "depth": depth, "arm": arm, "sims": args.sims, **r}
                with res.open("a") as fh:
                    fh.write(json.dumps(rec) + "\n")
                print(json.dumps(rec), flush=True)
    summary()


def summary():
    f = OUT / "results.jsonl"
    if not f.exists():
        print("no results yet")
        return
    recs = [json.loads(l) for l in f.read_text().splitlines()]
    by = {}
    for d in recs:
        by.setdefault((d["line"], d["depth"]), {})[d["arm"]] = d
    for depth in sorted({d["depth"] for d in recs}):
        pairs = [v for (_l, dp), v in by.items() if dp == depth and "uniform" in v and "net" in v]
        if not pairs:
            continue
        n = len(pairs)
        u = sum(v["uniform"]["found"] for v in pairs)
        k = sum(v["net"]["found"] for v in pairs)
        only_net = sum(1 for v in pairs if v["net"]["found"] and not v["uniform"]["found"])
        only_uni = sum(1 for v in pairs if v["uniform"]["found"] and not v["net"]["found"])
        tu = sum(v["uniform"]["seconds"] for v in pairs) / n
        tn = sum(v["net"]["seconds"] for v in pairs) / n
        print(f"  {depth:3} decisions out, {pairs[0]['net']['sims']} simulations, {n} held-out kills: "
              f"uniform found {u} ({tu:.1f} s), net found {k} ({tn:.1f} s); "
              f"net only {only_net}, uniform only {only_uni}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main()
