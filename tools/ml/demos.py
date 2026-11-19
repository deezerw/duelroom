"""Stage 16 step 1: every stored line as a ygorl demonstration (both seats).

ygorl's behaviour cloning (`ygorl.train.bc`) reads `Demonstration` records:
a start position and lines as action indices, replay-verified
(`ygorl.solver.demo.convert_line`). Our games and experiments already hold
thousands of such lines as .yrp files that replay from the duel's start:

  kill        a stored game's turn-2 kill check that killed (going first):
              seat 0 = our turn 1 (the games' search), seat 1 = their kill
  board_kill  board_data's kill lines (its turn-1 variants, kill at 4x/16x)
  break       a going-second game we won: seat 0 = their (trained) turn 1,
              seat 1 = our break

Only the TURN PLAYER's decisions are the search's; the other seat's answers on
that turn are the scripted defender's. Each record says so in
`solver["expert"] = "turn_player"`: the sample builder keeps a step only when
its player is the player whose turn it is.

    venv/bin/python tools/ml/demos.py [workers]      # resumable
    venv/bin/python tools/ml/demos.py summary
-> results/ml/demos/demos.jsonl (+ rejected.jsonl: lines that did not replay)
"""

import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "results" / "ml" / "demos"
DB = ROOT / "data" / "duelroom.db"


def sources():
    """[(kind, path, meta)] of every line on disk worth imitating."""
    out = []
    kills = ROOT / "results" / "ml" / "game_kills.jsonl"
    if kills.exists():
        for d in map(json.loads, kills.read_text().splitlines()):
            for k in d.get("kill", []):
                if k.get("lost") and k.get("line"):
                    out.append(("kill", k["line"], {"deck_hash": d["deck_hash"], "hand": d["hand_index"],
                                                    "eseed": d["enemy_seed"], "device": d["device"],
                                                    "defence": k["defence"]}))
    reps = ROOT / "results" / "ml" / "boards" / "replays"
    if reps.exists():
        for f in sorted(reps.glob("*/kill*-h*.yrp")):
            out.append(("board_kill", str(f), {"tag": f.parent.name}))
    conn = sqlite3.connect(DB)
    for dh, h, e, dev, run, line in conn.execute(
            "SELECT deck_hash, hand_index, enemy_seed, device, run, line FROM game_results "
            "WHERE first=0 AND outcome='win' AND line IS NOT NULL"):
        out.append(("break", line, {"deck_hash": dh, "hand": h, "eseed": e, "device": dev, "run": run}))
    return [s for s in out if Path(s[1]).exists()]


def convert(src):
    """One line -> a Demonstration as JSON, or ("rejected", reason)."""
    from skills import engine
    os.environ.update(engine.env())
    from ygorl.engine.replay import Replay
    from ygorl.solver.demo import Demonstration, convert_line

    kind, path, meta = src
    try:
        rep = Replay.from_yrp(Path(path))
        ln = convert_line(path, [], close_turn=False)
    except Exception as exc:  # noqa: BLE001 -- reported per line
        return {"rejected": True, "kind": kind, "path": path, "error": f"{type(exc).__name__}: {exc}"}
    a = rep.decks["a"]
    demo = Demonstration(deck={"name": a.get("name") or "a", "main": list(a["main"]), "extra": list(a["extra"])},
                         hand_index=int(meta.get("hand", -1)), hand_seed=0, hand=[], variant="plain",
                         targets=[], status="solved")
    demo.set_start(path)
    ln.source = str(path)
    demo.lines = [ln]
    demo.solver = {"kind": kind, "expert": "turn_player", **meta,
                   "steps": Counter(ln.players).most_common()}
    return demo.to_json()


def _key(src):
    return hashlib.sha1(src[1].encode()).hexdigest()[:16]


def main(workers=4):
    OUT.mkdir(parents=True, exist_ok=True)
    out, rej = OUT / "demos.jsonl", OUT / "rejected.jsonl"
    done = set()
    for f in (out, rej):
        if f.exists():
            done |= {json.loads(l)["key"] for l in f.read_text().splitlines()}
    todo = [s for s in sources() if _key(s) not in done]
    print(f"{len(todo)} line(s) to convert, {len(done)} done", flush=True)
    with ProcessPoolExecutor(int(workers)) as ex, out.open("a") as fo, rej.open("a") as fr:
        for n, (src, rec) in enumerate(zip(todo, ex.map(convert, todo, chunksize=8)), 1):
            rec["key"] = _key(src)
            (fr if rec.get("rejected") else fo).write(json.dumps(rec, separators=(",", ":")) + "\n")
            if n % 500 == 0:
                fo.flush()
                fr.flush()
                print(f"{n}/{len(todo)}", flush=True)
    summary()


def summary():
    recs = [json.loads(l) for l in (OUT / "demos.jsonl").read_text().splitlines()]
    rej = ([json.loads(l) for l in (OUT / "rejected.jsonl").read_text().splitlines()]
           if (OUT / "rejected.jsonl").exists() else [])
    by = Counter(r["solver"]["kind"] for r in recs)
    steps = Counter()
    for r in recs:
        for p, n in r["solver"]["steps"]:
            steps[(r["solver"]["kind"], p)] += n
    print(f"{len(recs)} lines converted, {len(rej)} rejected")
    for k, n in sorted(by.items()):
        print(f"  {k:11} {n:5} lines, steps seat 0 {steps[(k, 0)]}, seat 1 {steps[(k, 1)]}")
    for e, n in Counter(r["error"].split(":")[0] for r in rej).most_common(5):
        print(f"  rejected: {e} x{n}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["summary"]:
        summary()
    else:
        main(*sys.argv[1:2])
