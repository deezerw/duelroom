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
import os
import json
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


# Eras whose lines teach misplays fixed since (user, 2026-11-26: "perfect so we can start gpu
# training"): everything before 2026-11-20's fixes -- hand traps spent on their own turn (patch
# 0009), gifts handed over -- i.e. the relabelled devices kill1x / endgame-t1 / brk1x / gift and
# board_data's 2026-11-19 kill lines. The demos file keeps them; --eras all trains on them.
SKIP_ERAS = ("kill1x", "endgame-t1", "brk1x", "gift", "board_kill")


def seat_groups(solver, deck_name, hand_index):
    """{seat: held-out group} of a demonstration's decisions. OUR seat's decisions are
    grouped by our (deck, hand); the ENEMY seat's by its hand (the enemy seed). Grouping
    every decision by our hand (before 2026-11-26) left the 10 enemy hands in both halves:
    the held-out enemy-kill accuracy was measured on enemy hands the net trained on.
    Ours is seat 1 going second ("break"), seat 0 otherwise (kill, board_kill)."""
    ours = 1 if solver.get("kind") == "break" else 0
    e = solver.get("eseed")
    if e is None:                          # board_data: "h3-e1004-full-r0"
        m = re.search(r"-e(\d+)-", solver.get("tag") or "")
        e = m.group(1) if m else "?"
    return {ours: f"{solver.get('deck_hash') or deck_name}:{hand_index}", 1 - ours: f"enemy:{e}"}


def demo_groups(raw):
    """Every held-out group a parsed demos.jsonl record can put samples in."""
    return set(seat_groups(raw.get("solver") or {}, (raw.get("deck") or {}).get("name"),
                           raw.get("hand_index")).values())


# --eras method: only games of the LIVE method -- the devices games are played under
# (server, duelpc); every relabelled era (m21b, m21c, m25c ...) and board_data left out. Each
# bump changes how the defence answers (2026-11-26a: every card), so a kill line or a label
# from an older era is a search against a weaker or different defender.
LIVE_DEVICES = ("server", "duelpc")


def keep_demo(raw, skip=SKIP_ERAS, live_only=False):
    """Train on this demonstration (a parsed demos.jsonl record)? Its era is the game's
    device label; board_data's kill lines carry kind board_kill."""
    solver = raw.get("solver") or {}
    if live_only:
        return solver.get("device") in LIVE_DEVICES and solver.get("kind") != "board_kill"
    return solver.get("device") not in skip and solver.get("kind") not in skip


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


def both_steps(demo, vocab, cards, first):
    """ygorl's `line_steps` for BOTH seats in ONE replay ({player: steps}, turn per
    step): a PointObserver per seat, fed every point as line_steps feeds its one. A
    seat's observation is encoded only where the sample builder can keep it -- its
    own non-forced decision on its own turn (the defender's answers are dropped) --
    so every kept step's observation is the one line_steps gives. Was: one replay for
    the turns plus one encoding replay per seat (80% of the build in two)."""
    from ygorl.engine.duel import DuelSession
    from ygorl.env.events import DEFAULT_EVENT_LENGTH
    from ygorl.env.observer import PointObserver
    from ygorl.train.bc import DemoError, DemoStep, action_key

    ln = demo.lines[0]
    session = DuelSession(demo.replay(0).duel(None, cards=cards))
    obs = {p: PointObserver(cards, vocab, DEFAULT_EVENT_LENGTH) for p in (0, 1)}
    out, turns = {0: [], 1: []}, []
    try:
        for step, idx in enumerate(ln.actions):
            point = session.point
            if point is None:
                raise DemoError(f"step {step}: the duel stopped before the line ended")
            turn = session.tracker.turn
            turns.append(turn)
            act = point.actions[idx]
            card = (act.card.code, act.card.loc) if act.card is not None else None
            key = action_key(point, idx)
            for p in (0, 1):
                obs[p].observe(point)
                o = None
                if (point.player == p and len(point.actions) > 1
                        and (turn - 1 + first) % 2 == p):
                    o = obs[p].encode(point, session.core)
                out[p].append(DemoStep(step, point.player, idx, len(point.actions), act.kind,
                                       card, o, key))
            session.act(idx)
            for p in (0, 1):
                obs[p].on_decision(point, idx)
    finally:
        session.close()
    return out, turns


def samples(demo, vocab, cards, seen, skipped):
    """(obs, action, meta) of the turn player's real decisions in line 0, new ones only."""
    out = []
    for key, row in candidates(demo, vocab, cards, skipped):
        if key in seen:
            skipped["repeat"] += 1
            continue
        seen.add(key)
        out.append(row)
    return out


def candidates(demo, vocab, cards, skipped):
    """[(key, (obs, action, meta))] of line 0's turn-player decisions; `samples`
    drops the keys already seen (repeats are only known across lines)."""
    from ygorl.env.encoding import MAX_OPTIONS, canonical_action
    from ygorl.train.bc import undone_steps

    ln = demo.lines[0]
    start = hashlib.sha1(json.dumps(demo.start["core_seed"]).encode()
                         + json.dumps(demo.start["decks"]).encode()).hexdigest()[:12]
    first = demo.start.get("first", 0) if isinstance(demo.start, dict) else 0
    by_player, turns = both_steps(demo, vocab, cards, first)
    groups = seat_groups(demo.solver, demo.deck["name"], demo.hand_index)
    out = []
    for player in (0, 1):
        steps = by_player[player]
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
                out.append((key, (st.obs, canonical_action(st.obs, st.action),
                            {"kind": demo.solver["kind"], "seat": player, "turn": turn,
                             "n_choices": int(np.count_nonzero(st.obs["action_mask"])),
                             "group": groups[player]})))
    return out


_W = {}


def _cards_vocab():
    from ygorl.cards.cdb import CardVocab
    from ygorl.engine.duel import default_cards
    cards = default_cards()
    return cards, CardVocab.from_db(cards)


def _init(load_cards):
    _W["cards"], _W["vocab"] = load_cards()


def _one(raw):
    """A worker's lines: (candidates, skipped, error) per line, in order."""
    skipped = Counter()
    try:
        return candidates(_W["load"](raw), _W["vocab"], _W["cards"], skipped), skipped, None
    except Exception as exc:  # noqa: BLE001 -- counted, the line is left out
        return [], skipped, f"{type(exc).__name__}: {exc}"


def build(raws, load, load_cards, workers, log=print):
    """(rows, skipped, failed) over the demonstration lines `raws`, IN ORDER, so the
    first copy of a repeated decision is kept exactly as one process kept it. Was 22
    min on one core for 204k samples (2026-11-20); the lines are independent, only
    the repeat check spans them, and it runs here."""
    import multiprocessing as mp
    import time as _t
    seen, skipped, rows, failed, t0 = set(), Counter(), [], 0, _t.time()
    _W["load"] = load
    if workers > 1:
        # fork: the workers inherit `load`; nothing has touched CUDA yet
        pool = mp.get_context("fork").Pool(workers, _init, (load_cards,))
        it = pool.imap(_one, raws, chunksize=8)
    else:
        _init(load_cards)
        pool, it = None, map(_one, raws)
    try:
        for i, (cands, sk, err) in enumerate(it):
            skipped.update(sk)
            if err is not None:
                failed += 1
                if failed <= 5:
                    log(f"line {i}: {err}")
            for key, row in cands:
                if key in seen:
                    skipped["repeat"] += 1
                    continue
                seen.add(key)
                rows.append(row)
            if (i + 1) % 250 == 0:
                log(f"{i + 1}/{len(raws)} lines, {len(rows)} samples ({_t.time() - t0:.0f}s)")
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    return rows, skipped, failed


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
    ap.add_argument("--eras", default="current", choices=("current", "method", "all"),
                    help="current: leave out lines from before the 2026-11-20 fixes (SKIP_ERAS); "
                         "method: only the live method's games (LIVE_DEVICES)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1,
                    help="processes building samples (the result is the same for any count)")
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

    skip = () if args.eras == "all" else SKIP_ERAS
    every = Path(args.demos).read_text().splitlines()
    demos = [l for l in every if keep_demo(json.loads(l), skip, live_only=args.eras == "method")]
    if args.eras == "method":
        skip = ("every era but " + "/".join(LIVE_DEVICES),)
    print(f"{len(demos)} of {len(every)} lines kept (skipped eras: {', '.join(skip) or 'none'})", flush=True)
    t0 = time.time()
    rows, skipped, failed = build(demos, load, _cards_vocab, args.workers,
                                  log=lambda m: print(m, flush=True))
    rng = random.Random(args.seed)
    if len(rows) > args.max_samples:
        rows = rng.sample(rows, args.max_samples)
    print(f"{len(rows)} samples from {len(demos)} lines; skipped {dict(skipped)}; failed {failed}",
          flush=True)
    # every group the kept LINES can hold (not only those with samples), so prior_ab can
    # rebuild exactly this split from the demos file
    groups = sorted(set().union(*(demo_groups(json.loads(l)) for l in demos)))
    if len(groups) < 2:
        raise SystemExit("fewer than 2 (deck, hand) groups: nothing to hold out")
    rng.shuffle(groups)
    held = set(groups[:max(1, int(len(groups) * args.heldout))])
    train = stack([r for r in rows if r[2]["group"] not in held])
    test = stack([r for r in rows if r[2]["group"] in held])
    report = {"lines": len(demos), "eras": args.eras, "skip_eras": list(skip), "failed_lines": failed, "skipped": dict(skipped),
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
