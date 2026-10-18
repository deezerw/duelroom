"""Stage 5: flex-slot hill climbing.

ygorl ships deck tuning and MAP-Elites evolution, but both
(`tools/tune_deck.py`, `tools/evolve_decks.py`) require `--checkpoint`, a
trained policy network. This box is CPU-only with no discrete GPU and the
published agents took 32 GPUs for days, so that path is closed. What
replaces it is the plan's original one: hill climb the flex slots, judged
by the checkpoint-free solver metrics from Stages 3 and 4.

Why it is affordable at all: Stage 1's inclusion analysis already cut the
space. We are not searching 40 cards against a 13,000-card pool, we are
searching the ~8 slots that top lists disagree about, against the ~30
cards those lists actually run.

Two costs to respect:

* One candidate evaluation is one funnel_eval run. At 12 hands and a 60 s
  budget that is minutes, not seconds, so the search is measured in hours.
  `hands` is the dial; raise it only once a shortlist exists.
* Paired seeds are not optional and are already built in -- funnel_eval
  uses the same shuffle permutations for every deck it is handed. Two
  variants are therefore compared on identical hands, which is the single
  biggest variance reduction available.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import card_db, engine, gate, meta_scrape as ms
from .deck import Deck
from .stats import wilson


@dataclass
class Objective:
    """What "better" means. Weights are deliberately explicit.

    Signs matter and are easy to get backwards: for OUR deck we want
    survival HIGH (we want to play through their handtraps), whereas the
    Stage 4 matrix reads survival of META decks, where low is good for us.
    """

    brick: float = -2.0        # minimise
    solve_rate: float = 1.0    # maximise
    survival: float = 1.5      # maximise, for our own deck
    actions: float = -0.05     # shorter lines are less interruptible

    def score(self, summary):
        s = summary
        combo = (s.get("combo_actions") or {}).get("mean") or 0.0
        return (
            self.brick * (s.get("brick_rate") or 0.0)
            + self.solve_rate * (s.get("best_line_rate") or 0.0)
            + self.survival * (s.get("hand_trap_survival") or 0.0)
            + self.actions * combo
        )


@dataclass
class Candidate:
    deck: Deck
    label: str
    summary: dict = field(default_factory=dict)
    score: float = float("-inf")


def candidate_pool(conn, archetype, deck, fmt=ms.DEFAULT_FORMAT, limit=30):
    """Cards worth trying in this deck's flex slots.

    Flex and fringe cards from the archetype's own inclusion table: what
    real players of this deck disagree about. A plain text filter would
    be wider and worse -- these are already known-playable here.
    """
    buckets = ms.classify(conn, archetype, fmt=fmt, section="main")
    have = set(deck.main)
    pool = []
    for bucket in ("flex", "fringe"):
        for row in buckets[bucket]:
            if row["card_id"] in have:
                continue
            if card_db.banlist_limit(conn, row["card_id"]) == 0:
                continue
            pool.append(row)
    pool.sort(key=lambda r: -r["pct"])
    return pool[:limit]


def removable(conn, deck, archetype, fmt=ms.DEFAULT_FORMAT):
    """Cards in our list that top lists treat as optional, weakest first.

    Never touches the core: if 95% of winning lists run three copies,
    cutting one is not a tuning decision, it is a different deck.

    Ordered by how little the meta plays them, because the hill climb
    evaluates only ONE removal per candidate -- 6 slots x 30 candidates
    at minutes per evaluation is an overnight job on its own. It used to
    take whichever card came first out of a dict, which made the single
    most consequential choice in the search arbitrary. The least-played
    non-core card is a defensible cut; dict order is not.
    """
    buckets = ms.classify(conn, archetype, fmt=fmt, section="main")
    core = {r["card_id"] for r in buckets["core"]}
    pct = {r["card_id"]: r["pct"]
           for b in buckets.values() for r in b}
    counts = deck.counts("main")
    out = [(cid, n) for cid, n in counts.items() if cid not in core]
    # unknown to the meta sorts first: nobody plays it, so cutting it is
    # the least radical change available
    out.sort(key=lambda t: pct.get(t[0], 0.0))
    return out


def card_name(conn, code):
    """card_db.resolve returns a sqlite3.Row, which has no .get()."""
    row = card_db.resolve(conn, code)
    return row["name"] if row is not None else str(code)


def swap(deck, out_card, in_card, name=None):
    new = Deck(name=name or deck.name, main=list(deck.main),
               extra=list(deck.extra), side=list(deck.side))
    new.main.remove(out_card)
    new.main.append(in_card)
    return new


def threat_handtraps(top=2):
    """The handtraps worth optimising against, by measured strength.

    The objective weights handtrap survival most heavily, so WHICH
    handtraps get fired decides what the search optimises for. Left to
    funnel_eval's default that is Ash Blossom alone — and the 40-hand
    matrix put Infinite Impermanence well ahead of Ash (93.4% vs 74.1%),
    with kewl-tune playing through Imperm 0% of the time. Tuning a deck
    for Ash resilience while Imperm is the card actually beating it is
    the wrong objective.

    Falls back to Ash and Imperm if no matrix has been run.
    """
    from . import config, scenarios
    path = engine.ROOT / "results"
    files = sorted(path.glob("handtrap_matrix*.jsonl"),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        return [14558127, 10045474]
    recs = [json.loads(l) for l in files[-1].read_text().splitlines() if l.strip()]
    ranked = scenarios.weighted_handtrap_ranking(
        scenarios.handtrap_matrix(recs), config.weights())
    live = [code for code, _, score, _ in ranked if score > 0][:top]
    return live or [14558127, 10045474]


def evaluate(deck, target, hands=12, solve_ms=None, scratch=None, fire=None):
    """Score one list. Writes a temp .ydk plus a targets file for it.

    funnel_eval keys targets by deck NAME and refuses a deck it has no
    target for. Trial lists get generated names, so each one needs its
    own targets file pointing at the base deck's payoff -- otherwise
    every trial dies with "no targets for deck ...".

    Holding the target fixed across trials is also what makes the
    comparison meaningful: we are asking whether a swap reaches the SAME
    board more often, not whether it reaches some easier board.
    """
    scratch = Path(scratch or (engine.ROOT / "results" / "optimizer"))
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"{deck.name}.ydk"
    deck.write_ydk(path)

    fire = list(fire) if fire is not None else threat_handtraps()
    tfile = scratch / f"{deck.name}.targets.json"
    tfile.write_text(json.dumps({
        "_comment": "generated per trial by duelroom optimizer",
        deck.name: {"targets": [target], "fire": fire},
    }))
    rec = gate.run_funnel(path, hands=hands,
                          solve_ms=solve_ms or engine.SOLVE_MS,
                          out=scratch / f"{deck.name}.jsonl",
                          targets=tfile)
    return rec["summary"]


def hill_climb(conn, deck, archetype, target, objective=None, hands=12,
               rounds=3, pool_limit=12, solve_ms=None, log=print):
    """Greedy one-card swaps until nothing improves.

    Each candidate is tried against the single weakest non-core slot
    (see `removable`), not against every slot: the full cross product is
    hours of solver time per round. That makes this a search over what
    to ADD, with what to cut chosen by meta frequency.
    """
    # Returns (best Candidate, history). Every comparison runs on
    # identical hands, so differences are not shuffle luck -- but they
    # are still differences over `hands` hands, and `report` prints the
    # interval so a 1-point "gain" inside a 4-point CI reads as noise.
    objective = objective or Objective()
    fire = threat_handtraps()
    base_summary = evaluate(deck, target, hands, solve_ms, fire=fire)
    best = Candidate(deck, "base", base_summary, objective.score(base_summary))
    log(f"base {deck.name}: score {best.score:+.3f} "
        f"(brick {base_summary.get('brick_rate'):.2f}, "
        f"solve {base_summary.get('best_line_rate'):.2f})")

    history = [best]
    for rnd in range(rounds):
        pool = candidate_pool(conn, archetype, best.deck, limit=pool_limit)
        outs = removable(conn, best.deck, archetype)
        if not pool or not outs:
            log("no legal swap left")
            break

        improved = None
        for cand_row in pool:
            for out_card, _ in outs:
                trial = swap(best.deck, out_card, cand_row["card_id"],
                             name=f"{deck.name}_r{rnd}_{cand_row['card_id']}")
                problems = trial.validate(conn)
                if problems:
                    continue
                summary = evaluate(trial, target, hands, solve_ms, fire=fire)
                score = objective.score(summary)
                out_name = card_name(conn, out_card)
                log(f"  -{out_name} +{cand_row['name']}: {score:+.3f}")
                history.append(Candidate(trial, f"-{out_name} +{cand_row['name']}",
                                         summary, score))
                # Keep the BEST improvement, not the last one that beat
                # the base. `best.score` does not move inside this loop,
                # so a plain `score > best.score` lets every qualifying
                # candidate overwrite the previous one -- it accepted
                # Super Polymerization at +0.063 while Over Tuning sat in
                # the same history at +0.313.
                if score > best.score and (improved is None
                                           or score > improved.score):
                    improved = Candidate(trial, f"-{out_name} +{cand_row['name']}",
                                         summary, score)
                break  # one removal candidate per round keeps this finite
        if not improved:
            log(f"round {rnd}: no improvement, stopping")
            break
        best = improved
        log(f"round {rnd}: accepted {best.label} -> {best.score:+.3f}")
    return best, history


def report(history, hands):
    """Rank trials with intervals, so noise is visible as noise."""
    rows = []
    for c in history:
        s = c.summary
        solved = s.get("solved") or 0
        p, lo, hi = wilson(solved, s.get("hands") or hands)
        rows.append({
            "label": c.label,
            "score": c.score,
            "solve_rate": p,
            "ci": (lo, hi),
            "brick_rate": s.get("brick_rate"),
            "survival": s.get("hand_trap_survival"),
        })
    return sorted(rows, key=lambda r: -r["score"])


# --- side deck ------------------------------------------------------------

def side_in(deck, out_card, in_card, name=None):
    """Bring a side card in over a main-deck card, as you would between games.

    Strictly 1-for-1: `in_card` must already be IN the side deck, because
    that is what siding is. Appending a card that is not there silently
    produced a 16-card side deck and an illegal list -- caught by
    `Deck.validate`, but worth refusing outright rather than relying on a
    later check.

    To test a card that is not yet in the side, use `propose_side` to put
    it there first (displacing an existing side card), then side it in.
    """
    if in_card not in deck.side:
        raise ValueError(
            f"{in_card} is not in the side deck; use propose_side first"
        )
    new = Deck(name=name or f"{deck.name}_sided", main=list(deck.main),
               extra=list(deck.extra), side=list(deck.side))
    new.main.remove(out_card)
    new.main.append(in_card)
    new.side.remove(in_card)
    new.side.append(out_card)
    return new


def propose_side(deck, drop_side, add_side, name=None):
    """Replace one side-deck slot, keeping the side at its legal size."""
    new = Deck(name=name or f"{deck.name}_s", main=list(deck.main),
               extra=list(deck.extra), side=list(deck.side))
    new.side.remove(drop_side)
    new.side.append(add_side)
    return new


def side_plan(conn, deck, archetype, their_deck, their_target, our_target,
              candidates=None, hands=4, solve_ms=None, log=print):
    """Which side cards most improve this matchup, going second.

    The objective is deliberately different from the main-deck climb:
    a side card is judged on whether it gets us THROUGH their board, not
    on whether our own goldfish improves. Games two and three are
    overwhelmingly decided on the draw.

    Cost warning. Each candidate is `hands` full going-second runs, and
    each of those is two solves (their turn, then ours) at the solver
    budget. At four hands and 60 s that is roughly ten minutes per
    candidate, so a fifteen-slot search is an overnight job. Shortlist
    with `candidate_pool` before spending it.
    """
    from . import breaking

    def rate(d):
        tmp = Path(engine.ROOT / "results" / "side" / d.name)
        rows = breaking.going_second(their_deck, _written(d, tmp), their_target,
                                     our_target, hands=hands,
                                     solve_ms=solve_ms, scratch=tmp)
        searched = [r for r in rows if r["searched"]]
        if not searched:
            return None, rows
        return sum(r["broke"] for r in searched) / len(searched), rows

    base_rate, _ = rate(deck)
    log(f"base {deck.name} vs {Path(str(their_deck)).stem}: "
        f"{'no search' if base_rate is None else f'{base_rate*100:.0f}% through'}")

    # Default to the deck's OWN side deck: "which of my side cards help
    # this matchup" is the question that has an answer you can act on
    # between games.
    pool = candidates or sorted(set(deck.side))
    outs = removable(conn, deck, archetype)
    results = []
    for cid in pool:
        if not outs:
            break
        trial = side_in(deck, outs[0][0], cid,
                        name=f"{deck.name}_side_{cid}")
        if trial.validate(conn):
            continue
        r, _ = rate(trial)
        name = card_name(conn, cid)
        log(f"  +{name}: {'no search' if r is None else f'{r*100:.0f}%'}")
        results.append({"card_id": cid, "name": name, "rate": r,
                        "delta": None if r is None or base_rate is None
                                 else r - base_rate})
    return base_rate, sorted(
        results, key=lambda r: (r["delta"] is None, -(r["delta"] or 0)))


def _written(deck, scratch):
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    p = scratch / f"{deck.name}.ydk"
    deck.write_ydk(p)
    return p
