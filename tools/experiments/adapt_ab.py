"""A/B: does --adapt <corpus of this deck's solved lines> make held-out hands solve faster?"""
import json, sys, tempfile, itertools
from pathlib import Path
from multiprocessing import Pool
sys.path.insert(0, "/home/deezer/duelroom")
from skills import engine

SRC = Path("/home/deezer/duelroom/results/sweep_active.jsonl")
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
BUDGET_MS = int(sys.argv[2]) if len(sys.argv) > 2 else 10_000
recs = [json.loads(l) for l in SRC.read_text().splitlines() if l.strip()]
train = [r for r in recs if r["hand_index"] < 32 and r.get("lines")]
test = [r for r in recs if r["hand_index"] >= 32]

def build_corpus():
    from ygorl.engine.replay import Replay
    cdir = OUT / "corpus"; cdir.mkdir(exist_ok=True)
    n = 0
    for r in train:
        st = r["start"]
        for i, ln in enumerate(r["lines"]):
            rep = Replay(seed=0, first=0, rule_flags=st["rule_flags"], player=st["player"], shuffle_decks=False,
                         decks=st["decks"], responses=[bytes.fromhex(x) for x in ln["responses"]],
                         seed_words=st.get("core_seed"))
            rep.to_yrpx(cdir / f"h{r['hand_index']:02d}_l{i}.yrpX", names=("deck", "goldfish")); n += 1
    return cdir, n

def deck_file():
    d = recs[0]["deck"]; p = OUT / f"{d['name']}.ydk"
    p.write_text("#main\n" + "\n".join(map(str, d["main"])) + "\n#extra\n" + "\n".join(map(str, d["extra"])) + "\n!side\n")
    return p

def run(args):
    r, arm, seed, corpus, deck = args
    from ygorl.solver import HandJob, Workdir, find_solver
    from ygorl.solver import batch as BT
    scratch = Path(tempfile.mkdtemp(prefix=f"adx-{arm}-"))
    job = HandJob(deck_path=deck, hand_index=r["hand_index"], hand_seed=r["hand_seed"], targets=tuple(r["targets"]),
                  workdir=Workdir.create(scratch / "wd").path, scratch=scratch / "job", solve_ms=BUDGET_MS, threads=1,
                  hand=tuple(r["hand"]), binary=find_solver(), solver_seed=seed, timeout_s=BUDGET_MS / 1000 + 120)
    if arm == "adapt":
        orig = BT.SolveRequest
        BT.SolveRequest = lambda **kw: orig(**{**kw, "extra_args": tuple(kw.get("extra_args") or ()) + ("--adapt", str(corpus))})
    d = BT.solve_hand(job).to_json()
    return {"hand": r["hand_index"], "arm": arm, "seed": seed, "status": d["status"],
            "elapsed": (d.get("solver") or {}).get("elapsed_s"), "error": (d.get("error") or "")[:200]}

if __name__ == "__main__":
    corpus, n = build_corpus(); deck = deck_file()
    print(f"corpus: {n} lines from {len(train)} hands; test hands {[r['hand_index'] for r in test]}; budget {BUDGET_MS}ms", flush=True)
    jobs = [(r, arm, seed, corpus, deck) for seed in (1, 2) for r in test for arm in ("base", "adapt")]
    res = []
    with Pool(8) as p:
        for x in p.imap_unordered(run, jobs):
            res.append(x); print(json.dumps(x), flush=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=1))
    for arm in ("base", "adapt"):
        rs = [x for x in res if x["arm"] == arm]
        ok = [x for x in rs if x["status"] == "solved"]
        print(arm, f"solved {len(ok)}/{len(rs)}", "mean time-to-solve", round(sum(x["elapsed"] for x in ok) / max(1, len(ok)), 1))
