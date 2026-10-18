"""Scenario suite: handtrap matrix and derived solver targets.

`funnel_eval` fires a handtrap into every solved hand and reports whether
the line survives -- that is the handtrap matrix. It wants a per-deck
targets file, though, and ygorl only ships one for its own ten test decks.
Scraped tournament lists have no curated target.

Rather than hand-writing targets for decks nobody here plays, derive them:
`solve_blocking` already searched each deck with automatically planned
targets and recorded which ones it actually reached. The most frequently
reached target IS that deck's canonical payoff, measured instead of
guessed, and it updates itself when the meta moves.
"""

import json
from collections import Counter
from pathlib import Path

from . import card_db, engine

# The handtraps worth testing against. Passcodes verified against the
# card mirror; names kept for reports.
HANDTRAPS = {
    14558127: "Ash Blossom & Joyous Spring",
    10045474: "Infinite Impermanence",
    23434538: 'Maxx "C"',
    27204311: "Nibiru, the Primal Being",
    94145021: "Droll & Lock Bird",
    59438930: "Ghost Ogre & Snow Rabbit",
    97268402: "Effect Veiler",
}

# Handtraps this metric structurally cannot judge. The question asked is
# "did the line still reach its target", and these do not try to stop a
# line -- Maxx "C" punishes one with cards, Droll shuts off searching
# without necessarily preventing the target. A high survival against
# them is the metric failing to see the card, not the deck being fine.
# Re-examined against the raw fire entries after the `survives` fix.
# Maxx "C" stays: 856 of 908 windows convert (94%), so lines do continue
# through it, and the real cost -- the cards it hands the opponent -- is
# not what "did the line reach its target" measures. Droll is REMOVED:
# only 45 of 85 windows convert and 19 hands died to it, so the metric
# does capture what Droll does. It was only labelled unmeasurable
# because the earlier status-based reading scored it a spurious 0%.
BLIND_SPOTS = {
    23434538: ("lines convert 94% of its windows, so this measures "
               "completion, not the cards it hands them"),
}

MAXX_C = 23434538


# Common floodgates for the Stage 4 static check. Locals run these far more
# than top tables do.
FLOODGATES = {
    82732705: "Skill Drain",
    83326048: "Dimensional Barrier",
    90846359: "Rivalry of Warlords",
    97642679: "There Can Be Only One",
    23616456: "Summon Limit",
}


# How much a target's zone implies you actually BUILT something.
# Higher is more substantive. A monster you assembled on the field is a
# payoff; a card you set from your hand is a draw plus one action.
ZONE_RANK = {
    "atk": 3, "def": 3, "": 3,          # on the field, summoned
    "grave": 2, "banished": 2, "removed": 2,
    "szone": 1, "szone:fd": 1,          # set backrow
    "hand": 0,                           # not built at all
}

# Below this many actions a target is reached incidentally rather than
# worked towards, and firing a handtrap at it measures nothing.
TRIVIAL_ACTIONS = 4


def _zone_of(spec):
    return spec.split("@", 1)[1] if "@" in spec else ""


def _zone_rank(spec):
    return ZONE_RANK.get(_zone_of(spec), 1)


def derive_targets(jsonl_path, per_deck=1, min_hits=1):
    """Build a funnel_eval targets file from solve_blocking results.

    Ranked by SUBSTANCE, not frequency. ygorl's automatic target planner
    will happily propose "Droll in hand" or "Called by the Grave set
    face-down" for a deck with no real payoff board, and taking the most
    common proposal then picks exactly those: Blitzclique derived
    "Called by the Grave@szone:fd", reached in 6 actions, with the whole
    six-hand solve finishing in two seconds. Firing Ash Blossom at "did
    you manage to set a card" measures nothing, and every downstream
    number inherits that emptiness.

    The strongest signal is whether the target lives in the EXTRA deck.
    An extra-deck monster is available whenever the engine works; a
    main-deck one-of has to be DRAWN, so making it the goal measures draw
    luck rather than the deck. Blitzclique derived "Called by the Grave",
    a main-deck 1-of, and then gated at 0/12 -- while the same deck
    solved 6/6 under per-hand automatic targets. Six of the seven meta
    decks that behave well target an extra-deck monster; the one that
    does not (elfnote, on a main-deck Ars Magna) was quietly suspect for
    the same reason.

    After that: where the target ends up -- a monster you assembled beats
    a card you set, which beats a card merely in hand -- and then how
    often the deck reaches it.

    Frequency, not line length, is the right second key. Line length is
    only available per RECORD, so every target in one record shares a
    number and it cannot discriminate within a hand; ordering by it
    picked generic extra-deck monsters that happen to sit in long lines
    (S:P Little Knight for Sky Striker, F.A. Dawn Dragster for
    Fiendsmith) over each deck's own boss. Frequency recovers Sky Striker
    Ace = Zero, Fiendsmith's Desirae, Kewl Tune RS and Red-Eyes Dark
    Dragoon -- the cards those decks are actually trying to make.

    A targets list is also a CONJUNCTION -- the final board must contain
    every entry -- so one per deck by default. ygorl's own curated file
    uses a single target for nine of its ten decks.
    """
    records = [
        json.loads(l)
        for l in Path(jsonl_path).read_text().splitlines()
        if l.strip()
    ]
    stats, extras = {}, {}
    for r in records:
        if r.get("status") != "solved":
            continue
        d = r.get("deck")
        name = d.get("name") if isinstance(d, dict) else str(d)
        if isinstance(d, dict):
            extras.setdefault(name, set()).update(d.get("extra") or [])
        lines = r.get("lines") or []
        actions = max((len(l.get("actions") or []) for l in lines), default=0)
        for t in (r.get("targets") or []):
            acc = stats.setdefault(name, {}).setdefault(
                t, {"hits": 0, "actions": []})
            acc["hits"] += 1
            acc["actions"].append(actions)

    out = {}
    for deck, cand in stats.items():
        scored = []
        for spec, acc in cand.items():
            mean_actions = sum(acc["actions"]) / len(acc["actions"])
            code = int(spec.split("@")[0])
            scored.append((
                1 if code in extras.get(deck, ()) else 0,  # always makeable
                _zone_rank(spec),          # built > set > held
                acc["hits"],               # the deck's characteristic payoff
                mean_actions,              # weak tiebreak, see below
                spec,
            ))
        scored.sort(reverse=True)
        kept = [spec for *_, spec in scored[:per_deck]
                if cand[spec]["hits"] >= min_hits] or [scored[0][-1]]
        best = scored[0]
        out[deck] = {
            "targets": kept,
            "fire": sorted(HANDTRAPS),
            "_extra": best[0],
            "_zone_rank": best[1],
            "_mean_actions": round(best[3], 1),
        }
    return out


def trivial_targets(derived):
    """Decks whose best available target is not worth measuring.

    Returns [(deck, reason)]. These are not failures of the solver --
    some decks genuinely do not build a board -- but their solve rate
    and handtrap survival mean much less than they look like they do,
    and saying so is better than quietly reporting a confident number.
    """
    bad = []
    for deck, spec in derived.items():
        if deck.startswith("_"):
            continue
        # Derive the zone from the target itself rather than trusting a
        # stored key. `_zone_rank` and `_mean_actions` are stripped before
        # the file is written, so calling this on a SAVED spec silently
        # defaulted rank to 3 and the zone check could never fire -- half
        # the function quietly did nothing whenever it was handed data
        # read back off disk.
        targets = spec.get("targets") or []
        rank = (_zone_rank(targets[0]) if targets
                else spec.get("_zone_rank", 3))
        acts = spec.get("_mean_actions", 99)
        if not spec.get("_extra", 1):
            bad.append((deck, "best target is a main-deck card that must be "
                              "DRAWN — measures draw luck, not the deck"))
            continue
        # Zone is the trustworthy signal. The action count is only
        # available per record, so every target in a record shares one
        # number -- relying on it alone flagged branded's Mirrorjade as
        # "reached in 1 action", which is plainly false. It is used only
        # to sharpen the message for targets already suspect by zone.
        if rank <= 0:
            bad.append((deck, "best target is only a card in hand"))
        elif rank <= 1:
            detail = (f", reached in about {acts:.0f} actions"
                      if acts < TRIVIAL_ACTIONS else "")
            bad.append((deck, f"best target is a set card, not a board{detail}"))
    return bad


def write_targets(jsonl_path, out_path, per_deck=1, min_hits=2):
    """Refresh targets from a solve run, MERGING into what is already there.

    This must not clobber. The file holds two kinds of entry: meta decks
    derived from the board library, and user decks derived on demand by
    `ensure_target` at the cost of a solve pass each. Rebuilding it
    wholesale from the board library silently deleted every user deck --
    import your list, spend minutes deriving its target, run `targets` to
    refresh the meta, and it is gone with no error.
    """
    targets = derive_targets(jsonl_path, per_deck, min_hits)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for spec in targets.values():
        for k in ("_built_targets", "_zone_rank", "_mean_actions"):
            spec.pop(k, None)

    existing = load_targets(out_path)
    existing.pop("_comment", None)
    kept = {k: v for k, v in existing.items() if k not in targets}
    payload = {
        "_comment": (
            "Derived by duelroom: the substantive target each deck reaches "
            "most often. Refreshing merges — entries for decks absent from "
            "the latest solve run are preserved."
        ),
        **kept,
        **targets,
    }
    out_path.write_text(json.dumps(payload, indent=1))
    return targets


def describe_targets(conn, targets):
    """Render a derived targets file with card names."""
    rows = []
    for deck, spec in sorted(targets.items()):
        named = []
        for t in spec["targets"]:
            code = int(t.split("@")[0])
            row = card_db.resolve(conn, code)
            zone = f"@{t.split('@')[1]}" if "@" in t else ""
            named.append((row["name"] if row else f"<{code}>") + zone)
        rows.append((deck, named))
    return rows


# --- handtrap matrix ------------------------------------------------------

def handtrap_matrix(records):
    """Per deck per handtrap: survival, computed from the per-hand records.

    NOT `summary.survival_given_line`. That field counts a `no_window`
    outcome -- the handtrap never had a legal moment to be used -- as a
    survival, which conflates two opposite facts and inverts the answer.

    Sky-striker vs Nibiru is the clear case: the summary reports 90%
    survival, but the per-hand records are `no_window` nine times and
    `unsolved` once. Nibiru got exactly one legal window and STOPPED the
    line. Where testable its survival is 0%, not 90%, and the inflated
    number is most of why Nibiru ranked last among handtraps to main.

    So report both, because both matter and they are different questions:
      survival  -- of the windows where it COULD be used, how often did
                   the line still complete (did they play through it)
      windows   -- how often it had any legal window at all (is it even
                   live in this matchup)
    A handtrap with no window is useless here, but for a different reason
    than one that gets played through, and the fixes differ.
    """
    matrix = {}
    for rec in records:
        s = rec.get("summary") or {}
        deck = s.get("deck")
        if not deck:
            continue
        tested, survived, offered = Counter(), Counter(), Counter()
        for h in rec.get("hands") or []:
            if h.get("status") != "solved":
                continue
            for code, f in (h.get("fire") or {}).items():
                code = int(code)
                offered[code] += 1
                st = f.get("status")
                if st == "no_window":
                    continue
                tested[code] += 1
                # `survives` is authoritative. `status` reports whether the
                # SEARCH succeeded, not whether the line lived: an entry can
                # read status "solved" with windows 6, converted 3,
                # survives False -- the solver searched fine and the line
                # still died in half its windows. Reading status as survival
                # flipped Infinite Impermanence from a correct 0% to 100%.
                if f.get("survives"):
                    survived[code] += 1
        row = {}
        for code in offered:
            n = tested[code]
            row[code] = {
                "survival": (survived[code] / n) if n else None,
                "tested": n,
                "offered": offered[code],
                "window_rate": n / offered[code] if offered[code] else 0.0,
            }
        matrix[deck] = {
            "cells": row,
            "solved": s.get("solved"),
            "hands": s.get("hands"),
            "brick_rate": s.get("brick_rate"),
        }
    return matrix


def weighted_handtrap_ranking(matrix, weights, exclude=None):
    """Which handtraps to main, weighted by who you actually face.

    `weights` is the locals.toml share per archetype. A handtrap that
    blanks a deck nobody in your room plays is worth nothing, which is
    the entire reason that file exists.
    """
    # (deck, card) pairs whose measurement is known to be an artifact.
    # Counting them as a zero is not neutral: it drags a card's score
    # down with data we have already decided not to believe. They are
    # dropped from both the numerator and that card's weight total, so
    # the card is scored over the decks it was actually measured on.
    exclude = set(exclude or ())
    scores, coverage, card_weight = Counter(), Counter(), Counter()
    total_w = 0.0
    for deck, data in matrix.items():
        w = weights.get(deck, 0.0)
        if not w:
            continue
        total_w += w
        for code, cell in data["cells"].items():
            if (deck, code) in exclude:
                continue
            card_weight[code] += w
            # Expected value of drawing it: it must have a window AND the
            # line must fail through it. Scoring stop-rate alone would
            # reward a handtrap that is never live, which is the trap the
            # summary field falls into.
            surv = cell["survival"]
            stop_given_window = 0.0 if surv is None else (1.0 - surv)
            scores[code] += w * cell["window_rate"] * stop_given_window
            coverage[code] += w * cell["window_rate"]
    for code in list(scores):
        denom = card_weight[code] or total_w
        if denom:
            scores[code] /= denom
            coverage[code] /= denom
    return [
        (code, HANDTRAPS.get(code, str(code)), score, coverage[code])
        for code, score in scores.most_common()
    ]


def run_matrix(decks, targets_path, hands=12, solve_ms=None, workers=None,
               fire_ms=3000, out=None, timeout=None):
    """funnel_eval across several decks with every handtrap fired.

    The output path carries the hand count. funnel_eval resumes by
    SKIPPING any deck already present in the target file, which is right
    for continuing an interrupted sweep and silently wrong for re-running
    at a different sample size: a 40-hand run pointed at a file holding
    12-hand results exits in 0.0 seconds having done nothing, reporting
    "0 decks" and success. Separate files per hand count make that
    impossible, and a resume within one size still works.
    """
    import subprocess

    engine.require()
    engine.preflight(decks)
    solve_ms = solve_ms or engine.SOLVE_MS
    workers = workers or engine.DEFAULT_WORKERS
    out = (Path(out) if out else
           engine.ROOT / "results" / f"handtrap_matrix_{hands}h.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "nice", "-n", "19", str(engine.PYTHON), "tools/funnel_eval.py",
        *[str(Path(d).resolve()) for d in decks],
        "--targets", str(Path(targets_path).resolve()),
        "--hands", str(hands), "--solve-ms", str(solve_ms),
        "--fire-ms", str(fire_ms), "--workers", str(workers),
        "--no-filter", "--out", str(out),
    ]
    for code in sorted(HANDTRAPS):
        cmd += ["--fire", str(code)]
    before = len(out.read_text().splitlines()) if out.exists() else 0
    subprocess.run(cmd, env=engine.env(), cwd=str(engine.VENDOR),
                   timeout=timeout, check=True)
    records = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    if len(records) == before and len(records) < len(decks):
        raise RuntimeError(
            f"{out.name} gained no records: funnel_eval skipped every deck as "
            f"already present. Delete it to force a re-run.")
    return records


# --- targets for a deck the board library has never seen -----------------

TARGETS_FILE = engine.ROOT / "assets" / "derived_targets.json"


def load_targets(path=None):
    path = Path(path or TARGETS_FILE)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


# ygorl curates targets for its own ten test decks. Ours is the override;
# theirs is the fallback, so `gate snake_eye` keeps working without us
# re-deriving a target that upstream already hand-picked.
UPSTREAM_TARGETS = (engine.VENDOR / "tests" / "decks" / "solver_targets.json")


def target_for(deck_name, path=None):
    for src in (path or TARGETS_FILE, UPSTREAM_TARGETS):
        spec = load_targets(src).get(deck_name)
        if spec:
            targets = spec["targets"] if isinstance(spec, dict) else spec
            if targets:
                return targets[0]
    return None


def merged_targets_file(path=None):
    """One targets file covering both ours and ygorl's, for funnel_eval.

    funnel_eval takes a single --targets path and refuses any deck absent
    from it, so the two sources have to be combined before handing it
    over. Ours wins on conflict.
    """
    merged = dict(load_targets(UPSTREAM_TARGETS))
    merged.update(load_targets(path or TARGETS_FILE))
    merged.pop("_comment", None)
    out = engine.ROOT / "results" / "targets_merged.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"_comment": "generated: duelroom targets over ygorl's defaults",
         **merged}, indent=1))
    return out


def ensure_target(deck, hands=6, solve_ms=None, path=None, log=print):
    """Return this deck's solver target, deriving it first if needed.

    `derive_targets` reads the board library, so it only knows the meta
    decks that were goldfished into it. A freshly imported list -- which
    is every deck the user actually brings -- has no entry, and
    funnel_eval refuses a deck it has no target for. That surfaced as a
    bare CalledProcessError from `gate`, which reads like a crash rather
    than a missing prerequisite.

    So: solve the deck once with ygorl's automatic target planning, take
    the target it most often reached, and cache it alongside the others.
    Costs one solve_blocking pass the first time and nothing afterwards.
    """
    from . import engine as _engine

    deck = Path(deck)
    name = deck.stem
    path = Path(path or TARGETS_FILE)

    existing = target_for(name, path)
    if existing:
        return existing

    log(f"no target for {name} yet — solving {hands} hands to find one "
        f"(one-off, a few minutes)")
    out = _engine.ROOT / "results" / f"targets_{name}.jsonl"
    summary = _engine.ROOT / "results" / f"targets_{name}.json"
    _engine.solve_blocking(deck, hands=hands,
                           solve_ms=solve_ms or _engine.SOLVE_MS,
                           out=out, summary=summary)
    if not out.exists():
        log(f"  solver produced nothing for {name}")
        return None

    derived = derive_targets(out, per_deck=1)
    spec = derived.get(name)

    # Prefer what the deck actually ENDS ON over what the solver planned
    # towards. The planner aims at interruptions, which for some decks are
    # all main-deck cards, so it can never propose an extra-deck target no
    # matter how many hands are solved. The end boards in the same records
    # know better.
    if spec and not spec.get("_extra"):
        from .deck import Deck
        d = Deck.from_ydk(deck)
        board_target, share = extra_target_from_boards(out, d.extra)
        if board_target:
            log(f"  preferring {board_target} — on {share:.0%} of end boards "
                f"and in the extra deck")
            spec = {**spec, "targets": [board_target], "_extra": 1,
                    "_zone_rank": 3}
    if not spec:
        log(f"  {name} reached no target in {hands} hands — it may brick a lot, "
            f"or need a bigger budget (`ladder {name}`)")
        return None

    for _, reason in trivial_targets({name: spec}):
        log(f"  ! {name}: {reason}. Solve rate and handtrap survival for this")
        log(f"    deck will look better than they mean — there is little in")
        log(f"    that line to interrupt.")

    allt = load_targets(path)
    allt[name] = {"targets": spec["targets"], "fire": sorted(HANDTRAPS)}
    path.write_text(json.dumps(allt, indent=1))
    log(f"  derived {spec['targets'][0]} for {name}")
    return spec["targets"][0]


def extra_target_from_boards(jsonl_path, deck_extra, min_share=0.4):
    """Find an extra-deck monster this deck reliably ENDS ON.

    A second source of targets, independent of the solver's own planner.
    `solve_blocking` plans towards interruptions, which for a backrow
    deck are all main-deck cards, so its proposals can never yield an
    extra-deck target no matter how many hands are solved -- Blitzclique
    stayed on main-deck targets through sixteen.

    The end boards in those same records say what the deck actually
    makes. A monster appearing on at least `min_share` of solved end
    boards is something the engine produces rather than something you
    draw, which is exactly what a target should be.

    Returns (spec, share) or (None, 0.0).
    """
    extra = set(deck_extra)
    seen, solved = Counter(), 0
    for line in Path(jsonl_path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("status") != "solved":
            continue
        solved += 1
        for ln in (r.get("lines") or [])[:1]:
            board = ln.get("board") or {}
            for player in (board.get("players") or [])[:1]:
                for m in player.get("mzone") or []:
                    if m.get("code") in extra:
                        seen[m["code"]] += 1
    if not solved or not seen:
        return None, 0.0
    code, hits = seen.most_common(1)[0]
    share = hits / solved
    if share < min_share:
        return None, share
    return f"{code}@atk", share


def target_is_extra(conn, deck_path, target):
    """1 if the target lives in this deck's extra deck, else 0."""
    from .deck import Deck
    d = Deck.from_ydk(deck_path)
    d.canonicalise(conn)
    try:
        code = int(str(target).split("@")[0])
    except ValueError:
        return 1
    return 1 if code in set(d.extra) else 0


NIBIRU = 27204311

# window_anomalies() lived here. It flagged decks that "cleared 5 special
# summons but reported zero Nibiru windows", and its summon count came
# from Maxx "C" windows -- which are response windows, not summons. With
# that premise gone the contradiction it detected was not one: kewl-tune
# runs a 4-action line and may simply never make five special summons,
# so zero Nibiru windows is a plausible measurement rather than an
# artifact. Removed along with the ranking exclusion it justified.

# --- response windows -------------------------------------------

def response_windows(records):
    """How many times the opponent gets to respond during a line.

    NOT special summons. An earlier version of this claimed Maxx "C"
    windows were the summon count, on the reasoning that Maxx "C" draws
    on every special summon. That is what the card DOES once resolved;
    its activation has no condition at all, so the solver offers it at
    essentially every response window. Measured: the count correlates
    with line length at r = 0.937, and branded averages 1.0 actions
    against 6.8 "summons", which is not physically possible.

    What it does measure is useful on its own terms -- the interruption
    surface of a line. More windows is more chances for an opponent to
    interact, independent of what they hold.

    Returns {deck: {"mean", "max", "n"}}.
    """
    out = {}
    for rec in records:
        deck = (rec.get("summary") or {}).get("deck")
        if not deck:
            continue
        counts = []
        for h in rec.get("hands") or []:
            if h.get("status") != "solved":
                continue
            f = (h.get("fire") or {}).get(str(MAXX_C))
            if f and f.get("status") != "no_window":
                counts.append(f.get("windows") or 0)
        if not counts:
            continue
        out[deck] = {
            "mean": sum(counts) / len(counts),
            "max": max(counts),
            "n": len(counts),
        }
    return out


NIBIRU = 27204311
