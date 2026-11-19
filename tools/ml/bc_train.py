"""Stage 16 step 2: a policy by imitation of our solver lines (runs on duelpc, GPU).

Reads tools/ml/demos.py's demonstrations, keeps the decisions of the TURN
PLAYER only (the other seat's answers are the scripted defender), drops
repeats (the games replay one turn 1 in every run and defence), splits by
(deck, hand) so a held-out hand is never trained on, and trains ygorl's
PolicyNet with ygorl's BC loop (`ygorl.train.bc.train_bc`).

Runs in ~/duelroom-ml on duelpc (PyTorch cu128; the server has no torch):

    YGORL_THIRD_PARTY=~/duelroom/vendor/third_party_merged \\
      ~/duelroom-ml/bin/python tools/ml/bc_train.py <demos.jsonl> <out dir> [--epochs 8]
      [--max-samples 400000] [--device cuda]

Writes <out>/policy.pt (ygorl checkpoint: `policy:PATH` plays it) and
<out>/report.json: samples by kind/seat/turn, held-out step accuracy by
kind and seat (the baseline to beat: uniform over the legal options).
"""

import argparse
import hashlib
import json
import random
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


def turns_of(demo, cards):
    """Turn number at every step of line 0 (a replay, no encoding)."""
    from ygorl.engine.duel import DuelSession

    session = DuelSession(demo.replay(0).duel(None, cards=cards))
    out = []
    try:
        for idx in demo.lines[0].actions:
            out.append(session.tracker.turn)
            session.act(idx)
    finally:
        session.close()
    return out


def samples(demo, vocab, cards, seen, skipped):
    """(obs, action, meta) of the turn player's real decisions in line 0, new ones only."""
    from ygorl.env.encoding import MAX_OPTIONS, canonical_action
    from ygorl.train.bc import line_steps, undone_steps

    ln = demo.lines[0]
    turns = turns_of(demo, cards)
    start = hashlib.sha1(json.dumps(demo.start["core_seed"]).encode()
                         + json.dumps(demo.start["decks"]).encode()).hexdigest()[:12]
    first = demo.start.get("first", 0) if isinstance(demo.start, dict) else 0
    out = []
    for player in (0, 1):
        steps = line_steps(demo, 0, vocab, cards=cards, player=player)
        undone = undone_steps(steps)
        for st in steps:
            if st.player != player:
                continue
            turn = turns[st.step]
            if (turn - 1 + first) % 2 != player:
                skipped["defender"] += 1        # the other seat's answer on this turn: the script
                continue
            if st.step in undone:
                skipped["toggle"] += 1
            elif st.obs is None or int(np.count_nonzero(st.obs["action_mask"])) < 2:
                skipped["forced"] += 1
            elif st.action >= MAX_OPTIONS:
                skipped["beyond_128"] += 1
            elif not st.obs["action_mask"][canonical_action(st.obs, st.action)]:
                skipped["undo"] += 1
            else:
                key = (start, player, tuple(ln.actions[:st.step + 1]))
                if key in seen:
                    skipped["repeat"] += 1
                    continue
                seen.add(key)
                out.append((st.obs, canonical_action(st.obs, st.action),
                            {"kind": demo.solver["kind"], "seat": player, "turn": turn,
                             "n_choices": int(np.count_nonzero(st.obs["action_mask"])),
                             "group": f"{demo.solver.get('deck_hash') or demo.deck['name']}:"
                                      f"{demo.hand_index}"}))
    return out


def stack(rows):
    from ygorl.nets.batch import OBS_KEYS
    from ygorl.train.bc import BCData

    obs = {k: np.stack([r[0][k] for r in rows]) for k in OBS_KEYS if k in rows[0][0]}
    return BCData(obs, np.asarray([r[1] for r in rows], dtype=np.int64), [r[2] for r in rows])


def accuracy_by(net, data, field):
    from ygorl.train.bc import step_accuracy

    out = {}
    for v in sorted({m[field] for m in data.meta}, key=str):
        keep = np.asarray([m[field] == v for m in data.meta])
        sub = data.subset(keep)
        out[str(v)] = {"samples": len(sub), **step_accuracy(net, sub)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("demos")
    ap.add_argument("out")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--max-samples", type=int, default=400_000)
    ap.add_argument("--heldout", type=float, default=0.2, help="share of (deck, hand) groups held out")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    import torch

    from ygorl.cards.cdb import CardVocab
    from ygorl.engine.duel import default_cards
    from ygorl.nets.agent import save_checkpoint
    from ygorl.nets.policy import PolicyNet
    from ygorl.solver.demo import Demonstration
    from ygorl.train.bc import BCConfig, network_base, train_bc

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cards = default_cards()
    vocab = CardVocab.from_db(cards)
    def load(raw):
        d = json.loads(raw)
        d.pop("key", None)          # tools/ml/demos.py's resume key, not a ygorl field
        return Demonstration.from_json(d)

    demos = [load(l) for l in Path(args.demos).read_text().splitlines()]
    t0 = time.time()
    seen, skipped, rows, failed = set(), Counter(), [], 0
    for i, d in enumerate(demos):
        try:
            rows += samples(d, vocab, cards, seen, skipped)
        except Exception as exc:  # noqa: BLE001 -- counted, the line is left out
            failed += 1
            if failed <= 5:
                print(f"line {i} ({d.solver.get('kind')}): {type(exc).__name__}: {exc}", flush=True)
        if (i + 1) % 250 == 0:
            print(f"{i + 1}/{len(demos)} lines, {len(rows)} samples ({time.time() - t0:.0f}s)", flush=True)
    rng = random.Random(args.seed)
    if len(rows) > args.max_samples:
        rows = rng.sample(rows, args.max_samples)
    print(f"{len(rows)} samples from {len(demos)} lines; skipped {dict(skipped)}; failed {failed}",
          flush=True)
    groups = sorted({r[2]["group"] for r in rows})
    if len(groups) < 2:
        raise SystemExit("fewer than 2 (deck, hand) groups: nothing to hold out")
    rng.shuffle(groups)
    held = set(groups[:max(1, int(len(groups) * args.heldout))])
    train = stack([r for r in rows if r[2]["group"] not in held])
    test = stack([r for r in rows if r[2]["group"] in held])
    report = {"lines": len(demos), "failed_lines": failed, "skipped": dict(skipped),
              "train_samples": len(train), "heldout_samples": len(test),
              "groups": {"train": len(groups) - len(held), "heldout": len(held)},
              "by": dict(Counter((m["kind"], m["seat"], m["turn"]) for m in train.meta + test.meta)
                         .most_common(20)),
              "build_seconds": round(time.time() - t0)}
    report["by"] = {f"{k[0]}/seat{k[1]}/turn{k[2]}": n for k, n in report["by"].items()}
    print(json.dumps({k: v for k, v in report.items() if k != "by"}), flush=True)

    device = torch.device(args.device if torch.cuda.is_available() or args.device == "cpu" else "cpu")
    torch.manual_seed(args.seed)
    net = PolicyNet(network_base().actor_config(len(vocab), None)).to(device)
    cfg = BCConfig(epochs=args.epochs, batch_size=256, seed=args.seed)
    t1 = time.time()
    history = train_bc(net, train, cfg, eval_sets={"heldout": test},
                       log=lambda e: print(json.dumps(e), flush=True))
    report["train_seconds"] = round(time.time() - t1)
    report["history"] = history
    net.eval()
    report["heldout_by_kind"] = accuracy_by(net, test, "kind")
    report["heldout_by_seat"] = accuracy_by(net, test, "seat")
    save_checkpoint(out / "policy.pt", net, vocab, event_length=network_base().event_length,
                    meta={"trainer": "duelroom bc_train", "report": {k: report[k] for k in
                          ("train_samples", "heldout_samples", "groups")}})
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({"heldout_by_kind": report["heldout_by_kind"],
                      "heldout_by_seat": report["heldout_by_seat"]}, indent=1), flush=True)


if __name__ == "__main__":
    main()
