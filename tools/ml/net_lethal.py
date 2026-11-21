"""Does the learned net find wins the games' own solver missed? (Stage 16, before step 3b)

Step 3(a) showed the net prior finds twice the far kills at equal time on held-out
kills (prior_ab.py). Before building it into the C++ solver (step 3b, a big
build), ask the question that decides it: from the start of the lethal turn of
every NO-WIN game (our turn 3 going first, our turn 2 going second -- the games'
converged search found no win there), does PUCT over the real engine find one,
with the BC net as its prior, or with a uniform prior at ~equal wall time? Both
seats are searched (each node maximises its own player's value), so the
defender answers with its own search -- a win found is real, and verified by
replaying the principal line.

    venv/bin/python tools/ml/net_lethal.py prep [enemy]          # server: positions + their lines
    (duelpc) ~/duelroom-ml/bin/python tools/ml/net_lethal.py run results/ml/bc [sims]
    venv/bin/python tools/ml/net_lethal.py summary
"""

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "results" / "ml" / "net_lethal"


def prep(enemy="Angelechy-Meta"):
    import os

    from skills import db, engine, trainer
    os.environ.update(engine.env())
    from ygorl.engine.branch import actions_for_response
    from ygorl.engine.duel import DuelSession
    from ygorl.engine.replay import Replay
    from ygorl.solver.demo import canonical_response

    conn = db.connect()
    dh = trainer.deck_hash(engine.DECKS / "imported.ydk")
    (OUT / "lines").mkdir(parents=True, exist_ok=True)
    n = 0
    with (OUT / "positions.jsonl").open("w") as fh:
        for g in conn.execute("SELECT * FROM game_results WHERE deck_hash=? AND enemy=? AND outcome='no_win' "
                              "AND line IS NOT NULL AND method IS NOT NULL", (dh, enemy)):
            if not Path(g["line"]).exists():
                continue
            seat, turn = (0, 3) if g["first"] else (1, 2)
            rep = Replay.from_yrp(Path(g["line"]))
            s = DuelSession(rep.duel())
            acts = []
            try:
                for raw in rep.responses:
                    p = s.point
                    if p is None or (s.tracker.turn == turn and p.player == seat):
                        break         # the lethal turn's first decision of ours: the root
                    plan = actions_for_response(p.state, canonical_response(p.decision, raw))
                    if plan is None:
                        acts = None
                        break
                    for i in plan:
                        s.act(i)
                        acts.append(i)
            finally:
                s.close()
            if not acts:
                continue
            key = f"{g['first']}-{g['device']}-h{g['hand_index']}-e{g['enemy_seed']}-r{g['run']}"
            dst = OUT / "lines" / f"{key}.yrp"
            shutil.copy(g["line"], dst)
            fh.write(json.dumps({"key": key, "line": str(dst.relative_to(ROOT)), "acts": acts, "seat": seat,
                                 "damage": g["damage"]}) + "\n")
            n += 1
    print(f"{n} lethal-turn positions written to {OUT}")


def run(bc_dir, sims=800, arms="uniform6x,net", limit=None):
    import os
    if "YGORL_THIRD_PARTY" not in os.environ:     # duelpc sets it (as for bc_train.py)
        from skills import engine
        os.environ.update(engine.env())
    from skills import mcts
    from ygorl.engine.duel import DuelSession, default_cards
    from ygorl.engine.replay import Replay

    sims = int(sims)
    cards = default_cards()
    arms = arms.split(",")
    prior = None
    if "net" in arms:                          # torch: the PC's ~/duelroom-ml only
        from skills import net_prior
        prior = net_prior.Prior(str(Path(bc_dir) / "policy.pt"))
    res = OUT / "results.jsonl"
    done = {(d["key"], d["arm"]) for d in map(json.loads, res.read_text().splitlines())} if res.exists() else set()
    pos = [json.loads(l) for l in (OUT / "positions.jsonl").read_text().splitlines()][:int(limit) if limit else None]
    for d in pos:
        rep = Replay.from_yrp(ROOT / d["line"])
        make = lambda: DuelSession(rep.duel(None, cards=cards, snapshots=True))  # noqa: E731
        me = d["seat"]
        goal = lambda s: None if not s.done else (  # noqa: E731
            0.0 if s.tracker.engine_winner is None else (1.0 if s.tracker.engine_winner == 0 else -1.0))
        for arm, n_sims in [(a, sims * 6 if a == "uniform6x" else sims) for a in arms]:
            if (d["key"], arm) in done:
                continue
            t0 = time.time()
            if arm == "net":
                session, obs = net_prior.observer_through(prior, make, d["acts"], cards)
                search = mcts.Search(session, prior=prior, goal=goal, observer=obs, seed=0)
            else:
                session = make()
                for a in d["acts"]:
                    session.act(a)
                search = mcts.Search(session, goal=goal, seed=0)
            try:
                search.run(n_sims)
                for a in search.principal_line():
                    if session.point is None:
                        break
                    session.act(a)
                found = bool(session.done and session.tracker.engine_winner == me)
            finally:
                session.close()
            rec = {"key": d["key"], "arm": arm, "sims": n_sims, "found": int(found),
                   "seconds": round(time.time() - t0, 1), "damage": d["damage"]}
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
        by.setdefault(d["key"], {})[d["arm"]] = d
    pairs = [v for v in by.values() if "net" in v and "uniform6x" in v]
    n = len(pairs)
    net = sum(v["net"]["found"] for v in pairs)
    uni = sum(v["uniform6x"]["found"] for v in pairs)
    t_n = sum(v["net"]["seconds"] for v in pairs) / max(1, n)
    t_u = sum(v["uniform6x"]["seconds"] for v in pairs) / max(1, n)
    print(f"{n} no-win lethal turns (the games' converged solver found no win): "
          f"net prior found {net} ({t_n:.0f} s each), uniform x6 found {uni} ({t_u:.0f} s each); "
          f"net only {sum(1 for v in pairs if v['net']['found'] and not v['uniform6x']['found'])}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    {"prep": prep, "run": run, "summary": summary}[cmd](*sys.argv[2:])
