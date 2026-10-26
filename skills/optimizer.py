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
from collections import Counter
from datetime import datetime, timezone
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
    recovery: float = 0.5      # maximise: share of handtrap windows survived

    def score(self, summary):
        s = summary
        combo = (s.get("combo_actions") or {}).get("mean") or 0.0
        return (
            self.brick * (s.get("brick_rate") or 0.0)
            + self.solve_rate * (s.get("best_line_rate") or 0.0)
            + self.survival * (s.get("hand_trap_survival") or 0.0)
            + self.actions * combo
            + self.recovery * mean_window_recovery(s)
        )



def mean_window_recovery(summary):
    """Mean share of handtrap windows recovered, over the fired cards.

    Added because the objective could not SEE the effect that dominates
    handtrap resilience on a real deck. Measured on the active deck, 20
    hands, Infinite Impermanence:

      line length vs windows : correlation +0.75 (6 actions -> 4-9
                               windows; 12-13 actions -> 11-17)
      recoveries per hand    : flat at 3-4 regardless of line length
      short lines (6-8)      : 51 windows, 25 recovered = 49.0%
      long lines (10-13)     : 93 windows, 22 recovered = 23.7%

    So a longer line adds windows but not answers, and per-window
    recovery halves. The objective's two handtrap-related terms both miss
    this: `survival` is an all-or-nothing all-windows standard and
    saturates at 0.0 for any deck with a long line, and `actions` was
    treated as a -0.05 tiebreak. The quantity that actually varies, and
    that a deckbuilder can act on, was in the summary and unused.

    WEIGHT IS A JUDGEMENT CALL, not a measurement: 0.5 makes recovery
    matter without letting it outvote bricking. It wants calibrating
    against real results before anyone trusts the ordering it produces.
    Set `recovery=0` to score exactly as before.
    """
    wr = summary.get("window_recovery") or {}
    shares = [v for v in wr.values() if isinstance(v, (int, float))]
    return sum(shares) / len(shares) if shares else 0.0


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
            # `banlist_limit` raises on a status it cannot read. Here
            # the check EXCLUDES forbidden candidates, so an unreadable
            # status must also exclude -- never suggest a card whose
            # legality could not be established. Unguarded, one odd card
            # killed the whole pool build and with it an `optimize` run
            # that costs hours.
            try:
                if card_db.banlist_limit(conn, row["card_id"]) == 0:
                    continue
            except ValueError:
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


def evaluate(deck, target, hands=12, solve_ms=None, scratch=None, fire=None,
             confirm_draws=3, log=None):
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

    # The fire list MUST go on the command line. funnel_eval's
    # load_deck_targets returns only v["targets"] and discards every
    # other key, so a "fire" entry in this file is read by nothing --
    # writing one looked like a fix and changed no behaviour at all.
    # It is still written for provenance, but --fire is what acts.
    fire = list(fire) if fire is not None else threat_handtraps()
    tfile = scratch / f"{deck.name}.targets.json"
    tfile.write_text(json.dumps({
        "_comment": "fire is recorded here for provenance; funnel_eval "
                    "only honours --fire on the command line",
        deck.name: {"targets": [target], "fire": fire},
    }))
    rec = gate.run_funnel(path, hands=hands,
                          solve_ms=solve_ms or engine.SOLVE_MS,
                          out=scratch / f"{deck.name}.jsonl",
                          targets=tfile, fire=fire)

    # Verify what was actually fired rather than what was requested.
    fired = set()
    for h in rec.get("hands") or []:
        fired |= {int(c) for c in (h.get("fire") or {})}
    if fired and set(fire) - fired:
        missing = sorted(set(fire) - fired)
        raise RuntimeError(
            f"asked to fire {sorted(fire)} but the run only fired "
            f"{sorted(fired)} (missing {missing}) — the objective would be "
            f"scoring the wrong handtraps")

    summary = dict(rec["summary"])
    # Correct the brick rate before it is scored. One draw of a randomized
    # search called 5 of this deck's 20 hands bricks; the multi-draw sweep
    # solves all 20. Those false bricks hit the term weighted -2.0.
    if confirm_draws:
        check = confirm_bricks(path, rec, draws=confirm_draws,
                               solve_ms=solve_ms, log=log)
        n = summary.get("hands") or len(rec.get("hands") or []) or 0
        if check["false_bricks"] and n:
            summary["brick_rate"] = round(check["bricks"] / n, 4)
            summary["bricks"] = check["bricks"]
            summary["best_line_rate"] = round(1 - check["bricks"] / n, 4)
            summary["false_bricks"] = check["false_bricks"]
            summary["brick_rate_single_draw"] = rec["summary"].get("brick_rate")
    return summary


def confirm_bricks(deck_path, record, draws=3, solve_ms=None, log=None):
    """Re-solve the hands funnel_eval called bricks, with several draws.

    The optimiser weights brick rate at -2.0, the second heaviest term,
    and funnel_eval measures it with ONE draw of a randomized search.
    Measured 2026-10-24 on the user's own deck, same 20 hands: the
    multi-draw sweep solved 20/20 with zero bricks while the funnel
    reported brick 5/20 = 0.25. Five false bricks, all in the pessimistic
    direction, on the term that dominates the score.

    Only the bricked hands are re-drawn, so the cost is proportional to
    how wrong the measurement was: nothing when the deck really bricks,
    and a few solves when it does not. A hand stays a brick only if no
    draw reaches the target.

    Returns {"bricks", "false_bricks", "rechecked", "hand_seeds"} and does
    not mutate `record`.
    """
    from . import sweep

    say = log or (lambda m: None)
    solve_ms = solve_ms or engine.SOLVE_MS
    targets = record.get("targets") or []
    # funnel_eval nests one target list per deck.
    flat = [t for group in targets for t in
            (group if isinstance(group, (list, tuple)) else [group])]
    bricked = [h for h in (record.get("hands") or [])
               if h.get("status") == "brick" and h.get("hand")]
    if not flat or not bricked:
        return {"bricks": len(bricked), "false_bricks": 0,
                "rechecked": 0, "hand_seeds": []}

    name = Path(deck_path).stem
    work = []
    for h in bricked:
        for d in range(draws):
            work.append((str(deck_path), name, h.get("index", 0),
                         h.get("hand_seed", 0), list(h["hand"]), list(flat),
                         "recheck", solve_ms, 100 + d))
    say(f"  re-drawing {len(bricked)} bricked hand(s) x {draws} draws "
        f"to tell a brick from one bad draw")

    solved = set()
    # Through sweep's own seam rather than a second pool: it already
    # carries the nice initializer, and it is the point tests replace.
    for res in sweep._map_solves(work, engine.DEFAULT_WORKERS):
        if res.get("score") is not None:
            solved.add(res["hand_index"])
    false_n = sum(1 for h in bricked if h.get("index", 0) in solved)
    if false_n:
        say(f"  {false_n} of {len(bricked)} were NOT bricks — a different "
            f"seed reached the target")
    return {"bricks": len(bricked) - false_n, "false_bricks": false_n,
            "rechecked": len(bricked),
            "hand_seeds": [h.get("hand_seed") for h in bricked]}


def hill_climb(conn, deck, archetype, target, objective=None, hands=12,
               slots=1,
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

    # Checkpoint after every trial. A run is hours, and killing one --
    # which happens, e.g. when the objective itself turns out to be
    # wrong -- otherwise loses every evaluation with it. Each line is a
    # finished trial, so an interrupted run is still a usable shortlist.
    ledger = engine.ROOT / "results" / f"optimize_{deck.name}.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # Do NOT truncate. This file is described as resumption insurance, and
    # `write_text("")` made the next attempt erase the previous one's work
    # -- the opposite of a checkpoint. A crashed overnight run is exactly
    # when you want the trials it did finish.
    if ledger.exists() and ledger.stat().st_size:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        kept = ledger.with_name(f"{ledger.stem}.{stamp}.jsonl")
        ledger.replace(kept)
        log(f"previous ledger kept as {kept.name}")
    ledger.write_text("")

    def _record(label, summary, score):
        with ledger.open("a") as fh:
            fh.write(json.dumps({
                "label": label, "score": score,
                "solved": summary.get("solved"), "hands": summary.get("hands"),
                "brick_rate": summary.get("brick_rate"),
                "survival": summary.get("hand_trap_survival"),
                # `degenerate_terms` needs all four terms. Without these
                # two it could only be computed live, in memory, so a
                # stored run could not be checked for an empty ranking.
                "best_line_rate": summary.get("best_line_rate"),
                "combo_actions_mean": (summary.get("combo_actions")
                                       or {}).get("mean"),
                "false_bricks": summary.get("false_bricks"),
                "window_recovery": summary.get("window_recovery"),
                "brick_rate_single_draw": summary.get("brick_rate_single_draw"),
                "fire": fire,
            }) + "\n")
    base_summary = evaluate(deck, target, hands, solve_ms, fire=fire,
                            log=log)
    best = Candidate(deck, "base", base_summary, objective.score(base_summary))
    _record("base", base_summary, best.score)
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
            # `slots` cut candidates per add, not one. The loop used to end
            # with an unconditional `break` ("keeps this finite"), so only
            # the first removable card was ever cut -- the search was 1 cut
            # x 26 adds = 26 of a 22 x 26 = 572 space, and the one cut was
            # whichever card `removable` ranked first.
            #
            # Default 1 keeps the old cost exactly: widening multiplies
            # trials, and every trial is `hands` full funnel runs. slots=3
            # on this deck is 78 trials, which is most of a day.
            for out_card, _ in outs[:max(1, slots)]:
                trial = swap(best.deck, out_card, cand_row["card_id"],
                             # The cut belongs in the name. Without it two
                             # trials that add the same card over different
                             # cuts share a scratch path, so `evaluate`
                             # overwrites one with the other and `rescore`
                             # only ever sees the last.
                             name=f"{deck.name}_r{rnd}_"
                                  f"{out_card}_{cand_row['card_id']}")
                problems = trial.validate(conn)
                if problems:
                    continue
                summary = evaluate(trial, target, hands, solve_ms,
                                   fire=fire, log=log)
                score = objective.score(summary)
                out_name = card_name(conn, out_card)
                log(f"  -{out_name} +{cand_row['name']}: {score:+.3f}")
                label = f"-{out_name} +{cand_row['name']}"
                history.append(Candidate(trial, label, summary, score))
                _record(label, summary, score)
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
        if not improved:
            log(f"round {rnd}: no improvement, stopping")
            break
        best = improved
        log(f"round {rnd}: accepted {best.label} -> {best.score:+.3f}")
    return best, history


def degenerate_terms(history):
    """Which objective terms did not vary across the trials.

    A term with one distinct value cannot rank anything. Measured on
    kewl-tune at 12 hands, THREE of the four were constant — solve rate,
    brick rate and handtrap survival were identical in every trial — so
    the whole ordering came from the combo-length tiebreak at weight
    -0.05, a term that was never meant to decide anything.

    That ordering is not wrong so much as empty, and an empty ordering
    presented as a ranking is the failure this project keeps producing.
    Returns (constant_terms, varying_terms). Five terms now, not four:
    window recovery was added to the objective and a ranking resting on it
    alone would otherwise have passed this check unnoticed.
    """
    keys = {
        "solve rate": lambda c: (c.summary or {}).get("best_line_rate"),
        "brick rate": lambda c: (c.summary or {}).get("brick_rate"),
        "handtrap survival": lambda c: (c.summary or {}).get("hand_trap_survival"),
        "combo length": lambda c: ((c.summary or {}).get("combo_actions")
                                   or {}).get("mean"),
        "window recovery": lambda c: round(
            mean_window_recovery(c.summary or {}), 6),
    }
    constant, varying = [], []
    for name, get in keys.items():
        vals = {get(c) for c in history}
        (varying if len(vals) > 1 else constant).append(name)
    return constant, varying


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
            # `survival` is an all-windows standard, so it reads 0% on
            # decks whose lines merely give the handtrap too many chances.
            # Per-window recovery is the number that actually moves, and
            # without it here the caller cannot show the two together.
            "window_recovery": s.get("window_recovery") or {},
            "false_bricks": s.get("false_bricks"),
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

def stored_trials(deck_name, scratch=None):
    """Every stored funnel record for a deck's optimiser trials.

    `evaluate` writes one JSONL per trial, named after the trial, and they
    accumulate -- so a finished run can be re-ranked without spending the
    solver time again. Returns [(label, summary)], base first.
    """
    scratch = Path(scratch or (engine.ROOT / "results" / "optimizer"))
    out = []
    for path in sorted(scratch.glob(f"{deck_name}*.jsonl")):
        try:
            rows = [json.loads(l) for l in
                    path.read_text().splitlines() if l.strip()]
        except (OSError, ValueError):
            continue
        if not rows:
            continue
        summary = rows[0].get("summary") or {}
        if not summary:
            continue
        label = path.stem
        out.append(("base" if label == deck_name else label, summary))
    return sorted(out, key=lambda t: t[0] != "base")


def trial_change(label, deck_name, scratch=None):
    """What a stored trial actually did to the base list, from its .ydk.

    `evaluate` writes the trial's decklist next to its JSONL, so the edit
    is recoverable exactly. Reading it from the FILENAME is not reliable:
    trial names gained the cut partway through this project
    (`<deck>_r<round>_<add>` became `<deck>_r<round>_<cut>_<add>`), so the
    older half of the stored runs name only the card that went in. The
    decklists prove those were 1-for-1 swaps all along --
    `blue-eyes-branded-test_r0_24224830.ydk` and
    `..._r0_30576089_24224830.ydk` are byte-identical, and both cut
    30576089 -- so the old names were rendering a tested swap as an
    untested pure addition, which is a different (and illegal, at 45
    cards) list.

    Returns {"cut": [...], "add": [...], "list_hash": "..."} or None when
    the .ydk is not there to read.
    """
    scratch = Path(scratch or (engine.ROOT / "results" / "optimizer"))
    trial_ydk = scratch / f"{label}.ydk"
    base_ydk = scratch / f"{deck_name}.ydk"
    if not trial_ydk.exists() or not base_ydk.exists():
        return None
    try:
        trial, base = Deck.from_ydk(trial_ydk), Deck.from_ydk(base_ydk)
    except OSError:
        return None
    cut, add = [], []
    for section in ("main", "extra", "side"):
        before = Counter(getattr(base, section))
        after = Counter(getattr(trial, section))
        cut += sorted((before - after).elements())   # left the deck
        add += sorted((after - before).elements())   # joined it
    return {"cut": cut, "add": add, "list_hash": trial.list_hash}


def rescore(deck_name, objective=None, scratch=None):
    """Re-rank a finished run's trials under `objective`.

    The weights in `Objective` are judgement calls -- `recovery` especially
    -- and each run costs hours of solver, so being unable to ask "what
    would this have ranked under different weights" meant the weights
    could never be examined at all. Every term is in the stored summaries.

    Returns rows sorted best first, each with the score and the terms it
    came from.
    """
    objective = objective or Objective()
    rows = []
    for label, s in stored_trials(deck_name, scratch):
        row = {
            "label": label,
            "score": objective.score(s),
            "brick_rate": s.get("brick_rate"),
            "best_line_rate": s.get("best_line_rate"),
            "survival": s.get("hand_trap_survival"),
            "combo_mean": (s.get("combo_actions") or {}).get("mean"),
            "window_recovery": round(mean_window_recovery(s), 4),
            "hands": s.get("hands"),
            "measured": 1,
        }
        row.update(trial_change(label, deck_name, scratch)
                   or {"cut": None, "add": None, "list_hash": None})
        rows.append(row)
    return sorted(_merge_duplicate_lists(rows), key=lambda r: -r["score"])


def _merge_duplicate_lists(rows):
    """One decklist is one candidate, however many names it was stored under.

    The stored runs contain the same list twice: a trial named
    `<deck>_r0_<add>` from before the cut was part of the name, and
    `<deck>_r0_<cut>_<add>` from after. Identical .ydk, identical summary
    -- the solver is deterministic given the same deck, hands and target,
    so re-running it reproduced the numbers exactly. A ranking table that
    prints both shows one experiment as two agreeing data points, which
    is how a ranking gets believed harder than its evidence.

    Collapses only on identical metrics. If two records of the same
    decklist DISAGREE, that is a finding rather than a duplicate, so both
    rows stay and are flagged.
    """
    def metrics(r):
        return (r["brick_rate"], r["best_line_rate"], r["survival"],
                r["combo_mean"], r["window_recovery"], r["hands"])

    groups, out = {}, []
    for r in rows:
        if r["label"] == "base" or not r["list_hash"]:
            out.append(r)
            continue
        groups.setdefault(r["list_hash"], []).append(r)
    for group in groups.values():
        if len({metrics(r) for r in group}) == 1:
            # Keep the longest label: it is the one naming both halves of
            # the swap, and so the one a reader can trace back.
            keep = max(group, key=lambda r: len(r["label"]))
            keep["measured"] = len(group)
            out.append(keep)
        else:
            for r in group:
                r["inconsistent"] = len(group)
                out.append(r)
    return out

def paired_check(deck_path, record, draws=3, solve_ms=None, log=None):
    """Re-solve EVERY hand of a funnel record with the multi-draw path.

    This is the comparison that was missing, and its absence produced the
    worst error of this project's 2026-10-24 session: I reported the
    optimiser's brick rate as "25 points wrong" by holding a sweep that
    solved 20/20 against a funnel that reported 5/20, when the two runs
    shared neither their hands (0 of 20 seeds overlapped) nor their goals
    (the sweep aims at each hand's own ladder; the funnel at one payoff
    card). Two numbers measuring different things, reported as a defect.

    Here the hands and the target are the funnel's own, so a disagreement
    is a real disagreement. Returns per-hand rows plus the counts:

      agree_solved   both reached the target
      agree_brick    neither did, over `draws` seeds
      funnel_pessimistic  funnel said brick, a re-draw reached it
      funnel_optimistic   funnel said solved, no re-draw reached it

    The last is possible and worth knowing: the funnel's single draw may
    have found a line the re-draws missed, which bounds how much of its
    solve rate is luck rather than reach.
    """
    from . import sweep

    say = log or (lambda m: None)
    solve_ms = solve_ms or engine.SOLVE_MS
    targets = record.get("targets") or []
    flat = [t for g in targets for t in
            (g if isinstance(g, (list, tuple)) else [g])]
    hands = [h for h in (record.get("hands") or []) if h.get("hand")]
    if not flat or not hands:
        return {"rows": [], "agree_solved": 0, "agree_brick": 0,
                "funnel_pessimistic": 0, "funnel_optimistic": 0}

    name = Path(deck_path).stem
    work = []
    for h in hands:
        for d in range(draws):
            work.append((str(deck_path), name, h.get("index", 0),
                         h.get("hand_seed", 0), list(h["hand"]), list(flat),
                         "paired", solve_ms, 200 + d))
    say(f"  paired check: {len(hands)} hand(s) x {draws} draws against the "
        f"funnel's own hands and target")

    reached = set()
    for res in sweep._map_solves(work, engine.DEFAULT_WORKERS):
        if res.get("score") is not None:
            reached.add(res["hand_index"])

    rows, counts = [], {"agree_solved": 0, "agree_brick": 0,
                        "funnel_pessimistic": 0, "funnel_optimistic": 0}
    for h in hands:
        i = h.get("index", 0)
        funnel_solved = h.get("status") == "solved"
        multi_solved = i in reached
        if funnel_solved and multi_solved:
            key = "agree_solved"
        elif not funnel_solved and not multi_solved:
            key = "agree_brick"
        elif multi_solved:
            key = "funnel_pessimistic"
        else:
            key = "funnel_optimistic"
        counts[key] += 1
        rows.append({"index": i, "hand_seed": h.get("hand_seed"),
                     "funnel": h.get("status"),
                     "multi_draw": "solved" if multi_solved else "unsolved",
                     "verdict": key})
    return {"rows": rows, **counts}

