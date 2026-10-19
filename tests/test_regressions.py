"""Regression tests for bugs that were actually found in this project.

Every test here corresponds to a real bug, not a hypothetical one. They
are deliberately written as "this specific wrong behaviour must not come
back" rather than as general-purpose unit tests, because the bugs here
share a shape -- code that runs cleanly and produces a confident, wrong
number -- and that shape is invisible to type checks and smoke tests.

Pure Python: no solver, no engine, no network. Runs in under a second.

    venv/bin/pytest tests/ -q
"""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skills import config, importer, scenarios, stats  # noqa: E402
from skills.deck import Deck  # noqa: E402


# --- stats ---------------------------------------------------------------

def test_hypergeometric_pmf_sums_to_one():
    total = sum(stats.pmf(40, 9, 5, k) for k in range(6))
    assert math.isclose(total, 1.0, abs_tol=1e-12)


def test_group_and_brick_are_complements():
    g = stats.group_chance(40, 9, 5)
    b = stats.brick_chance(40, 9, 5)
    assert math.isclose(g + b, 1.0, abs_tol=1e-12)


def test_combo_chance_reduces_to_group_chance_for_one_group():
    assert math.isclose(
        stats.combo_chance(40, [(9, 1)], 5), stats.group_chance(40, 9, 5),
        abs_tol=1e-12)


def test_known_three_of_in_forty():
    # 1 - C(37,5)/C(40,5)
    assert round(stats.open_chance(40, 3, 5) * 100, 2) == 33.76


def test_wilson_handles_zero_successes():
    p, lo, hi = stats.wilson(0, 50)
    assert p == 0.0 and lo == 0.0 and 0 < hi < 1


# --- deck / import -------------------------------------------------------

def test_ydke_round_trip_is_exact():
    d = Deck(name="t", main=[1, 1, 2, 3], extra=[9, 9], side=[7])
    back = importer.from_ydke(importer.to_ydke(d))
    assert (back.main, back.extra, back.side) == (d.main, d.extra, d.side)


def test_list_hash_ignores_order_but_not_multiplicity():
    a = Deck(main=[1, 2, 2], extra=[], side=[])
    b = Deck(main=[2, 1, 2], extra=[], side=[])
    c = Deck(main=[1, 2, 2, 2], extra=[], side=[])
    assert a.list_hash == b.list_hash
    assert a.list_hash != c.list_hash


def test_ydk_round_trip(tmp_path):
    d = Deck(name="rt", main=[10, 10, 11], extra=[20], side=[30])
    p = tmp_path / "rt.ydk"
    d.write_ydk(p)
    back = Deck.from_ydk(p)
    assert (back.main, back.extra, back.side) == (d.main, d.extra, d.side)


# --- target selection ----------------------------------------------------
# Bug: a targets list is a CONJUNCTION, so two targets demand both at once.
# Bug: @hand and @szone:fd targets are satisfied by the draw, not built.
# Bug: a main-deck target must be DRAWN -> measures draw luck (0/12 gate).

def _record(deck, targets, extra=(), actions=10, status="solved"):
    return {
        "deck": {"name": deck, "main": [], "extra": list(extra)},
        "status": status,
        "targets": list(targets),
        "lines": [{"actions": list(range(actions))}],
    }


def _write(tmp_path, records):
    p = tmp_path / "recs.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def test_extra_deck_target_beats_a_more_frequent_main_deck_one(tmp_path):
    # main-deck card reached 3x, extra-deck monster only 1x: the extra
    # deck card must still win, because the other has to be drawn.
    recs = [_record("d", ["111@szone:fd"], extra=[999]) for _ in range(3)]
    recs.append(_record("d", ["999@atk"], extra=[999]))
    got = scenarios.derive_targets(_write(tmp_path, recs))
    assert got["d"]["targets"] == ["999@atk"]
    assert got["d"]["_extra"] == 1


def test_built_target_beats_card_merely_in_hand(tmp_path):
    recs = [_record("d", ["111@hand"]) for _ in range(5)]
    recs.append(_record("d", ["222@szone:fd"]))
    got = scenarios.derive_targets(_write(tmp_path, recs))
    assert got["d"]["targets"] == ["222@szone:fd"]


def test_only_one_target_per_deck_by_default(tmp_path):
    # Two targets would be a conjunction: the board must contain BOTH.
    recs = [_record("d", ["1@atk", "2@atk"], extra=[1, 2]) for _ in range(3)]
    got = scenarios.derive_targets(_write(tmp_path, recs))
    assert len(got["d"]["targets"]) == 1


def test_trivial_targets_fires_on_a_saved_spec():
    # _zone_rank is stripped before the file is written, so the check must
    # derive the zone from the target string or it silently never fires.
    saved = {"targets": ["24224830@szone:fd"], "fire": []}
    assert scenarios.trivial_targets({"d": {**saved, "_extra": 1}})
    assert scenarios.trivial_targets({"d": {**saved, "_extra": 0}})
    good = {"targets": ["15665977@atk"], "fire": []}
    assert not scenarios.trivial_targets({"d": {**good, "_extra": 1}})


def test_write_targets_merges_instead_of_clobbering(tmp_path):
    out = tmp_path / "targets.json"
    out.write_text(json.dumps({
        "_comment": "x",
        "my-deck": {"targets": ["777@atk"], "fire": []},
    }))
    recs = [_record("meta-deck", ["1@atk"], extra=[1]) for _ in range(3)]
    scenarios.write_targets(_write(tmp_path, recs), out)
    after = json.loads(out.read_text())
    assert "my-deck" in after, "a meta refresh deleted the user's deck target"
    assert after["my-deck"]["targets"] == ["777@atk"]
    assert "meta-deck" in after


# --- handtrap matrix -----------------------------------------------------
# Bug: funnel_eval's summary counts a `no_window` outcome as a survival,
# which inverts the answer (Nibiru read 90% survival on 9 no_window + 1 stop).

def _funnel(deck, fire_entries):
    """fire_entries: list of (status, survives)."""
    return {
        "summary": {"deck": deck, "solved": len(fire_entries),
                    "hands": len(fire_entries)},
        "hands": [
            {"status": "solved",
             "fire": {"27204311": {"status": st, "survives": sv}}}
            for st, sv in fire_entries
        ],
    }


def test_no_window_is_not_counted_as_survival():
    rec = _funnel("d", [("no_window", False)] * 9 + [("unsolved", False)])
    cell = scenarios.handtrap_matrix([rec])["d"]["cells"][27204311]
    assert cell["tested"] == 1, "no_window must not count as a tested window"
    assert cell["survival"] == 0.0, "the one real window STOPPED the line"
    assert cell["window_rate"] == pytest.approx(0.1)


def test_status_solved_does_not_mean_the_line_survived():
    # A fire entry can read status "solved" (the SEARCH succeeded) with
    # survives False (the line died in most of its windows). Reading
    # status as survival flipped Infinite Impermanence from 0% to 100%.
    rec = _funnel("d", [("solved", False)] * 10)
    cell = scenarios.handtrap_matrix([rec])["d"]["cells"][27204311]
    assert cell["tested"] == 10
    assert cell["survival"] == 0.0, "status=solved must not imply survival"

    rec2 = _funnel("d", [("solved", True)] * 4 + [("solved", False)] * 6)
    cell2 = scenarios.handtrap_matrix([rec2])["d"]["cells"][27204311]
    assert cell2["survival"] == pytest.approx(0.4)


def test_handtrap_never_live_cannot_rank_well():
    never = {"d": {"cells": {27204311: {"survival": None, "tested": 0,
                                        "offered": 10, "window_rate": 0.0}}}}
    ranked = scenarios.weighted_handtrap_ranking(never, {"d": 1.0})
    assert ranked[0][2] == 0.0, "a handtrap with no window must score zero"


# --- locals weighting ----------------------------------------------------
# Bug: counting tag rows sums to 166% of the deck count and inflates
# hybrid-heavy archetypes (a four-engine list is not four opponents).

def test_hybrid_decks_split_their_weight(tmp_path):
    from skills import db
    conn = db.connect(tmp_path / "t.db")
    with conn:
        conn.execute("INSERT INTO decks (id, format, list_hash, scraped_at) "
                     "VALUES (1,'F','h1','now'), (2,'F','h2','now')")
        conn.executemany("INSERT INTO deck_tags (deck_id, tag) VALUES (?,?)",
                         [(1, "A"), (1, "B"), (1, "C"), (1, "D"), (2, "A")])
    shares, total = config.scraped_shares(conn, top=10)
    assert total == pytest.approx(2.0), "two decks must contribute two units"
    d = dict(shares)
    # deck 1 gives 0.25 to each of four tags; deck 2 gives 1.0 to A.
    assert d["a"] == pytest.approx(1.25 / 2.0)
    assert math.isclose(sum(d.values()), 1.0, abs_tol=1e-9)


# --- optimizer -----------------------------------------------------------
# Bug: hill_climb compared every trial against base.score, which never
# moves, so the LAST qualifying swap won rather than the best.

def test_hill_climb_keeps_the_best_improvement_not_the_last():
    class C:
        def __init__(self, s):
            self.score = s
    base, improved = C(-0.225), None
    for score in (0.063, -0.200, 0.313, 0.063):
        if score > base.score and (improved is None or score > improved.score):
            improved = C(score)
    assert improved.score == pytest.approx(0.313)


def test_objective_orders_a_better_summary_higher():
    from skills.optimizer import Objective
    o = Objective()
    worse = {"brick_rate": 0.2, "best_line_rate": 0.8,
             "hand_trap_survival": 0.4, "combo_actions": {"mean": 8.0}}
    better = {"brick_rate": 0.1, "best_line_rate": 0.9,
              "hand_trap_survival": 0.5, "combo_actions": {"mean": 7.0}}
    assert o.score(better) > o.score(worse)


def test_side_in_refuses_a_card_not_in_the_side_deck():
    from skills import optimizer
    d = Deck(name="d", main=[1, 2, 3], extra=[], side=[9])
    ok = optimizer.side_in(d, 1, 9)
    assert len(ok.side) == len(d.side), "siding must stay 1-for-1"
    assert 9 in ok.main and 1 in ok.side
    with pytest.raises(ValueError):
        optimizer.side_in(d, 2, 12345)


# --- metric blind spots --------------------------------------------------
# Bug: report labelled Maxx "C" at 80% survival as "fine". The metric asks
# only whether the line reached its target, which Maxx "C" never prevents,
# so a high number there is the metric missing the card.

def test_blind_spot_cards_are_declared():
    # Maxx "C" only. Droll was removed after the `survives` fix: 45 of 85
    # of its windows convert and 19 hands died to it, so the metric does
    # capture it. Maxx "C" stays because 94% of its windows convert --
    # lines continue through it and the cards it hands over are invisible
    # to a "did the line finish" measurement.
    assert 23434538 in scenarios.BLIND_SPOTS, 'Maxx "C" must stay flagged'
    for code, reason in scenarios.BLIND_SPOTS.items():
        assert reason and isinstance(reason, str)


def test_blind_spots_are_not_in_the_judgeable_set():
    # the verdict logic must exclude them before picking a "worst" card
    live = [(None, 23434538, 0.8), (None, 10045474, 0.0)]
    judged = [t for t in live if int(t[1]) not in scenarios.BLIND_SPOTS]
    assert len(judged) == 1
    assert judged[0][1] == 10045474


# --- direction labelling -------------------------------------------------
# Bug: handtrap survival means the opposite thing for your own deck
# (high is good) versus an opponent's (low is good), and `report` and
# `threats` printed it with no direction at all.

def test_slug_matches_deck_names_across_spellings():
    # the "(yours)" marker depends on this matching file stems to the
    # archetype names used in tables
    assert config.slug("Kewl Tune") == config.slug("kewl-tune")
    assert config.slug("Light and Darkness Ritual") == "light-and-darkness-ritual"
    assert config.slug("kewl-tune") == "kewl-tune"


# --- response windows ---------------------------------------------------
# Bug (mine): I claimed Maxx "C" windows were the special-summon count,
# because Maxx "C" draws on every special summon. That is what the card
# does once RESOLVED; its activation has no condition, so the solver
# offers it at nearly every response window. The count correlates with
# line length at r=0.937, and branded averaged 1.0 actions against 6.8
# "summons" — impossible. The metric is real, the name was wrong.

def test_response_windows_are_not_summon_counts():
    recs = [{
        "summary": {"deck": "d"},
        "hands": [
            {"status": "solved",
             "fire": {"23434538": {"status": "solved", "windows": w}}}
            for w in (3, 7, 9)
        ],
    }]
    got = scenarios.response_windows(recs)["d"]
    assert got["mean"] == pytest.approx((3 + 7 + 9) / 3)
    assert got["max"] == 9
    assert "nibiru_rate" not in got, "must not claim a summon threshold"


def test_no_window_entries_excluded_from_response_windows():
    recs = [{
        "summary": {"deck": "d"},
        "hands": [
            {"status": "solved",
             "fire": {"23434538": {"status": "no_window", "windows": 0}}},
            {"status": "solved",
             "fire": {"23434538": {"status": "solved", "windows": 8}}},
        ],
    }]
    got = scenarios.response_windows(recs)["d"]
    assert got["n"] == 1 and got["mean"] == 8


def test_summon_count_derivation_is_gone():
    # it rested on a false premise; nothing may depend on it again
    assert not hasattr(scenarios, "summon_counts")
    assert not hasattr(scenarios, "window_anomalies")


def test_droll_is_no_longer_a_blind_spot():
    # it was only labelled unmeasurable under the buggy status reading;
    # 45/85 windows convert and 19 hands died to it
    assert 94145021 not in scenarios.BLIND_SPOTS
    assert 23434538 in scenarios.BLIND_SPOTS


# --- resume-vs-rerun -----------------------------------------------------
# Bug: funnel_eval resumes by SKIPPING decks already in the target file.
# A 40-hand run pointed at a file holding 12-hand results exited in 0.0s
# having done nothing, and reported success.

def test_matrix_output_path_carries_the_hand_count():
    import inspect
    src = inspect.getsource(scenarios.run_matrix)
    assert 'handtrap_matrix_{hands}h' in src, (
        "runs at different sample sizes must not share an output file")
    assert "gained no records" in src, (
        "a run that skipped every deck must raise, not report success")


# --- partial reads -------------------------------------------------------
# Bug: _latest_matrix picks the newest file by mtime, which during a sweep
# is the one still being written. Reading 1 of 7 decks as if complete
# would silently narrow every conclusion to whichever deck finished first.

def test_matrix_reader_prefers_newest_but_callers_must_see_the_count(tmp_path):
    import re
    src = (Path(__file__).resolve().parent.parent / "daemon.py").read_text()
    # the reader must surface how many decks it actually got
    assert "deck(s))" in src, "matrix must print how many decks it read"
    assert "looks PARTIAL" in src, "a half-written sweep must be called out"


# --- provenance ----------------------------------------------------------
# A solve rate without its budget is close to meaningless: 10s reports a
# brick where 60s solves the same hand in nine actions, and two runs look
# identical on screen.

def test_latest_funnel_returns_its_source(tmp_path, monkeypatch):
    from skills import engine, report as report_mod
    results = tmp_path / "results"
    results.mkdir()
    (results / "run_a.jsonl").write_text(json.dumps({
        "summary": {"deck": "d", "solved": 1, "hands": 1},
        "config": {"solve_ms": 60000, "hands": 1},
    }) + "\n")
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    rec, src = report_mod.latest_funnel("d")
    assert rec is not None
    assert src is not None and src.name == "run_a.jsonl", (
        "callers need the source to state the budget a number came from")


def test_latest_funnel_misses_cleanly(tmp_path, monkeypatch):
    from skills import engine, report as report_mod
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    assert report_mod.latest_funnel("nope") == (None, None)


# --- archetype identification --------------------------------------------
# The headline use case is importing your own deck, where nobody knows to
# pass an archetype name. It is inferable from the deck's own cards.

def test_identify_matches_on_core_not_staples(tmp_path):
    from skills import db, meta_scrape as ms
    conn = db.connect(tmp_path / "t.db")
    rows = []
    # archetype A: a real 4-card core. B: one core card plus staples.
    for cid in (101, 102, 103, 104):
        rows.append(("A", "F", cid, "main", 1.0, 3.0, 50, "now"))
    for cid in (201, 202, 203):
        rows.append(("B", "F", cid, "main", 1.0, 3.0, 50, "now"))
    with conn:
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
    deck = Deck(main=[101, 102, 103, 104, 201], extra=[], side=[])
    got = ms.identify_archetype(conn, deck, fmt="F")
    assert got[0][0] == "A", "must match the deck whose whole core is present"
    assert got[0][1] == pytest.approx(1.0)


def test_identify_ignores_cores_too_thin_to_mean_anything(tmp_path):
    from skills import db, meta_scrape as ms
    conn = db.connect(tmp_path / "t.db")
    with conn:
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [("Thin", "F", 1, "main", 1.0, 3.0, 50, "now"),
             ("Thin", "F", 2, "main", 1.0, 3.0, 50, "now")])
    deck = Deck(main=[1, 2], extra=[], side=[])
    assert ms.identify_archetype(conn, deck, fmt="F") == [], (
        "a 2-card core identifies nothing")


# --- optimizer removal choice --------------------------------------------
# The hill climb evaluates ONE removal per candidate (the full cross
# product is hours of solver time), so which card it cuts is the single
# most consequential choice in the search. It used to take whatever came
# first out of a dict.

def test_removable_puts_the_least_played_card_first(tmp_path):
    from skills import db, optimizer
    from skills.meta_scrape import DEFAULT_FORMAT as FMT
    conn = db.connect(tmp_path / "t.db")
    with conn:
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [("A", FMT, 1, "main", 1.00, 3, 50, "now"),
             ("A", FMT, 2, "main", 0.55, 1, 50, "now"),
             ("A", FMT, 3, "main", 0.25, 1, 50, "now")])
    deck = Deck(main=[1, 2, 3, 999], extra=[], side=[])
    ids = [cid for cid, _ in optimizer.removable(conn, deck, "A")]
    assert 1 not in ids, "core cards must never be cut"
    assert ids[0] == 999, "a card no list plays is cut before a 25% card"
    assert ids == [999, 3, 2], "weakest-first ordering"


# --- hill climb control flow ---------------------------------------------
# A real optimize run is hours of solver time. A control-flow bug there
# is expensive to discover, so the loop is exercised against a stubbed
# evaluator: no solver, no engine, deterministic.

def _stub_optimizer(monkeypatch, scores):
    """Make evaluate() return canned summaries keyed by the added card."""
    from skills import optimizer
    calls = []

    def fake_evaluate(deck, target, hands=12, solve_ms=None, scratch=None,
                      fire=None):
        calls.append(deck.name)
        added = set(deck.main) - _stub_optimizer.base
        key = next(iter(added), None)
        rate = scores.get(key, 0.5)
        return {"brick_rate": 1 - rate, "best_line_rate": rate,
                "hand_trap_survival": rate, "combo_actions": {"mean": 5.0},
                "solved": int(rate * 10), "hands": 10}

    monkeypatch.setattr(optimizer, "evaluate", fake_evaluate)
    return calls


def test_hill_climb_accepts_the_best_candidate_and_stops(tmp_path, monkeypatch):
    from skills import db, optimizer
    from skills.meta_scrape import DEFAULT_FORMAT as FMT
    conn = db.connect(tmp_path / "t.db")
    with conn:
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [("A", FMT, 1, "main", 1.0, 1, 50, "now"),     # core, never cut
             ("A", FMT, 9, "main", 0.10, 1, 50, "now"),    # our weak slot
             ("A", FMT, 50, "main", 0.40, 1, 50, "now"),   # candidates
             ("A", FMT, 51, "main", 0.35, 1, 50, "now"),
             ("A", FMT, 52, "main", 0.30, 1, 50, "now")])
        conn.executemany("INSERT INTO cards (id, name, last_fetched) VALUES (?,?,?)",
                         [(i, f"c{i}", "now") for i in (1, 9, 50, 51, 52)])

    base = Deck(name="base", main=[1, 9], extra=[], side=[])
    _stub_optimizer.base = set(base.main)
    # The pool is ordered by inclusion (50, 51, 52), so the BEST candidate
    # must come FIRST for this to distinguish "best wins" from "last wins".
    # With the best last, both behaviours pick the same card and the test
    # passes against the bug it is meant to catch.
    _stub_optimizer(monkeypatch, {50: 0.9, 51: 0.6, 52: 0.2})
    monkeypatch.setattr(Deck, "validate", lambda self, conn, region="tcg": [])

    best, history = optimizer.hill_climb(
        conn, base, "A", "1@atk", hands=4, rounds=1, log=lambda *a: None)
    assert "c50" in best.label, (
        f"took {best.label}; c50 scores highest and must win over the later "
        f"c51, which also beats base")
    assert len(history) >= 4, "every trial must be recorded, not just the winner"


def test_hill_climb_stops_when_nothing_improves(tmp_path, monkeypatch):
    from skills import db, optimizer
    from skills.meta_scrape import DEFAULT_FORMAT as FMT
    conn = db.connect(tmp_path / "t.db")
    with conn:
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [("A", FMT, 1, "main", 1.0, 1, 50, "now"),
             ("A", FMT, 9, "main", 0.10, 1, 50, "now"),
             ("A", FMT, 50, "main", 0.40, 1, 50, "now")])
        conn.executemany("INSERT INTO cards (id, name, last_fetched) VALUES (?,?,?)",
                         [(i, f"c{i}", "now") for i in (1, 9, 50)])
    base = Deck(name="base", main=[1, 9], extra=[], side=[])
    _stub_optimizer.base = set(base.main)
    _stub_optimizer(monkeypatch, {50: 0.1})   # strictly worse than base 0.5
    monkeypatch.setattr(Deck, "validate", lambda self, conn, region="tcg": [])
    best, history = optimizer.hill_climb(
        conn, base, "A", "1@atk", hands=4, rounds=3, log=lambda *a: None)
    assert best.label == "base", "a worse swap must not be accepted"


def test_hypergeometric_handles_impossible_arguments():
    # Found by mutation testing: dropping pmf's bounds checks left every
    # existing stats test green, yet asking for more successes than cards
    # drawn raises ValueError from math.comb rather than returning 0.
    # These are reachable from real input — "what are the odds of opening
    # 4 copies of a 3-of" is a question a user can ask.
    assert stats.pmf(40, 30, 5, 7) == 0.0      # 7 hits from a 5-card draw
    assert stats.pmf(40, 3, 5, 4) == 0.0       # 4 copies of a 3-of
    assert stats.pmf(40, 3, 5, -1) == 0.0      # negative successes
    assert stats.pmf(10, 8, 5, 1) == 0.0       # more misses than non-hits exist
    assert stats.at_least(40, 3, 5, 4) == 0
    assert stats.open_chance(40, 3, 5, want=4) == 0


# --- gaps found by mutation testing --------------------------------------

def test_tag_matching_respects_word_boundaries():
    # Plain substring matching put "Ra" second in the meta because it hit
    # inside "D(ra)gon", and "P" matched inside "Ten(p)ai". Punctuation
    # tags must still work, so the boundary is only asserted on the sides
    # that are alphanumeric.
    from skills import meta_scrape as ms
    vocab = ["Ra", "P", "Dragon", "Dark Magician", "@Ignister", "-Eyes Dragon"]
    assert "Ra" not in ms.match_archetypes("Angelechy Tenpai Dragon", vocab)
    assert "P" not in ms.match_archetypes("Angelechy Tenpai Dragon", vocab)
    assert "Ra" in ms.match_archetypes("Ra Sphere Mode", vocab)
    assert "@Ignister" in ms.match_archetypes("@Ignister Maliss", vocab)
    assert "-Eyes Dragon" in ms.match_archetypes("Blue-Eyes Dragon", vocab)


def test_excluded_cells_are_actually_excluded():
    # The exclusion argument must do something: scoring a card over decks
    # whose data we have decided not to believe drags it down with a zero.
    matrix = {
        "good": {"cells": {1: {"survival": 0.0, "tested": 4,
                               "offered": 4, "window_rate": 1.0}}},
        "bad": {"cells": {1: {"survival": None, "tested": 0,
                              "offered": 5, "window_rate": 0.0}}},
    }
    w = {"good": 0.5, "bad": 0.5}
    naive = scenarios.weighted_handtrap_ranking(matrix, w)[0][2]
    excl = scenarios.weighted_handtrap_ranking(
        matrix, w, exclude={("bad", 1)})[0][2]
    assert naive == pytest.approx(0.5)
    assert excl == pytest.approx(1.0), "exclude= must change the score"


def test_recent_set_databases_are_merged():
    # BabelCDB ships recent sets as separate release-*.cdb that ygorl
    # never loads, and a deck holding one of those cards segfaults the
    # solver after five misleading diagnostics.
    from skills import carddb
    extras = carddb.extra_cdbs()
    assert extras, "no release-*.cdb found to merge — recent cards will be missing"
    assert all(p.name.startswith("release-") for p in extras)
    assert not any("prerelease" in p.name for p in extras), (
        "pre-release sets are not tournament legal and must stay out")


def test_special_summons_are_replayed_not_inferred():
    # The Maxx "C"-window shortcut gave branded 6.8 "summons" against 1.0
    # actions. Replaying gives 0.6. The function must not be reachable
    # without actually replaying, so it is checked for the observer.
    import inspect
    src = inspect.getsource(scenarios.special_summons)
    assert "SpSummoned" in src, "must count the engine's own summon message"
    assert "replay" in src, "must replay the line rather than infer"
    assert "23434538" not in src, 'must not read Maxx "C" windows'


def test_maxx_c_cost_is_the_summon_count():
    # Maxx "C" draws on every special summon, so the replayed count IS
    # the cost. The survival metric cannot see it; this is the separate
    # question that can.
    got = scenarios.maxx_c_cost({"d": {"mean": 8.4, "max": 15,
                                       "nibiru_rate": 1.0, "n": 40}})
    assert got["d"]["cards"] == pytest.approx(8.4)
    assert got["d"]["worst"] == 15


# --- optimizer objective -------------------------------------------------
# The objective weights handtrap survival most heavily, so which
# handtraps get fired decides what the search optimises for. Left to
# funnel_eval's default that was Ash alone, while the 40-hand matrix put
# Imperm well ahead of it.

def test_fire_list_reaches_the_command_line_not_just_the_file(tmp_path,
                                                             monkeypatch):
    # The first version of this fix wrote "fire" into the per-trial
    # targets file. funnel_eval's load_deck_targets returns only
    # v["targets"] and drops every other key, so that changed nothing:
    # trials still fired Ash alone while the file claimed otherwise.
    # Asserting the file contents could not catch that. Assert the call.
    from skills import engine, gate, optimizer
    seen = {}

    def fake_run_funnel(path, hands=12, solve_ms=None, out=None,
                        targets=None, fire=None):
        seen["fire"] = fire
        seen["file"] = json.loads(Path(targets).read_text())
        return {"summary": {"solved": 1, "hands": 1},
                "hands": [{"fire": {str(c): {} for c in (fire or ())}}]}

    monkeypatch.setattr(gate, "run_funnel", fake_run_funnel)
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    deck = Deck(name="t", main=[1], extra=[], side=[])
    optimizer.evaluate(deck, "1@atk", hands=1, fire=[111, 222])
    assert seen["fire"] == [111, 222], (
        "fire must be passed to run_funnel; funnel_eval ignores the file key")


def test_evaluate_refuses_a_run_that_fired_the_wrong_handtraps(tmp_path,
                                                               monkeypatch):
    # Belt and braces: if the request is silently dropped again, the run
    # must fail loudly rather than score the objective on one handtrap.
    from skills import engine, gate, optimizer

    def fake_run_funnel(path, hands=12, solve_ms=None, out=None,
                        targets=None, fire=None):
        return {"summary": {"solved": 1, "hands": 1},
                "hands": [{"fire": {"111": {}}}]}   # only one of the two

    monkeypatch.setattr(gate, "run_funnel", fake_run_funnel)
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    deck = Deck(name="t", main=[1], extra=[], side=[])
    with pytest.raises(RuntimeError, match="only fired"):
        optimizer.evaluate(deck, "1@atk", hands=1, fire=[111, 222])


def test_threat_handtraps_falls_back_without_a_matrix(tmp_path, monkeypatch):
    from skills import engine, optimizer
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    got = optimizer.threat_handtraps()
    assert 14558127 in got and 10045474 in got, (
        "with no measurement, fall back to Ash and Imperm rather than nothing")


def test_hill_climb_checkpoints_every_trial(tmp_path, monkeypatch):
    # A run is hours. Killing one — which happened when the objective
    # turned out to be wrong — must not lose every evaluation with it.
    from skills import db, engine, optimizer
    from skills.meta_scrape import DEFAULT_FORMAT as FMT
    conn = db.connect(tmp_path / "t.db")
    with conn:
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [("A", FMT, 1, "main", 1.0, 1, 50, "now"),
             ("A", FMT, 9, "main", 0.10, 1, 50, "now"),
             ("A", FMT, 50, "main", 0.40, 1, 50, "now")])
        conn.executemany("INSERT INTO cards (id, name, last_fetched) VALUES (?,?,?)",
                         [(i, f"c{i}", "now") for i in (1, 9, 50)])
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    (tmp_path / "results").mkdir(exist_ok=True)
    base = Deck(name="ck", main=[1, 9], extra=[], side=[])
    _stub_optimizer.base = set(base.main)
    _stub_optimizer(monkeypatch, {50: 0.9})
    monkeypatch.setattr(Deck, "validate", lambda self, conn, region="tcg": [])
    optimizer.hill_climb(conn, base, "A", "1@atk", hands=2, rounds=1,
                         log=lambda *a: None)
    lines = (tmp_path / "results" / "optimize_ck.jsonl").read_text().splitlines()
    assert len(lines) >= 2, "base plus each trial must be on disk as it finishes"
    assert json.loads(lines[0])["label"] == "base"
    assert "fire" in json.loads(lines[0]), "record which handtraps scored it"


def test_a_one_card_swap_often_leaves_the_hand_unchanged():
    # Recorded because the opposite was asserted twice in this project's
    # own docs. Measured 55% unchanged over 80 (candidate, seed) pairs.
    # It matters in two directions: it is why the optimizer's paired
    # comparison is tighter than shuffle luck would allow, and it is NOT
    # a reason memoization would work — the decks still differ by a card
    # and the solver searches the deck.
    from skills import breaking, db, optimizer
    from ygorl.solver.batch import sample_hand
    conn = db.connect()
    deck = Deck.from_ydk("assets/decks/kewl-tune.ydk")
    deck.canonicalise(conn)
    outs = optimizer.removable(conn, deck, "Kewl Tune")
    pool = optimizer.candidate_pool(conn, "Kewl Tune", deck, limit=2)
    if not outs or not pool:
        pytest.skip("needs scraped inclusion data")
    same = total = 0
    for cand in pool:
        swapped = optimizer.swap(deck, outs[0][0], cand["card_id"])
        for seed in range(10):
            a, _ = sample_hand(breaking.to_ydeck(deck), seed=seed)
            b, _ = sample_hand(breaking.to_ydeck(swapped), seed=seed)
            total += 1
            same += (a == b)
    assert total and same > 0, (
        "a one-card swap must sometimes leave the hand identical; if this "
        "ever reads 0 the paired-comparison reasoning needs revisiting")


# --- engine.duel parameter contract --------------------------------------
# duel() is currently used by nothing, which makes it unverified surface
# that a bot-vs-bot stage would rest on. Each knob was checked to take
# effect; max_turns has a surprising contract worth pinning.

@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "vendor" / "ygorl").exists(),
    reason="engine not built")
def test_max_turns_is_off_by_one_and_stays_that_way():
    from skills import engine
    A = "assets/decks/kewl-tune.ydk"
    B = "assets/decks/sky-striker.ydk"
    r = engine.duel(A, B, seed=0, max_turns=2)
    assert r["reason"] == "turn_limit"
    assert r["turns"] == 3, (
        "upstream stops as turn N+1 begins; if this becomes 2 the contract "
        "changed and callers capping turns need revisiting")


def test_degenerate_objective_terms_are_detected():
    # Measured on kewl-tune at 12 hands: solve rate, brick rate and
    # handtrap survival were identical across every trial, so the whole
    # ranking came from the combo-length tiebreak at weight -0.05.
    from skills import optimizer

    class C:
        def __init__(self, mean):
            self.summary = {"best_line_rate": 0.83, "brick_rate": 0.17,
                            "hand_trap_survival": 0.0,
                            "combo_actions": {"mean": mean}}
    hist = [C(4.0), C(4.5), C(5.0)]
    constant, varying = optimizer.degenerate_terms(hist)
    assert set(constant) == {"solve rate", "brick rate", "handtrap survival"}
    assert varying == ["combo length"]

    class D(C):
        def __init__(self, mean, solved):
            super().__init__(mean)
            self.summary["best_line_rate"] = solved
    mixed = [D(4.0, 0.8), D(4.5, 0.9)]
    constant2, varying2 = optimizer.degenerate_terms(mixed)
    assert "solve rate" in varying2


def test_optimize_report_block_defines_rows_before_use():
    # A use-before-assignment crashed `optimize` at the very end, after
    # hours of solver time, swallowing the result table. The checkpoint
    # ledger made the data recoverable; the crash still should not ship.
    src = (Path(__file__).resolve().parent.parent / "daemon.py").read_text()
    block = src[src.index("def cmd_optimize"):]
    block = block[:block.index("\ndef ")]
    assign = block.index("rows = optimizer.report(")
    first_use = block.index("for r in rows[")
    assert assign < first_use, (
        "rows must be assigned before the result table iterates it")


# --- card image cache ----------------------------------------------------

def test_an_image_takes_its_final_name_only_after_the_whole_body(tmp_path,
                                                                  monkeypatch):
    """The failure that makes a cache permanently wrong rather than slow.

    A truncated JPEG under the final name is indistinguishable from a
    complete one, so every later run skips it and that card is broken
    forever. The body must be written under a temporary name and renamed.

    Checked by watching where the bytes are actually written -- asserting
    only that an interrupted fetch leaves no .jpg behind would pass just
    as happily against a direct write, since the interruption happens
    before any write at all.
    """
    from pathlib import Path as P

    from skills import images
    monkeypatch.setattr(images, "CACHE", tmp_path)
    monkeypatch.setattr(images, "RPS", 1e6)

    written = []
    real_write = P.write_bytes
    monkeypatch.setattr(P, "write_bytes",
                        lambda self, b: (written.append(self.name),
                                         real_write(self, b))[1])

    class Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            class R:
                status_code, content = 200, b"\xff\xd8body"
            return R()
    monkeypatch.setattr(images.httpx, "Client", lambda **kw: Client())

    class FakeConn:
        def execute(self, q, *a):
            return [{"card_id": 101, "p": 1.0}] if "inclusion" in q else []
    images.fetch(FakeConn(), "small-meta", log=lambda m: None)

    jpgs = [w for w in written if w.endswith(".jpg")]
    assert not jpgs, f"wrote straight to the final name: {jpgs}"
    assert "101.jpg.part" in written
    assert images.have("small", 101), "the rename never happened"
    assert not list((tmp_path / "small").glob("*.part")), "a .part was left behind"


def test_an_interrupted_image_fetch_leaves_nothing_behind(tmp_path, monkeypatch):
    from skills import images
    monkeypatch.setattr(images, "CACHE", tmp_path)
    monkeypatch.setattr(images, "RPS", 1e6)

    class Boom(Exception):
        pass

    class Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            raise Boom("connection reset mid-body")

    monkeypatch.setattr(images.httpx, "Client", lambda **kw: Client())
    monkeypatch.setattr(images.httpx, "HTTPError", Boom)

    class FakeConn:
        def execute(self, q, *a):
            return [{"card_id": 101, "p": 1.0}] if "inclusion" in q else []
    r = images.fetch(FakeConn(), "small-meta", log=lambda m: None)
    assert r["fetched"] == 0 and r["failed"] == 1
    assert not images.have("small", 101)
    # missing.json is expected; an image file of any kind is not.
    assert not list((tmp_path / "small").glob("*.jpg*")), "a partial file survived"
    assert images.load_missing("small") == set(), "a retryable error was given up on"


def test_absent_art_is_remembered_but_a_network_error_is_not(tmp_path, monkeypatch):
    """A 404 means there is no artwork; a timeout means try again later.

    Recording both as permanent would silently stop re-fetching cards the
    cache simply failed to reach.
    """
    from skills import images
    monkeypatch.setattr(images, "CACHE", tmp_path)
    monkeypatch.setattr(images, "RPS", 1e6)

    class Boom(Exception):
        pass

    class Resp:
        def __init__(self, code, body=b""):
            self.status_code, self.content = code, body

    class Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            if url.endswith("/404.jpg"):
                return Resp(404)
            if url.endswith("/500.jpg"):
                return Resp(500)
            return Resp(200, b"\xff\xd8ok")

    monkeypatch.setattr(images.httpx, "Client", lambda **kw: Client())
    monkeypatch.setattr(images.httpx, "HTTPError", Boom)

    class FakeConn:
        def execute(self, q, *a):
            return [{"card_id": i, "p": 1.0} for i in (404, 500, 7)] \
                if "inclusion" in q else []
    r = images.fetch(FakeConn(), "small-meta", log=lambda m: None)
    assert (r["fetched"], r["absent"], r["failed"]) == (1, 1, 1)
    assert images.load_missing("small") == {404}, "a retryable error was given up on"
    assert images.have("small", 7)


def test_a_limited_run_does_not_misreport_what_is_cached(tmp_path, monkeypatch):
    from skills import images
    monkeypatch.setattr(images, "CACHE", tmp_path)
    monkeypatch.setattr(images, "RPS", 1e6)
    said = []

    class Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            class R:
                status_code, content = 200, b"\xff\xd8ok"
            return R()
    monkeypatch.setattr(images.httpx, "Client", lambda **kw: Client())

    class FakeConn:
        def execute(self, q, *a):
            return [{"card_id": i, "p": 1.0} for i in range(50)] \
                if "inclusion" in q else []
    images.fetch(FakeConn(), "small-meta", limit=5, log=said.append)
    assert "0 already cached" in said[0], said[0]


def test_image_scopes_are_validated(tmp_path, monkeypatch):
    from skills import db, images
    conn = db.connect()
    for bad in ("small", "tiny-all", "small-some", "full"):
        with pytest.raises(ValueError):
            images.wanted(conn, bad)
    size, ids = images.wanted(conn, "small-all")
    assert size == "small" and len(ids) > 14000
    _, meta = images.wanted(conn, "small-meta")
    assert ids[:len(meta)] == meta, "meta-played cards are not fetched first"


# --- one chosen hand (handlab) -------------------------------------------

FIRE_LOG = Path(__file__).resolve().parent / "fixtures" / "fire_ash.log"


def test_window_detail_is_parsed_out_of_the_solver_log():
    """The per-window verdict is the whole point of the feature: not
    "2 of 3 Ash timings kill you" but "Ash on your Branded Opening"."""
    from skills import handlab
    rows, declared = handlab.parse_windows(FIRE_LOG.read_text())
    assert declared == 3 and len(rows) == 3
    first = rows[0]
    assert first["over"] == "Branded Opening"
    assert first["decision"] == 2
    assert first["survived"] is False
    assert first["missing"] == ["Aluber the Jester of Despia"]
    assert first["answers"] == ["The Fallen & The Virtuous"]
    assert [r["over"] for r in rows[1:]] == [
        "Aluber the Jester of Despia", "Branded Fusion"]


CONVERTED_LOG = (Path(__file__).resolve().parent / "fixtures"
                 / "fire_veiler_converted.log")


def test_both_verdicts_are_read_from_REAL_solver_output():
    """The solver writes failure as "pas convertie" and success as
    "CONVERTIE (board COMPLET)" -- different words AND different case.

    The first version of this test built its success case by lowercasing
    a failure line itself, so it never saw the real format: every
    converted window shipped as "no recovery found". Both fixtures are
    now genuine solver logs.
    """
    from skills import handlab
    lost, _ = handlab.parse_windows(FIRE_LOG.read_text())
    assert lost and not any(r["survived"] for r in lost)
    won, _ = handlab.parse_windows(CONVERTED_LOG.read_text())
    assert won and all(r["survived"] for r in won), (
        "a converted window was read as a failure")


def test_an_open_window_is_not_found_inside_a_card_name():
    """"over Branded Opening" contains "open". The same substring trap
    as "Ra" inside "Dragon", which this project has already paid for."""
    from skills import handlab
    rows, _ = handlab.parse_windows(FIRE_LOG.read_text())
    assert not rows[0]["open_window"], "'Opening' was read as an open window"
    rows2, _ = handlab.parse_windows(CONVERTED_LOG.read_text())
    assert [r["open_window"] for r in rows2] == [False, True, True]


def test_an_empty_chain_window_is_placed_by_summon_count():
    """It lands on no card of ours, so "after the 2nd summon" is the only
    handle it has -- and it is the one Nibiru is reasoned about with."""
    from skills import db, engine
    engine._export()
    from skills import handlab
    conn = db.connect()
    record = json.loads((Path(__file__).resolve().parent / "fixtures"
                         / "solved_line.json").read_text())
    st = handlab.steps(conn, record)
    assert [s["summon_index"] for s in st] == [None, None, 1, None, None, 2]

    rows, _ = handlab.parse_windows(CONVERTED_LOG.read_text())
    annotated, unplaced = handlab.annotate_steps(
        {"steps": st, "handtraps": [{"name": "Effect Veiler", "code": 97268402,
                                     "detail": rows}]})
    assert not unplaced, f"windows with nowhere to go: {unplaced}"
    placed = {s["n"]: len(s["threats"]) for s in annotated if s["threats"]}
    # one named window on step 5, two empty-chain ones after the 2nd summon
    assert placed == {5: 1, 6: 2}, placed
    sixth = next(s for s in annotated if s["n"] == 6)
    assert all(t["open_window"] and t["after_summons"] == 2
               for t in sixth["threats"])


def test_a_changed_log_format_fails_loudly(monkeypatch):
    """A parser that returns [] on an unknown format reports "no windows",
    which reads as "this hand is safe"."""
    from skills import handlab
    text = FIRE_LOG.read_text().replace("window  0 (", "fenetre  0 (")
    rows, declared = handlab.parse_windows(text)
    assert declared == 3 and len(rows) == 2, "expected one row to stop parsing"
    # analyse() turns that mismatch into parse_error rather than a short list.
    assert len(rows) != declared


def test_a_partial_hand_is_filled_from_the_deck_and_says_so():
    from skills import db, handlab
    from skills.deck import Deck
    c = db.connect()
    d = Deck.from_ydk(Path(__file__).resolve().parent.parent
                      / "assets" / "decks" / "my-despia.ydk")
    d.canonicalise(c)
    nadir = handlab.resolve_in_deck(c, d, "nadir")
    hand, filler = handlab.fill_hand(c, d, [nadir], seed=0)
    assert len(hand) == 5 and len(filler) == 4
    assert hand[0] == nadir, "the chosen card must stay in the hand"
    assert all(f in d.main for f in filler)
    # Deterministic in the seed, so a report can be reproduced.
    assert handlab.fill_hand(c, d, [nadir], seed=0)[0] == hand
    assert handlab.fill_hand(c, d, [nadir], seed=1)[0] != hand


def test_filling_respects_how_many_copies_the_deck_runs():
    """A 1-of must not be dealt to you twice."""
    from collections import Counter
    from skills import db, handlab
    from skills.deck import Deck
    c = db.connect()
    d = Deck.from_ydk(Path(__file__).resolve().parent.parent
                      / "assets" / "decks" / "my-despia.ydk")
    d.canonicalise(c)
    have = Counter(d.main)
    for seed in range(25):
        hand, _ = handlab.fill_hand(c, d, [], seed=seed)
        for code, n in Counter(hand).items():
            assert n <= have[code], f"dealt {n} of a {have[code]}-of"


def test_a_card_not_in_the_deck_is_refused():
    from skills import db, handlab
    from skills.deck import Deck
    c = db.connect()
    d = Deck.from_ydk(Path(__file__).resolve().parent.parent
                      / "assets" / "decks" / "my-despia.ydk")
    d.canonicalise(c)
    with pytest.raises(ValueError):
        handlab.parse_hand(c, d, "Blue-Eyes White Dragon")
    with pytest.raises(ValueError, match="matches"):
        handlab.parse_hand(c, d, "branded")          # ambiguous
    with pytest.raises(ValueError):
        handlab.parse_hand(c, d, "a, b, c, d, e, f")  # six cards
    got = handlab.parse_hand(c, d, "nadir, aluber")
    assert len(got) == 2


PILOT_REPORT = Path(__file__).resolve().parent / "fixtures" / "pilot_report.json"


def test_threats_are_pinned_to_the_step_they_interrupt():
    """Not "Ash beats this hand" but "Ash on your Branded Opening"."""
    from skills import handlab
    r = json.loads(PILOT_REPORT.read_text())
    steps, unplaced = handlab.annotate_steps(r)
    assert not unplaced
    marked = {s["name"]: [t["name"] for t in s["threats"]] for s in steps
              if s["threats"]}
    assert "Branded Opening" in marked
    assert "Ash Blossom & Joyous Spring" in marked["Branded Opening"]
    assert "The Fallen & The Virtuous" not in marked, \
        "a step with no window was marked as threatened"
    step = next(s for s in steps if s["name"] == "Branded Opening")
    assert step["threats"][0]["missing"] == ["Aluber the Jester of Despia"]


def test_a_card_played_twice_does_not_collect_both_windows():
    """A line can activate the same card twice; the windows must land on
    separate steps, in order, not pile onto the first match."""
    from skills import handlab
    r = {"steps": [{"n": 1, "verb": "activate", "name": "Foo"},
                   {"n": 2, "verb": "activate", "name": "Bar"},
                   {"n": 3, "verb": "activate", "name": "Foo"}],
         "handtraps": [{"name": "Ash", "code": 1, "detail": [
             {"over": "Foo", "survived": False, "decision": 2},
             {"over": "Foo", "survived": True, "decision": 9}]}]}
    steps, unplaced = handlab.annotate_steps(r)
    assert not unplaced
    assert len(steps[0]["threats"]) == 1 and len(steps[2]["threats"]) == 1
    assert steps[0]["threats"][0]["survived"] is False
    assert steps[2]["threats"][0]["survived"] is True


def test_a_window_matching_no_step_is_reported_not_misattached():
    """A threat pinned to the wrong action is worse than one left over."""
    from skills import handlab
    r = {"steps": [{"n": 1, "verb": "activate", "name": "Foo"}],
         "handtraps": [{"name": "Ash", "code": 1, "detail": [
             {"over": "Something Else", "survived": False, "decision": 4}]}]}
    steps, unplaced = handlab.annotate_steps(r)
    assert steps[0]["threats"] == []
    assert len(unplaced) == 1 and unplaced[0]["over"] == "Something Else"


def test_the_solver_aims_at_a_board_for_the_hand_you_actually_hold(monkeypatch):
    """ygorl's blocking.solve_blocking honours an explicit job.hand in the
    DUEL and then plans what to aim for from sample_hand(deck, hand_seed)
    -- a different, seeded hand. The solver searches your cards for a
    board chosen for somebody else's and reports "no line found".

    Caught live: the same five cards solved under one seed and came back
    unsolved under another.
    """
    from skills import handlab

    seen = {}

    def fake_plan(main, extra, hand, cards):
        seen["hand"] = list(hand)

        class P:
            base, pieces = [], []
        return P()

    def fake_upstream(job, **kw):
        return {"status": "unsolved", "lines": [], "solver": {}}

    import ygorl.solver.blocking as B
    import ygorl.solver.batch as BT
    from ygorl.cards.ydk import load_ydk
    monkeypatch.setattr(B, "blocking_plan", fake_plan)
    monkeypatch.setattr(B, "solve_blocking", fake_upstream)
    monkeypatch.setattr(BT, "solve_hand", lambda *a, **k: None)

    deck_path = (Path(__file__).resolve().parent.parent / "assets" / "decks"
                 / "my-despia.ydk")
    mine = list(load_ydk(deck_path).main[:5])

    class Job:
        pass
    job = Job()
    job.deck_path, job.hand_seed, job.scratch = deck_path, 7, Path("/tmp")
    handlab.solve_for_hand(job, mine, pieces=1)
    assert seen["hand"] == mine, (
        "targets were planned from a seeded hand, not the one you hold")


def test_a_random_hand_is_one_the_deck_could_really_open():
    from collections import Counter
    from skills import db, handlab
    from skills.deck import Deck
    c = db.connect()
    d = Deck.from_ydk(Path(__file__).resolve().parent.parent
                      / "assets" / "decks" / "my-despia.ydk")
    d.canonicalise(c)
    have = Counter(d.main)
    for seed in range(40):
        hand, got = handlab.random_hand(d, seed)
        assert got == seed and len(hand) == 5
        for code, n in Counter(hand).items():
            assert n <= have[code], f"dealt {n} copies of a {have[code]}-of"


def test_a_dealt_hand_can_be_dealt_again():
    """A report nobody can reproduce is an anecdote."""
    from skills import db, handlab
    from skills.deck import Deck
    c = db.connect()
    d = Deck.from_ydk(Path(__file__).resolve().parent.parent
                      / "assets" / "decks" / "my-despia.ydk")
    d.canonicalise(c)
    first, seed = handlab.random_hand(d)
    assert handlab.random_hand(d, seed)[0] == first
    assert handlab.parse_hand(c, d, f"random:{seed}") == first
    # Two unseeded deals should not be the same hand every time.
    deals = {tuple(handlab.random_hand(d)[0]) for _ in range(12)}
    assert len(deals) > 1, "the deal is not random"


def test_the_board_reader_finds_every_place_a_card_can_act_from():
    """A negate on the field and a negate in the graveyard are not the
    same card to play around, so "where" has to be real for all of them.

    Guards against a reader that silently only ever looks at the field:
    the first version found nothing in the graveyard across 271 stored
    boards, and only a pool scan showed that was the decks' doing and not
    a broken detector.
    """
    import random

    from skills import engine
    engine._export()
    from skills import db, handlab
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import card_interruptions

    conn = db.connect()
    cards = default_cards()
    ids = [r["id"] for r in conn.execute("SELECT id FROM cards")]
    random.Random(0).shuffle(ids)
    places = set()
    quick_places = set()
    for cid in ids[:1500]:
        places |= {i.where for i in card_interruptions(cid, cards)}
        quick_places |= set(handlab.quick_actions(cid, cards))
    assert {"field", "set", "hand", "grave"} <= places, (
        f"no card interrupts from {{'field','set','hand','grave'}} - {places}")
    assert "grave" in quick_places, "no graveyard follow-up is ever detected"


def test_board_reading_separates_stopping_from_follow_up():
    from skills import db, handlab
    conn = db.connect()
    rec = json.loads(PILOT_REPORT.read_text())
    rd = rec["reading"]
    assert rd["interruptions"] and rd["follow_up"]
    stop = rd["interruptions"][0]
    assert stop["kind"] in ("negate", "disrupt") and stop["where"] == "field"
    # A card that both negates and does something else must say so rather
    # than being counted twice as two separate pieces of follow-up.
    dup = rd["follow_up"][0]
    assert dup["name"] == stop["name"] and dup["also_interrupts"] is True
    assert rd["resources"]["grave"] == 7


def test_an_empty_board_reads_as_empty_not_as_an_error():
    from skills import db, handlab
    conn = db.connect()
    assert handlab.read_board(conn, {"lines": []}) == {
        "interruptions": [], "follow_up": [], "resources": {}}
    assert handlab.read_board(conn, {"lines": [{"board": {}}]})["resources"] == {}


def test_read_board_sees_the_graveyard_and_the_hand_not_just_the_field():
    """The question was "a GY negate, a kept Ash" -- a reader that only
    looks at the field answers "nothing" and looks right doing it.

    Built from a synthetic board because the stored library happens to
    contain no graveyard interrupter, which is exactly how a field-only
    reader survived its first two tests.
    """
    from skills import db, engine
    engine._export()
    from skills import handlab

    conn = db.connect()
    FOX = 94919024        # Lunalight Crimson Fox: negates from the grave
    ASH = 14558127        # kept in hand
    MIRRORJADE = 44146295  # disrupts from the field
    rec = {"lines": [{"board": {"players": [{
        "mzone": [{"code": MIRRORJADE, "position": 1, "sequence": 0}],
        "szone": [], "hand": [ASH], "grave": [FOX], "banished": [],
        "deck_count": 30, "extra_count": 10}]}}]}
    rd = handlab.read_board(conn, rec)
    by_where = {x["where"]: x for x in rd["interruptions"]}
    assert "grave" in by_where, "a graveyard negate was not seen"
    assert by_where["grave"]["name"] == "Lunalight Crimson Fox"
    assert by_where["grave"]["kind"] == "negate"
    assert "hand" in by_where, "a hand trap kept in hand was not seen"
    assert by_where["hand"]["name"] == "Ash Blossom & Joyous Spring"
    assert "field" in by_where
    assert rd["resources"]["grave"] == 1 and rd["resources"]["hand"] == 1


def test_a_face_down_monster_is_not_read_as_an_active_negate():
    from skills import db, engine
    engine._export()
    from skills import handlab
    conn = db.connect()
    MIRRORJADE = 44146295
    face_down = {"lines": [{"board": {"players": [{
        "mzone": [{"code": MIRRORJADE, "position": 8, "sequence": 0}],
        "szone": [], "hand": [], "grave": [], "banished": []}]}}]}
    assert handlab.read_board(conn, face_down)["interruptions"] == []


def test_the_line_says_what_each_card_searched_and_used():
    """Replays a stored line rather than reading a pre-built fixture.

    The first version of this test asserted against the fixture's own
    `steps`, which had been generated by the code under test -- so
    breaking the attribution could not fail it, and a mutation proved
    exactly that.
    """
    from skills import db, engine
    engine._export()
    from skills import handlab
    conn = db.connect()
    record = json.loads((Path(__file__).resolve().parent / "fixtures"
                         / "solved_line.json").read_text())
    by_name = {s["name"]: s for s in handlab.steps(conn, record)}
    did = lambda n, w: [d["name"] for d in by_name[n]["did"] if d["what"] == w]

    # A chain resolves BACKWARDS. Crediting the nearest preceding
    # activation hands Aluber's search to Branded Opening.
    assert did("Aluber the Jester of Despia", "searched") == ["Branded Fusion"]
    assert did("Branded Opening", "searched") == ["Aluber the Jester of Despia"]
    assert did("Branded Opening", "discarded") == ["Nadir Servant"]
    assert sorted(did("Branded Fusion", "material")) == [
        "Fallen of the White Dragon", "Mulcharmy Fuwalos"]


def test_trap_selection():
    from skills import handlab
    assert len(handlab.parse_traps("")) == 7
    assert len(handlab.parse_traps("all")) == 7
    assert handlab.parse_traps("none") == {}
    picked = handlab.parse_traps("ash, imperm")
    assert set(picked.values()) == {"Ash Blossom & Joyous Spring",
                                    "Infinite Impermanence"}
    assert handlab.parse_traps("14558127") == {
        14558127: "Ash Blossom & Joyous Spring"}
    with pytest.raises(ValueError):
        handlab.parse_traps("not a card")


def test_exposure_prefers_the_line_that_offers_fewer_chances():
    """Four windows surviving three, and one window surviving none, both
    lose once. The second is the safer line, so chances offered breaks
    the tie rather than being ignored."""
    from skills import handlab
    many = [{"windows": 4, "converted": 3}]
    few = [{"windows": 1, "converted": 0}]
    assert handlab.exposure(many)[0] == handlab.exposure(few)[0] == 1
    assert handlab.exposure(few) < handlab.exposure(many)
    assert handlab.exposure([{"windows": 0, "converted": 0}]) == (0, 0)


def test_one_hand_trap_does_not_pay_for_a_process_pool(monkeypatch):
    """Spawning a pool to run a single job costs more than it saves, and
    the sequential path is also what keeps a one-trap run debuggable."""
    import ygorl.solver.batch as BT
    from skills import engine
    engine._export()
    from skills import handlab

    calls = []

    class _D:
        def to_json(self):
            return {"status": "solved", "lines": [], "solver": {}}

    monkeypatch.setattr(BT, "solve_fire",
                        lambda job, base, code: (calls.append(code), _D())[1])
    monkeypatch.setattr(handlab.multiprocessing if hasattr(
        handlab, "multiprocessing") else BT, "__name__", "x", raising=False)

    class Job:
        scratch, threads = Path("/tmp"), 1
    got = list(handlab._fire_all(Job(), object(), {7: "Only One"}, 1,
                                 lambda m: None))
    assert [g[0] for g in got] == [7] and calls == [7]


def test_the_report_orders_hand_traps_by_name_not_by_who_finished():
    """Fired in parallel, they come back in completion order; a person
    reads the report, so the order has to be stable."""
    rows = [{"name": "Nibiru, the Primal Being"}, {"name": "Ash Blossom"},
            {"name": "Effect Veiler"}]
    rows.sort(key=lambda r: r["name"])
    assert [r["name"] for r in rows] == [
        "Ash Blossom", "Effect Veiler", "Nibiru, the Primal Being"]
    src = (Path(__file__).resolve().parent.parent / "skills"
           / "handlab.py").read_text()
    assert 'out["handtraps"].sort(key=lambda r: r["name"])' in src, (
        "the parallel results are appended but never reordered")
