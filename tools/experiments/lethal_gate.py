"""STAGE9 Gate B: does `--goal-win` (patch 0002) find lethal exactly when
there is one?

Positions with a known answer: both decks are 40 Alexandrite Dragon (a
2000 ATK vanilla), the root is turn 3 (player 0's second turn), and the
opponent's starting LP decides the answer:

  lethal      both passed, opponent at 2000: one direct attack wins
  one-short   same at 2001: no win exists
  clear       each side summoned one; opponent at 2000: trade one attacker
              into the blocker, then the second goes direct
  clear-short same at 2001: no win exists

Pass: wins found for lethal and clear, none for the two short ones, and
every written win replays to the duel ending with player 0 as winner.

    venv/bin/python tools/experiments/lethal_gate.py [ms]
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skills import breaking, engine  # noqa: E402

ROOTED_BIN = (engine.VENDOR / "build" / "combo-solver-rooted" / "bin"
              / "combosolver")
ALEXANDRITE = 43096270

CASES = [  # name, opponent LP, summon on turn 1 (p0), turn 2 (p1), expect win
    ("lethal", 2000, False, False, True),
    ("one-short", 2001, False, False, False),
    ("clear", 2000, True, True, True),
    ("clear-short", 2001, True, True, False),
]


def scripted_prefix(tpl, summon_turns):
    """Responses that play turns 1-2 by script and stop at turn 3's first decision."""
    from ygorl.engine.duel import DuelSession
    from ygorl.engine.replay import Replay

    session = DuelSession(Replay.from_yrp(tpl).duel())
    summoned = set()
    while True:
        pt = session.point
        if pt is None:
            raise RuntimeError("duel ended inside the scripted prefix")
        if pt.turn >= 3:
            return list(session.tracker.result.responses)
        kinds = [a.kind for a in pt.actions]
        pick = len(kinds) - 1          # chains: pass; prompts: the last option
        if "summon" in kinds and pt.turn in summon_turns \
                and pt.turn not in summoned:
            pick = kinds.index("summon")
            summoned.add(pt.turn)
        elif "end_phase" in kinds:
            pick = kinds.index("end_phase")
        elif pt.decision.name in ("SELECT_PLACE", "SELECT_POSITION",
                                  "SELECT_DISFIELD"):
            pick = 0
        session.act(pick)


def winner_of(path):
    from ygorl.engine.replay import Replay
    import ygorl.engine.messages as M

    rep = Replay.from_yrp(Path(path))
    won = {}

    class _Obs:
        def on_start(self, core):
            pass

        def on_buffer(self, core, buf):
            for raw in M.split_messages(buf):
                m = M.decode_message(raw)
                if type(m).__name__ == "Win":
                    won["p"] = m.player

    rep.duel().replay(rep.responses, observer=_Obs())
    return won.get("p")


def main():
    from ygorl.data.environment import PlayerRules
    from ygorl.engine.duel import DuelConfig
    from ygorl.engine.replay import Replay
    from ygorl.solver.combo_solver import Workdir

    ms = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    binary = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOTED_BIN
    root = Path(tempfile.mkdtemp(prefix="duelroom-gateB-"))
    deck = root / "alex.ydk"
    deck.write_text("#main\n" + f"{ALEXANDRITE}\n" * 40 + "#extra\n!side\n")
    ok = True
    for name, lp, s1, s2, expect in CASES:
        d = root / name
        d.mkdir()
        cfg = DuelConfig(player=PlayerRules(starting_lp=lp))
        tpl = breaking.template_vs(d / "tpl.yrpX", deck, deck, seed=1,
                                   config=cfg, shuffle=True)
        turns = {t for t, on in ((1, s1), (2, s2)) if on}
        responses = scripted_prefix(tpl, turns)
        start, prefix = breaking.split_position(Replay.from_yrp(tpl),
                                                responses, d)
        wd = Workdir.create(d / "wd")
        out = d / "out"
        out.mkdir()
        cmd = [str(binary), str(prefix), "--workdir", str(wd.path)]
        for s in wd.scriptdirs:
            cmd += ["--scriptdir", str(s)]
        cmd += ["--start", str(start), "--root", str(prefix), "--no-ref",
                "--target", f"{ALEXANDRITE}@atk", "--player", "0",
                "--turns", "3", "--goal-win", "--solve-ms", str(ms),
                "--threads", "2", "--seed", "1", "--outdir", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=engine.env())
        (d / "solver.log").write_text(r.stdout + "\n--- stderr ---\n"
                                      + r.stderr)
        wins = sorted(out.glob("solution_*.yrp"))
        verified = [w for w in wins if winner_of(w) == 0]
        found = bool(verified)
        good = found == expect and len(verified) == len(wins)
        ok &= good
        print(f"{name:12s} LP {lp}: {len(wins)} written, {len(verified)} "
              f"replay to a win; expected {'win' if expect else 'none'} -> "
              f"{'ok' if good else 'WRONG'}  ({len(responses)} prefix answers)",
              flush=True)
    print(f"\nGate B {'PASS' if ok else 'FAIL'}  ({root})")


if __name__ == "__main__":
    main()
