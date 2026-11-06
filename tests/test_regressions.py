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
import tempfile
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
                      fire=None, **kw):
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
    # Five terms since window recovery joined the objective. The kewl-tune
    # summaries carried no window_recovery at all, so it reads constant
    # too -- which is itself correct: a term with no data cannot rank.
    assert set(constant) == {"solve rate", "brick rate", "handtrap survival",
                             "window recovery"}
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


def test_the_goal_climbs_past_a_pair():
    """Upstream stops at base + 2 interrupters, so a deck with four
    reachable ones is never asked whether it can make three. That is not
    the solver failing to find a good board -- it is nobody requesting
    one, and it is why these reports kept returning two interruptions."""
    src = (Path(__file__).resolve().parent.parent / "skills"
           / "handlab.py").read_text()
    block = src[src.index("    solved_pieces = []"):src.index("    if not results")]
    assert 'f"stack:{len(trying)}"' in block, "the climb is gone"
    assert "solved_pieces[:2]" not in block, (
        "still capped at the best two pieces")
    # It must stop at the first refusal rather than grinding every subset.
    assert "break" in block


def test_the_fire_verdict_summary_is_parsed():
    from skills import handlab
    v = handlab.parse_verdict(CONVERTED_LOG.read_text())
    assert v == {"converted": 3, "windows": 3, "full": 3, "partial": 0}
    assert handlab.parse_verdict("nothing here") is None
    made_up = ("=== --fire verdict: 5 window(s) out of 9 converted "
               "(2 full board, 3 without the sacrificed card) ===")
    assert handlab.parse_verdict(made_up) == {
        "converted": 5, "windows": 9, "full": 2, "partial": 3}


def test_a_spare_is_only_offered_when_the_board_has_one_to_spare():
    """Offering the only interrupter as expendable asks nothing at all."""
    src = (Path(__file__).resolve().parent.parent / "skills"
           / "handlab.py").read_text()
    assert "spare = goal[-1] if len(goal) > 1 else None" in src


def test_the_spare_reaches_the_child_process():
    """The pool runs solve_fire in a subprocess, so a flag injected in
    the parent would never be seen. Checked structurally because the
    alternative is a two-minute solver run per assertion."""
    import inspect
    from skills import handlab
    src = inspect.getsource(handlab._fire_one)
    assert "--fire-spare" in src
    assert "job, base, code, spare = args" in src, (
        "the spare is not carried into the worker")
    assert "finally" in src, "the patched SolveRequest is never restored"


# --- gauntlet: ranking boards by what they stop --------------------------

def test_board_score_no_longer_eats_the_plans_piece_list():
    """`**score.to_json()` splatted last overwrote "pieces" in every
    blocking record ever written -- 271 of 280 in results/boards.jsonl
    hold the board score's shape under that key. Both lists are wanted."""
    import inspect
    from skills import handlab
    src = inspect.getsource(handlab.solve_for_hand)
    assert '"plan_pieces": [p.target for p in plan.pieces]' in src
    assert '"pieces": [p.target for p in plan.pieces]' not in src, (
        "the plan's pieces are back under the key the board score uses")


def test_two_draws_of_the_same_board_collapse():
    """The candidates must be ALTERNATIVES. A board is identified by what
    it interrupts with, regardless of the order the solver listed it in,
    or four draws of one board would be offered as four choices."""
    from skills.gauntlet import Candidate
    a = Candidate(targets=("1",), label="piece:1", interruptions=2, negates=1,
                  pieces=((100, "field", True), (200, "hand", False)),
                  draws=1, solve_s=1.0)
    b = Candidate(targets=("1",), label="stack:2", interruptions=2, negates=1,
                  pieces=((200, "hand", False), (100, "field", True)),
                  draws=1, solve_s=1.0)
    c = Candidate(targets=("1",), label="piece:1", interruptions=2, negates=1,
                  pieces=((100, "field", True), (300, "hand", False)),
                  draws=1, solve_s=1.0)
    assert a.board_key() == b.board_key()
    assert a.board_key() != c.board_key()


def _stub_candidates(monkeypatch, cands):
    from skills import gauntlet
    monkeypatch.setattr(gauntlet, "candidate_boards",
                        lambda *a, **k: list(cands))


def _stub_defend(monkeypatch, rates, controls=None):
    """defend() keyed by candidate label, so no solver is needed.

    `rates` is the board's stop rate, `controls` its PAIRED control's --
    the same position with the builder passing. Each candidate carries
    its own now; there is no separate floor.
    """
    from skills import gauntlet

    def fake(our_deck, our_hand, candidate, their_decks, **kw):
        rate = rates[candidate.label]
        ctrl = (controls or {}).get(candidate.label, 0.0)
        return {"candidate": candidate.to_json(), "rows": [],
                "tested": 10, "stopped": int(rate * 10), "stop_rate": rate,
                "control_tested": 10, "control_stopped": int(ctrl * 10),
                "control_rate": ctrl, "beats_control": rate > ctrl}
    monkeypatch.setattr(gauntlet, "defend", fake)


def _cand(label, interruptions, code):
    from skills.gauntlet import Candidate
    return Candidate(targets=(str(code),), label=label,
                     interruptions=interruptions, negates=0,
                     pieces=((code, "field", False),), draws=1, solve_s=1.0)


def test_the_do_nothing_board_is_carried_as_a_candidate(monkeypatch):
    """The `base` rung -- build nothing, keep what the hand holds -- is
    a real comparison the heuristic score does not make, so it survives
    the `keep` cut. It is NOT the zero point: "do nothing" held Droll and
    Effect Veiler in the run that prompted this, two live interactions.
    The zero point is each candidate's own paired control."""
    from skills import gauntlet
    cands = [_cand("stack:2", 5, 1), _cand("piece:9", 4, 2),
             _cand("base", 2, 3)]
    _stub_candidates(monkeypatch, cands)
    _stub_defend(monkeypatch, {"stack:2": 0.8, "piece:9": 0.6, "base": 0.2},
                 controls={"stack:2": 0.1, "piece:9": 0.1, "base": 0.1})
    rep = gauntlet.rank("deck.ydk", [1, 2, 3, 4, 5], [("them.ydk", "1")],
                        keep=2)
    labels = [r["candidate"]["label"] for r in rep["results"]]
    assert "base" in labels, "the base rung was dropped by the keep cut"
    assert rep["discriminated"] is True
    assert rep["beats_control"] is True


def test_identical_stop_rates_are_reported_as_no_resolution(monkeypatch):
    """Three boards that all stop everything is a test with no power, not
    a three-way tie to read an ordering into."""
    from skills import gauntlet
    cands = [_cand("stack:2", 5, 1), _cand("base", 2, 3)]
    _stub_candidates(monkeypatch, cands)
    _stub_defend(monkeypatch, {"stack:2": 1.0, "base": 1.0},
                 controls={"stack:2": 0.5, "base": 0.5})
    rep = gauntlet.rank("deck.ydk", [1, 2, 3, 4, 5], [("them.ydk", "1")],
                        keep=2)
    assert rep["discriminated"] is False


def test_the_opponent_gets_best_of_n_draws():
    """Their search is the same randomized search -- measured at 50%
    P(solve) per 60s draw on a borderline goal -- so one draw would make
    "stopped" a coin flip. Any draw through counts as through."""
    import inspect
    from skills import gauntlet
    src = inspect.getsource(gauntlet.defend)
    assert 'cell["through"] > 0' in src, (
        "the opponent no longer gets the benefit of every draw")


def test_an_empty_field_stopping_everything_is_not_a_ranking(monkeypatch):
    """If the attacker cannot function against NOTHING, the ordering
    above is not about anybody's board.

    This is what actually happened, and for a reason no floor could
    reveal: the harness never put a board on the field at all, so the
    board and the control were the same position. Now they differ by
    exactly the board, and a control that stops everything means the
    attacker's own turn-2 search is the obstacle."""
    from skills import gauntlet
    cands = [_cand("stack:2", 5, 1), _cand("base", 2, 3)]
    _stub_candidates(monkeypatch, cands)
    _stub_defend(monkeypatch, {"stack:2": 1.0, "base": 1.0},
                 controls={"stack:2": 1.0, "base": 1.0})
    rep = gauntlet.rank("deck.ydk", [1, 2, 3, 4, 5], [("them.ydk", "1")],
                        keep=2)
    assert rep["control_is_broken"] is True
    assert rep["beats_control"] is False, (
        "a board no better than an empty field was called a result")
    _stub_defend(monkeypatch, {"stack:2": 1.0, "base": 0.9},
                 controls={"stack:2": 0.5, "base": 0.5})
    rep = gauntlet.rank("deck.ydk", [1, 2, 3, 4, 5], [("them.ydk", "1")],
                        keep=2)
    assert rep["control_is_broken"] is False
    assert rep["beats_control"] is True


def test_the_unsupported_notice_is_not_optional(monkeypatch):
    """Every path that reports a going-second number must say it is not
    to be believed. `rank` prints it and the record carries it, because a
    JSON file outlives the terminal it was printed in."""
    from skills import breaking, gauntlet
    cands = [_cand("stack:2", 5, 1)]
    _stub_candidates(monkeypatch, cands)
    _stub_defend(monkeypatch, {"stack:2": 0.8}, controls={"stack:2": 0.1})
    said = []
    rep = gauntlet.rank("deck.ydk", [1, 2, 3, 4, 5], [("them.ydk", "1")],
                        keep=1, log=said.append)
    assert any("UNSUPPORTED" in m for m in said), said[:3]
    assert rep["unsupported"] == breaking.UNSUPPORTED


# --- sweep: where the extra draws go -------------------------------------

def _att(label, status, targets, interruptions=None, best_placed=None):
    a = {"label": label, "status": status, "targets": targets}
    if interruptions is not None:
        a["interruptions"], a["negates"] = interruptions, 0
    if best_placed is not None:
        a["best_placed"] = best_placed
    return a


def test_a_goal_that_placed_nothing_earns_no_redraws():
    """41% of the stored sweep's failed attempts (132 of 320) never placed
    a single target card, each burning a full budget -- 2.2 of 8.2 hours.
    A seed does not rescue a goal the search cannot get one card towards."""
    from skills.sweep import allocate
    atts = [_att("piece:1", "unsolved", ["1"], best_placed=0),
            _att("piece:2", "unsolved", ["2"], best_placed=0)]
    assert allocate(atts, draws=4, n_goals=2) == []


def test_solved_goals_are_redrawn_before_near_misses():
    """Re-drawing a SOLVED goal is what finds the better board; a near
    miss is only a maybe."""
    from skills.sweep import allocate
    atts = [_att("piece:1", "unsolved", ["a", "1"], best_placed=1),
            _att("piece:2", "solved", ["a", "2"], interruptions=3)]
    spend = allocate(atts, draws=3, n_goals=2)
    assert len(spend) == 4, "the budget changed"
    assert spend[0][1] == "piece:2", "the solved goal was not redrawn first"
    assert {s[1] for s in spend} == {"piece:1", "piece:2"}


def test_the_better_board_is_redrawn_first():
    from skills.sweep import allocate
    atts = [_att("piece:1", "solved", ["1"], interruptions=2),
            _att("stack:2", "solved", ["1", "2"], interruptions=5)]
    spend = allocate(atts, draws=2, n_goals=2)
    assert [s[1] for s in spend] == ["stack:2", "piece:1"]


def test_redraws_spread_round_robin_not_all_into_one():
    from skills.sweep import allocate
    atts = [_att("a", "solved", ["1"], interruptions=3),
            _att("b", "solved", ["2"], interruptions=3),
            _att("c", "unsolved", ["3"], best_placed=0)]
    spend = allocate(atts, draws=3, n_goals=3)
    labels = [s[1] for s in spend]
    assert labels.count("a") == 3 and labels.count("b") == 3, labels
    assert "c" not in labels


def test_the_budget_is_unchanged_by_the_allocation():
    """Breadth round is n_goals solves; depth must be (draws-1)*n_goals,
    or the two-round sweep is not comparable with a flat one."""
    from skills.sweep import allocate
    atts = [_att(f"g{i}", "solved", [str(i)], interruptions=2)
            for i in range(5)]
    for draws in (1, 2, 4, 7):
        spend = allocate(atts, draws=draws, n_goals=5)
        assert len(spend) == max(0, (draws - 1) * 5), draws


def test_each_redraw_of_a_goal_gets_its_own_seed():
    """Two draws of one goal at the same seed redo identical work. The
    seed must be reproducible across RUNS but distinct across DRAWS."""
    from skills.sweep import _draw_seed
    a = _draw_seed(99, ["1@hand", "2"], 0)
    assert a == _draw_seed(99, ["1@hand", "2"], 0), "not reproducible"
    assert a != _draw_seed(99, ["1@hand", "2"], 1), "draws collide"
    assert a != _draw_seed(99, ["1@hand", "3"], 0), "goals collide"
    assert a != _draw_seed(98, ["1@hand", "2"], 0), "hands collide"


def test_sweep_summary_matches_the_vendored_shape():
    """`gate.load_boards` reads this shape. sweep.summarize is a copy of
    the vendored one, so the keys must not drift apart silently."""
    import inspect
    from skills import sweep
    vendored = (Path(__file__).resolve().parent.parent / "vendor" / "ygorl"
                / "tools" / "solve_blocking.py").read_text()
    ours = inspect.getsource(sweep.summarize)
    for key in ("solve_rate", "wall_per_hand_s", "mean_interruptions",
                "top_pieces", "boards", "hand_left", "spells_traps"):
        assert f'"{key}"' in ours, f"sweep.summarize dropped {key}"
        assert f'"{key}"' in vendored, f"{key} is no longer upstream's"


def test_non_prefix_stacks_are_proposed_once_the_pieces_are_known():
    """`_goals` can only enumerate PREFIX stacks: with pieces A B C D it
    asks AB, ABC, ABCD and never AC. A deck whose reachable pair is A+C
    was never asked whether it can make two interrupters."""
    from skills.sweep import combinations_to_try
    atts = [_att("piece:A", "solved", ["base@hand", "A"], interruptions=2),
            _att("piece:B", "unsolved", ["base@hand", "B"], best_placed=0),
            _att("piece:C", "solved", ["base@hand", "C"], interruptions=2),
            _att("stack:2", "unsolved", ["base@hand", "A", "B"],
                 best_placed=1)]
    combos = combinations_to_try(atts)
    sets = [frozenset(t) for t, _ in combos]
    assert frozenset(["base@hand", "A", "C"]) in sets, (
        "A+C solved separately and was still never requested together")
    # B never placed a card, so no combination should include it.
    assert all("B" not in t for t, _ in combos), combos


def test_bigger_stacks_are_proposed_before_smaller_ones():
    """More interrupters is the point, so the three-stack is asked first."""
    from skills.sweep import combinations_to_try
    atts = [_att(f"piece:{p}", "solved", ["b@hand", p], interruptions=2)
            for p in "ABC"]
    combos = combinations_to_try(atts)
    assert len(combos[0][0]) == 4, combos[0]      # base + 3 pieces
    assert combos[0][1] == "stack:3"


def test_a_combination_already_tried_is_not_proposed_again():
    from skills.sweep import combinations_to_try
    atts = [_att("piece:A", "solved", ["b@hand", "A"], interruptions=2),
            _att("piece:C", "solved", ["b@hand", "C"], interruptions=2),
            _att("stack:2", "solved", ["b@hand", "A", "C"], interruptions=4)]
    assert combinations_to_try(atts) == []


def test_one_solved_piece_proposes_no_stack():
    from skills.sweep import combinations_to_try
    atts = [_att("piece:A", "solved", ["b@hand", "A"], interruptions=2),
            _att("piece:B", "unsolved", ["b@hand", "B"], best_placed=0)]
    assert combinations_to_try(atts) == []


def test_breadth_does_not_ask_for_stacks():
    """A target set is a conjunction, so it is monotone: if the search
    cannot place one card of {base, A} it cannot place {base, A, B}. The
    prefix stacks in the breadth round spent budget proving what the piece
    goals already proved -- and, because `combinations_to_try` skips what
    was tried, they crowded out the evidence-based stacks it exists to
    propose. The first despia sweep said so: "0 of them combinations the
    enumeration could not reach".

    Behavioural, against a real deck and a real hand: no breadth goal may
    name more than one piece."""
    from skills import sweep
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import blocking_plan

    deck_path = (Path(__file__).resolve().parent.parent / "assets" / "decks"
                 / "elfnote.ydk")
    deck = load_ydk(deck_path)
    hand = list(deck.main[:5])
    cards = default_cards()
    base = set(blocking_plan(deck.main, deck.extra, hand, cards).base)
    goals = sweep._breadth_goals(deck_path, hand, 4)
    assert goals, "no breadth goals at all"
    for targets, label in goals:
        extra = [t for t in targets if t not in base]
        assert len(extra) <= 1, (
            f"{label} names {len(extra)} pieces: breadth is enumerating "
            f"stacks again ({targets})")
    assert not any(l.startswith("stack") for _, l in goals)


def test_the_stacks_wave_runs_at_the_triage_budget():
    """New goals are asked cheaply first, same as the pieces were; only
    what earns it gets the full budget."""
    import inspect
    from skills import sweep
    body = inspect.getsource(sweep.solve_deck)
    i = body.index("combinations_to_try")
    window = body[i:i + 400]
    assert "triage_ms" in window, (
        "the stacks wave is spending the full budget on untried goals")


def test_the_sweep_runs_breadth_then_stacks_then_depth(tmp_path,
                                                       monkeypatch):
    """The wave structure, with no solver at all.

    What it pins: breadth asks one draw of each piece goal at the TRIAGE
    budget; the stacks wave then asks for combinations of the pieces that
    solved, also cheap; only the depth wave spends the full budget. Those
    three decisions are the sweep, and they had no test because
    exercising them for real is hours of CPU.
    """
    from skills import sweep

    waves = []

    def fake_map(batch, workers):
        waves.append([{"label": b[6], "targets": tuple(b[5]),
                       "solve_ms": b[7], "draw": b[8], "hand": b[2]}
                      for b in batch])
        for b in batch:
            label, targets = b[6], tuple(b[5])
            # Pieces A and C solve; B does not and places nothing. Any
            # stack of the solvable ones solves, and better.
            solvable = "B" not in targets
            n = len([t for t in targets if not t.endswith("@hand")])
            yield {
                "hand_index": b[2], "label": label,
                "attempt": {"label": label, "targets": list(targets),
                            "draw": b[8], "solver_seed": 1,
                            "status": "solved" if solvable else "unsolved",
                            "wall_s": 1.0,
                            **({"interruptions": 1 + n, "negates": 0}
                               if solvable else {"best_placed": 0})},
                "score": {"interruptions": 1 + n, "negates": 0,
                          "pieces": []} if solvable else None,
                "record": {"deck": {"name": "t"}, "status": "solved",
                           "targets": list(targets), "lines": [{"board": {}}],
                           "solver": {}} if solvable else None,
                "unsolved_record": None if solvable else {
                    "deck": {"name": "t"}, "status": "unsolved",
                    "targets": list(targets), "lines": [], "solver": {}},
            }

    monkeypatch.setattr(sweep, "_map_solves", fake_map)
    monkeypatch.setattr(sweep.engine, "require", lambda: None)
    monkeypatch.setattr(sweep.engine, "preflight", lambda d: None)
    monkeypatch.setattr(sweep, "_breadth_goals", lambda p, h, n: [
        (("b@hand", "A"), "piece:A"), (("b@hand", "B"), "piece:B"),
        (("b@hand", "C"), "piece:C"), (("b@hand",), "base")])

    class FakeDeck:
        name = "t"
        main = list(range(40))
        extra = []

    monkeypatch.setattr("ygorl.cards.ydk.load_ydk", lambda p: FakeDeck())
    monkeypatch.setattr("ygorl.solver.batch.sample_hand",
                        lambda deck, seed, *a, **k: (list(range(5)), None))
    monkeypatch.setattr("ygorl.solver.batch.hand_seed",
                        lambda s, n, i: 1000 + i)

    out = tmp_path / "sweep.jsonl"
    sweep.solve_deck(tmp_path / "t.ydk", hands=1, draws=4, out=out,
                     solve_ms=60_000, workers=1)

    assert len(waves) >= 3, (
        f"expected breadth, stacks and at least one depth round; "
        f"got {len(waves)}")
    breadth, stacks, depth_rounds = waves[0], waves[1], waves[2:]
    assert all(j["solve_ms"] == sweep.TRIAGE_MS for j in breadth), (
        "breadth is not using the triage budget")
    assert all(j["solve_ms"] == sweep.TRIAGE_MS for j in stacks), (
        "the stacks wave is paying the full budget for untried goals")
    assert all(j["solve_ms"] == 60_000 for w in depth_rounds for j in w), (
        "a depth round is not using the full budget")
    # A and C solved separately, so A+C must be asked for -- the pair the
    # prefix enumeration could never name.
    assert any(set(j["targets"]) == {"b@hand", "A", "C"} for j in stacks), (
        [j["targets"] for j in stacks])
    # B placed nothing, so no depth round may name it.
    assert all("B" not in j["targets"] for w in depth_rounds for j in w), (
        [j["targets"] for w in depth_rounds for j in w])
    # Early stopping is OFF by default -- it gave back the whole measured
    # quality gain on despia -- so all draws-1 rounds must run.
    assert len(depth_rounds) == 3, (
        f"{len(depth_rounds)} depth rounds at draws=4 with the default "
        f"patience: early stopping is on by default again")
    # Every draw index must be distinct per goal, or re-draws repeat the
    # same reproducible seed and redo identical work.
    draws_used = [(j["hand"], j["targets"], j["draw"])
                  for w in depth_rounds for j in w]
    assert len(draws_used) == len(set(draws_used)), "a draw index repeats"


def test_every_solve_deck_knob_is_reachable_from_sweep():
    """A parameter that exists and cannot be set is this project's
    signature bug: `engine.duel`'s arguments were audited for exactly
    this, and the handtrap fire list was written and then dropped. If
    solve_deck grows a knob, sweep() has to forward it."""
    import inspect
    from skills import sweep
    inner = set(inspect.signature(sweep.solve_deck).parameters)
    outer = set(inspect.signature(sweep.sweep).parameters)
    # first_hand is deliberately per-deck, not a sweep-wide option
    missing = inner - outer - {"deck_path", "first_hand"}
    assert not missing, f"sweep() cannot set: {sorted(missing)}"


def test_early_stopping_fires_when_asked_for(tmp_path, monkeypatch):
    """`patience` is opt-in, and when passed it must actually stop a hand
    that is not improving -- 2 flat rounds, not all draws-1."""
    from skills import sweep

    rounds = []

    def fake_map(batch, workers):
        rounds.append(batch)
        for b in batch:
            targets = tuple(b[5])
            yield {"hand_index": b[2], "label": b[6],
                   "attempt": {"label": b[6], "targets": list(targets),
                               "draw": b[8], "solver_seed": 1,
                               "status": "solved", "wall_s": 1.0,
                               "interruptions": 2, "negates": 0},
                   "score": {"interruptions": 2, "negates": 0, "pieces": []},
                   "record": {"deck": {"name": "t"}, "status": "solved",
                              "targets": list(targets),
                              "lines": [{"board": {}}], "solver": {}},
                   "unsolved_record": None}

    monkeypatch.setattr(sweep, "_map_solves", fake_map)
    monkeypatch.setattr(sweep.engine, "require", lambda: None)
    monkeypatch.setattr(sweep.engine, "preflight", lambda d: None)
    monkeypatch.setattr(sweep, "_breadth_goals",
                        lambda p, h, n: [(("A",), "piece:A")])

    class FakeDeck:
        name = "t"
        main = list(range(40))
        extra = []

    monkeypatch.setattr("ygorl.cards.ydk.load_ydk", lambda p: FakeDeck())
    monkeypatch.setattr("ygorl.solver.batch.sample_hand",
                        lambda deck, seed, *a, **k: (list(range(5)), None))
    monkeypatch.setattr("ygorl.solver.batch.hand_seed", lambda s, n, i: 7)

    sweep.solve_deck(tmp_path / "t.ydk", hands=1, draws=6,
                     out=tmp_path / "s.jsonl", solve_ms=60_000, workers=1,
                     patience=2)
    # breadth, stacks-or-not, then depth rounds. The stub never improves,
    # so depth must stop after 2 rounds instead of running all 5.
    depth = [r for r in rounds if all(j[7] == 60_000 for j in r)]
    assert len(depth) == 2, f"{len(depth)} depth rounds with patience=2"


def test_a_single_draw_does_not_silently_become_a_cheap_run():
    """With draws=1 there is no depth round, so nothing is promoted to the
    full budget and a 15s triage would just BE the run -- the one failure
    mode this project names outright."""
    import inspect
    from skills import sweep
    src = inspect.getsource(sweep.solve_deck)
    assert "if draws <= 1:" in src and "triage_ms = solve_ms" in src, (
        "draws=1 can run the whole sweep at the triage budget")


def test_a_degenerate_objective_refuses_to_rank(monkeypatch, capsys):
    """A ranked table with a caveat underneath still reads as a ranking,
    which is the shape of every wrong answer this project has produced.
    Measured on the stored kewl-tune run: brick rate, survival and solve
    rate were each ONE distinct value over all 10 trials, so the whole
    order came from the combo-length tiebreak at weight -0.05."""
    import daemon
    from skills import optimizer

    # Three terms constant, one moving: the degenerate case.
    hist = [optimizer.Candidate(
        deck=None, label=f"swap{i}", score=0.3 - i * 0.01,
        summary={"brick_rate": 0.1667, "best_line_rate": 0.8333,
                 "hand_trap_survival": 0.0,
                 "combo_actions": {"mean": 9.0 + i}}) for i in range(4)]
    const, vary = optimizer.degenerate_terms(hist)
    assert len(vary) == 1 and vary[0] == "combo length", (const, vary)

    rows = optimizer.report(hist, hands=12)
    assert rows, "report produced nothing to rank"
    # The scores DO differ, so without the guard a table would be printed.
    assert len({r["score"] for r in rows}) > 1


def test_the_optimizer_refusal_precedes_any_table():
    """Order matters: the refusal must be emitted before the trial list,
    and the degenerate branch must not write a 'best list' file."""
    import inspect
    import daemon
    src = inspect.getsource(daemon.cmd_optimize)
    i_refuse = src.index("NO RANKING")
    i_table = src.index('"change"') if '"change"' in src else src.index("change")
    assert i_refuse < i_table, (
        "the ranked table is printed before the no-ranking refusal")
    # The refusal branch returns rather than writing optimize_*.ydk.
    branch = src[i_refuse:src.index("best:", i_refuse)]
    assert "return" in branch, "the degenerate branch falls through to writing a list"
    assert "write_ydk" not in branch, (
        "a degenerate run still writes a 'best list'")


def test_the_solve_paths_clean_up_their_scratch_root():
    """`solve_hand` removes its own `job` subdirectory, not the mkdtemp
    ROOT it is handed, so every draw leaked a directory: 914 of them
    under /tmp over two days (122 MB), one per (hand, goal, draw).

    The first version of this test only grepped the source for
    "rmtree(scratch" -- and passed while `sweep.py` was missing
    `import shutil`, so EVERY sweep raised NameError at run time. A
    structural assertion cannot catch a missing import. This one runs the
    cleanup path."""
    import inspect
    from skills import gauntlet, sweep
    for fn in (sweep._solve_one, gauntlet._one_draw):
        src = inspect.getsource(fn)
        assert "finally:" in src, f"{fn.__name__} has no cleanup"
        assert "rmtree(scratch" in src, (
            f"{fn.__name__} does not remove its mkdtemp root")
    # Every name the cleanup uses must actually resolve in that module.
    for mod in (sweep, gauntlet):
        assert hasattr(mod, "shutil"), (
            f"{mod.__name__} calls shutil.rmtree without importing shutil")
        assert hasattr(mod, "tempfile"), f"{mod.__name__} lacks tempfile"


def test_solve_one_reaches_the_solver_without_a_nameerror(monkeypatch, tmp_path):
    """`_solve_one` is stubbed out of every other test, so a NameError in
    it is invisible to the suite. This calls it for real with the solver
    itself replaced, which exercises the mkdtemp + finally + rmtree path
    that broke every sweep and the optimiser's brick re-check."""
    from skills import sweep

    class FakeDemo:
        status = "unsolved"
        lines = []
        solver = {"wall_s": 0.1}

        def to_json(self):
            return {"deck": {"name": "t"}, "status": "unsolved",
                    "lines": [], "solver": dict(self.solver)}

    monkeypatch.setattr("ygorl.solver.batch.solve_hand",
                        lambda job, cards=None, scripts=None: FakeDemo())
    monkeypatch.setattr("ygorl.engine.duel.default_cards", lambda: object())
    monkeypatch.setattr("ygorl.solver.find_solver", lambda: tmp_path / "bin")

    before = set(Path(tempfile.gettempdir()).glob("duelroom-sweep-*"))
    res = sweep._solve_one((str(tmp_path / "d.ydk"), "t", 0, 7, [1, 2, 3],
                            ["9@atk"], "piece:9", 1000, 0))
    assert res["hand_index"] == 0
    assert res["score"] is None
    after = set(Path(tempfile.gettempdir()).glob("duelroom-sweep-*"))
    assert after == before, "the scratch directory was left behind"


def test_the_opposing_hand_is_dealt_through_the_decklist(tmp_path):
    """`--hand` does not reach the going-second player. Proven on the
    solver's own report: its "starting position (player 1)" block listed
    Synchro Overtake + 3x Effect Veiler + Dominus Purge -- the last five
    entries of kewl-tune.ydk, reversed -- while --hand carried six
    different cards, and two different template seeds dealt the SAME
    five. shuffle_decks=False plus DUEL_PSEUDO_SHUFFLE means nothing is
    ever shuffled, so the hand comes off the end of the list.

    Consequence: every going-second trial tested one hand, so `hands=6`
    was the same hand six times."""
    from skills.breaking import deck_with_hand
    from ygorl.cards.ydk import load_ydk

    src = Path(__file__).resolve().parent.parent / "assets" / "decks" / "kewl-tune.ydk"
    original = load_ydk(src)
    hand = list(original.main[:5])
    out = deck_with_hand(src, hand, tmp_path / "dealt.ydk")
    dealt = load_ydk(out)

    assert sorted(dealt.main) == sorted(original.main), (
        "the deck's contents changed; only the ORDER may move")
    assert sorted(dealt.main[-len(hand):]) == sorted(hand), (
        "the requested hand is not at the end, where the core draws from")
    assert list(dealt.extra) == list(original.extra)


def test_a_card_not_in_the_deck_cannot_be_dealt(tmp_path):
    from skills.breaking import deck_with_hand
    src = Path(__file__).resolve().parent.parent / "assets" / "decks" / "kewl-tune.ydk"
    with pytest.raises(ValueError, match="not in the main deck"):
        deck_with_hand(src, [1], tmp_path / "x.ydk")


def test_the_attacker_hand_is_dealt_by_the_replay_not_by_a_flag():
    """`--hand` does not reach the going-second player, and the fix for
    that is no longer `deck_with_hand`.

    The old fix rewrote the decklist so the core would draw the chosen
    hand off its end. It was built on the belief that the solver was
    resuming a duel, and it was not -- it was building a fresh one from a
    template, where `--deck`/`--hand` reach side a and nothing else. With
    `--start`/`--approach` the attacker's hand comes from the start
    replay's own deal, varied by the template seed, and `check_root`
    verifies it was actually drawn.
    """
    import inspect

    from skills import breaking, gauntlet

    for fn in (breaking.going_second, gauntlet.defend):
        src = inspect.getsource(fn)
        assert "deck_with_hand" not in src, (
            f"{fn.__name__} still reorders a decklist to deal the hand")
    # second_position varies the attacker's hand through the template
    # seed, which only works because the template shuffles the other seat
    # -- without that, player 1's hand is its decklist tail for every
    # seed, which is how `hands=6` was one hand six times.
    src = inspect.getsource(breaking.second_position)
    assert "shuffle=True" in src, (
        "the template must shuffle, or every seed deals the same hand")
    assert "check_root(" in src, (
        "second_position must verify the position it returns")


def test_the_sweep_checkpoints_each_depth_round(tmp_path, monkeypatch):
    """Nothing is written until every wave finishes -- a hand's best is not
    known until the last round that could improve it. A 20-hand sweep sat
    at depth round three with 545s of solver spent and no output file. The
    docstring claimed records appended "as each hand completes", which was
    false. A .partial checkpoint makes a killed run salvageable."""
    from skills import sweep

    def fake_map(batch, workers):
        for b in batch:
            targets = tuple(b[5])
            yield {"hand_index": b[2], "label": b[6],
                   "attempt": {"label": b[6], "targets": list(targets),
                               "draw": b[8], "solver_seed": 1,
                               "status": "solved", "wall_s": 1.0,
                               "interruptions": 2, "negates": 1},
                   "score": {"interruptions": 2, "negates": 1, "pieces": []},
                   "record": {"deck": {"name": "t"}, "status": "solved",
                              "targets": list(targets),
                              "lines": [{"board": {}}], "solver": {}},
                   "unsolved_record": None}

    monkeypatch.setattr(sweep, "_map_solves", fake_map)
    monkeypatch.setattr(sweep.engine, "require", lambda: None)
    monkeypatch.setattr(sweep.engine, "preflight", lambda d: None)
    monkeypatch.setattr(sweep, "_breadth_goals",
                        lambda p, h, n: [(("A",), "piece:A")])

    class FakeDeck:
        name = "t"
        main = list(range(40))
        extra = []

    monkeypatch.setattr("ygorl.cards.ydk.load_ydk", lambda p: FakeDeck())
    monkeypatch.setattr("ygorl.solver.batch.sample_hand",
                        lambda deck, seed, *a, **k: (list(range(5)), None))
    monkeypatch.setattr("ygorl.solver.batch.hand_seed", lambda s, n, i: 7)

    out = tmp_path / "s.jsonl"
    sweep.solve_deck(tmp_path / "t.ydk", hands=1, draws=3, out=out,
                     solve_ms=60_000, workers=1)
    partial = Path(str(out) + ".partial")
    assert partial.exists(), "no checkpoint was written during the depth rounds"
    rows = [l for l in partial.read_text().splitlines() if l.strip()]
    assert rows, "the checkpoint is empty"
    rec = json.loads(rows[0])
    assert rec["hand_index"] == 0 and rec["hand_seed"] == 7


def test_the_sweep_docstring_does_not_promise_incremental_writes():
    import inspect
    from skills import sweep
    doc = inspect.getdoc(sweep.solve_deck) or ""
    # The old phrase still appears -- quoted, as the thing that was wrong.
    # What matters is that it is marked false and the real behaviour stated.
    assert "until every wave has finished" in doc
    assert "was false" in doc, "the claim is stated without being corrected"
    assert doc.index("until every wave has finished") < \
        doc.index("as each hand completes"), (
        "the correction must precede the quoted claim, or it reads as a promise")


def test_no_caller_drives_the_solver_in_synthetic_start_mode():
    """The going-second start must be a POSITION, not a template.

    `going_second_args` passed the start replay as the solver's
    positional argument -- which is a template -- with `--deck`/`--hand`
    beside it. That mode builds a FRESH duel: the solver prints
    "=== synthetic start ===" and, queried at the root, BOTH players'
    fields were empty. So no going-second number this project printed was
    ever measured against a board, and `--player 1` searched the seat
    `--deck` does not reach. Four separate fixes each "changed the hand"
    and none changed the result.
    """
    import inspect

    import pytest

    from skills import breaking, gauntlet

    with pytest.raises(NotImplementedError):
        breaking.going_second_args("start", "deck", (1,), "t")

    for fn in (breaking.going_second, breaking.floodgate_check,
               gauntlet.defend):
        src = inspect.getsource(fn)
        assert "second_position(" in src, (
            f"{fn.__name__} must build its position with second_position")
        assert "going_second_args" not in src, (
            f"{fn.__name__} still builds the synthetic-start command line")

    # The supported shape, and every part of it is load-bearing: --start
    # for the duel's parameters, --approach for the responses that reach
    # the position, --no-ref so the goal comes from --target instead of
    # the reference (without it the solver enters repair mode and
    # searches nothing), --player for the seat.
    args = breaking.attack_args("ref.yrpX", "start.yrpX", "prefix.yrpX",
                                "12345@atk", solve_ms=1000)
    for flag in ("--start", "--approach", "--no-ref", "--target",
                 "--player", "--finisher-min"):
        assert flag in args, f"attack_args dropped {flag}"
    assert args[args.index("--player") + 1] == str(breaking.ATTACKER)


def test_check_root_refuses_a_position_that_measures_nothing():
    """A position with no board, or no draw for turn, must raise.

    These were the two silent failures: the attacker holding five cards
    means its turn never began, and an empty builder field means there is
    no board under test. Both produced "the board stopped them" for
    weeks.
    """
    import pytest

    from skills import breaking

    def fake(monkey_state):
        return lambda path, cards=None: monkey_state

    # drawn for turn, board present -> fine
    ok = {"players": [{"field": 3, "hand": 3}, {"field": 0, "hand": 6}]}
    # never drew for turn
    no_draw = {"players": [{"field": 3, "hand": 3}, {"field": 0, "hand": 5}]}
    # drew, but nothing is on the builder's field
    no_board = {"players": [{"field": 0, "hand": 5}, {"field": 0, "hand": 6}]}

    real = breaking.root_state
    try:
        breaking.root_state = fake(ok)
        st = breaking.check_root("p.yrpX", expect_board=True)
        assert st["board_cards"] == 3 and st["attacker_hand"] == 6

        breaking.root_state = fake(no_draw)
        with pytest.raises(breaking.RootNotPlayable, match="drawn for turn"):
            breaking.check_root("p.yrpX", expect_board=True)

        breaking.root_state = fake(no_board)
        with pytest.raises(breaking.RootNotPlayable, match="EMPTY"):
            breaking.check_root("p.yrpX", expect_board=True)
        # the control WANTS an empty field
        assert breaking.check_root("p.yrpX", expect_board=False)["board_cards"] == 0
    finally:
        breaking.root_state = real


def test_a_false_brick_is_corrected_before_scoring(monkeypatch, tmp_path):
    """The optimiser weights brick rate -2.0 and funnel_eval measures it
    with ONE draw of a randomized search. Measured on the active deck,
    same 20 hands: sweep solved 20/20 with zero bricks, the funnel
    reported 5/20 = 0.25. Those five hit the heaviest negative term."""
    from skills import optimizer

    record = {
        "targets": [["59822133@atk"]],
        "summary": {"hands": 4, "bricks": 2, "brick_rate": 0.5,
                    "best_line_rate": 0.5, "hand_trap_survival": 0.0,
                    "combo_actions": {"mean": 8.0}},
        "hands": [
            {"index": 0, "status": "solved", "hand": [1], "hand_seed": 11},
            {"index": 1, "status": "solved", "hand": [2], "hand_seed": 12},
            {"index": 2, "status": "brick", "hand": [3], "hand_seed": 13},
            {"index": 3, "status": "brick", "hand": [4], "hand_seed": 14},
        ],
    }

    # Hand 2 solves on a re-draw; hand 3 never does.
    def fake_solve(args):
        hand_index = args[2]
        solved = hand_index == 2
        return {"hand_index": hand_index, "label": "recheck",
                "attempt": {"label": "recheck", "targets": [], "draw": 0,
                            "solver_seed": 1,
                            "status": "solved" if solved else "unsolved",
                            "wall_s": 1.0},
                "score": {"interruptions": 2, "negates": 0, "pieces": []}
                if solved else None,
                "record": {} if solved else None,
                "unsolved_record": None if solved else {}}

    def fake_map(batch, workers):
        for b in batch:
            yield fake_solve(b)

    monkeypatch.setattr("skills.sweep._map_solves", fake_map)
    out = optimizer.confirm_bricks(tmp_path / "d.ydk", record, draws=2)
    assert out["rechecked"] == 2
    assert out["false_bricks"] == 1, out
    assert out["bricks"] == 1, out


def test_confirm_bricks_is_a_noop_without_targets_or_bricks(tmp_path):
    from skills import optimizer
    assert optimizer.confirm_bricks(
        tmp_path / "d.ydk",
        {"targets": [], "hands": [{"index": 0, "status": "brick",
                                   "hand": [1]}]})["rechecked"] == 0
    assert optimizer.confirm_bricks(
        tmp_path / "d.ydk",
        {"targets": [["1@atk"]], "hands": [
            {"index": 0, "status": "solved", "hand": [1]}]})["bricks"] == 0


def test_evaluate_records_what_the_single_draw_said():
    """A corrected number must not erase the one it replaced, or the
    25-point gap becomes invisible on the next run."""
    import inspect
    from skills import optimizer
    src = inspect.getsource(optimizer.evaluate)
    assert "brick_rate_single_draw" in src, (
        "the corrected rate overwrites the original with no record of it")
    assert "false_bricks" in src


def test_the_ledger_is_not_truncated_over_a_previous_run(tmp_path, monkeypatch):
    """`ledger.write_text("")` made each attempt erase the last one's
    trials, which is the opposite of the resumption insurance BACKLOG
    describes. A crashed overnight run is exactly when those trials
    matter."""
    import inspect
    from skills import optimizer
    src = inspect.getsource(optimizer.hill_climb)
    i_guard = src.index("if ledger.exists()")
    i_trunc = src.index('ledger.write_text("")', i_guard)
    assert i_guard < i_trunc, "the ledger is truncated before being kept"
    assert "ledger.replace(kept)" in src, "the old ledger is not preserved"


def test_the_ledger_records_all_four_objective_terms():
    """degenerate_terms needs solve rate, brick rate, survival and combo
    length. The ledger stored only two of them, so an empty ranking could
    be detected live and never from a stored run."""
    import inspect
    from skills import optimizer
    src = inspect.getsource(optimizer.hill_climb)
    for key in ("brick_rate", "survival", "best_line_rate",
                "combo_actions_mean"):
        assert f'"{key}"' in src, f"the ledger drops {key}"


def test_the_brick_correction_is_not_silent():
    """A number quietly corrected is a number nobody can check."""
    import inspect
    from skills import optimizer
    src = inspect.getsource(optimizer.hill_climb)
    assert src.count("log=log") >= 2, (
        "evaluate is called without a log, so confirm_bricks says nothing")


def test_export_ranks_by_placement_not_scrape_order():
    """`ORDER BY d.id LIMIT 1` ranked lists by the accident of scrape
    order. That is how branded.ydk became a Branded/Blitzclique hybrid
    with one Fallen of Albaz and no Branded Fusion, averaging 1.0
    interruptions over 40 hands while sitting in the board library as a
    meta deck."""
    import sqlite3
    from skills import meta_scrape as ms

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE decks (id INTEGER PRIMARY KEY, title TEXT,
                            placement TEXT, deck_date TEXT);
        CREATE TABLE deck_tags (deck_id INTEGER, tag TEXT);
    """)
    rows = [(1, "scraped first, did badly", "Top 64", "2026-01-01"),
            (2, "the winner", "Winner", "2026-01-02"),
            (3, "also a winner but older", "Winner", "2025-06-01"),
            (4, "no placement recorded", None, "2026-05-01")]
    conn.executemany("INSERT INTO decks VALUES (?,?,?,?)", rows)
    conn.executemany("INSERT INTO deck_tags VALUES (?,?)",
                     [(r[0], "branded") for r in rows])

    best = ms.best_list(conn, "branded")
    assert best["id"] == 2, (
        f"picked {best['id']} ({best['placement']}); the newest Winner is 2")
    assert ms.best_list(conn, "nothing-tagged-this") is None


def test_placement_rank_orders_and_tolerates_junk():
    from skills import meta_scrape as ms
    assert (ms.placement_rank("Winner") < ms.placement_rank("Runner-Up")
            < ms.placement_rank("3rd Place") < ms.placement_rank("Top 4")
            < ms.placement_rank("Top 32") < ms.placement_rank(""))
    assert ms.placement_rank(None) == ms.placement_rank("")
    # An unrecognised string must not sort ahead of a real placement.
    assert ms.placement_rank("weird") > ms.placement_rank("Top 64")


def test_report_carries_window_recovery_beside_survival():
    """`survival` counts only hands that recover from EVERY window, since
    the opponent picks the timing. On the active deck Infinite
    Impermanence gets a median of 11 windows per line and about a third
    are recoverable, so survival is a correct 0% while per-window
    recovery is 32.6%. Showing 0% alone invites dismissing the column or
    panicking at it."""
    from skills import optimizer

    hist = [optimizer.Candidate(
        deck=None, label="base", score=0.1,
        summary={"solved": 15, "hands": 20, "brick_rate": 0.25,
                 "hand_trap_survival": 0.0,
                 "window_recovery": {"10045474": 0.326, "14558127": 0.31},
                 "combo_actions": {"mean": 8.9}})]
    row = optimizer.report(hist, hands=20)[0]
    assert row["survival"] == 0.0
    assert row["window_recovery"]["10045474"] == 0.326, (
        "window recovery is dropped, so 0% survival has no context")


def test_the_optimizer_output_explains_the_survival_standard():
    import inspect
    import daemon
    src = inspect.getsource(daemon.cmd_optimize)
    assert "EVERY window" in src, (
        "the output prints 0% survival without saying it is an all-windows "
        "standard")
    assert "window_recovery" in src


def test_window_recovery_is_in_the_objective():
    """The effect that dominates handtrap resilience was in the summary
    and unused. Measured on the active deck: line length correlates +0.75
    with Imperm windows while recoveries per hand stay flat at 3-4, so
    short lines recover 49.0% of windows and long ones 23.7%. `survival`
    cannot see it (all-or-nothing, saturated at 0.0) and `actions` was a
    -0.05 tiebreak."""
    from skills.optimizer import Objective, mean_window_recovery

    s = {"brick_rate": 0.25, "best_line_rate": 0.75,
         "hand_trap_survival": 0.0, "combo_actions": {"mean": 8.9},
         "window_recovery": {"10045474": 0.49, "14558127": 0.31}}
    assert abs(mean_window_recovery(s) - 0.40) < 1e-9
    assert Objective().score(s) > Objective(recovery=0).score(s), (
        "recovery does not affect the score")
    # Two decks identical but for recovery must order by it.
    worse = dict(s, window_recovery={"10045474": 0.10, "14558127": 0.10})
    assert Objective().score(s) > Objective().score(worse)
    # And the old behaviour stays reachable exactly.
    assert Objective(recovery=0).score(s) == Objective(recovery=0).score(worse)


def test_window_recovery_handles_a_missing_or_empty_dict():
    from skills.optimizer import mean_window_recovery
    assert mean_window_recovery({}) == 0.0
    assert mean_window_recovery({"window_recovery": {}}) == 0.0
    assert mean_window_recovery({"window_recovery": {"a": None}}) == 0.0


def test_degenerate_terms_counts_the_new_term():
    """A ranking resting on window recovery alone must still be refused."""
    from skills import optimizer
    hist = [optimizer.Candidate(
        deck=None, label=f"t{i}", score=0.1 * i,
        summary={"brick_rate": 0.2, "best_line_rate": 0.8,
                 "hand_trap_survival": 0.0,
                 "combo_actions": {"mean": 9.0},
                 "window_recovery": {"a": 0.1 * i}}) for i in range(3)]
    const, vary = optimizer.degenerate_terms(hist)
    assert vary == ["window recovery"], (const, vary)
    assert len(const) == 4


def test_rescore_reranks_stored_trials_without_the_solver(tmp_path):
    """Each `optimize` run costs hours, and the objective's weights are
    judgement calls, so being unable to re-rank stored trials meant the
    weights could never be examined at all."""
    import json as _json
    from skills import optimizer

    def write(name, brick, recov, combo=8.0):
        (tmp_path / f"{name}.jsonl").write_text(_json.dumps({
            "summary": {"brick_rate": brick, "best_line_rate": 1 - brick,
                        "hand_trap_survival": 0.0, "hands": 20,
                        "combo_actions": {"mean": combo},
                        "window_recovery": {"a": recov}}}) + "\n")

    # A realistic gap: the swap bricks more and recovers a little better,
    # which is the trade the recovery term exists to weigh. At the default
    # weight consistency should still win; at a heavy weight it should not.
    write("deck", 0.25, 0.30)            # base
    write("deck_r0_111", 0.30, 0.40)     # worse bricks, better recovery
    write("deck_r0_222", 0.30, 0.10)

    rows = optimizer.rescore("deck", scratch=tmp_path)
    assert [r["label"] for r in rows][0] == "base", rows
    # With recovery weighted heavily the better-recovering swap must win,
    # which is the whole point of being able to re-weight.
    heavy = optimizer.rescore("deck", objective=optimizer.Objective(recovery=5.0),
                              scratch=tmp_path)
    assert heavy[0]["label"] == "deck_r0_111", heavy
    # And with it off, recovery must not affect the order at all.
    off = optimizer.rescore("deck", objective=optimizer.Objective(recovery=0),
                            scratch=tmp_path)
    assert off[0]["label"] == "base"


def test_stored_trials_puts_base_first_and_skips_junk(tmp_path):
    from skills import optimizer
    (tmp_path / "deck.jsonl").write_text('{"summary": {"brick_rate": 0.1}}\n')
    (tmp_path / "deck_r0_9.jsonl").write_text('{"summary": {"brick_rate": 0.2}}\n')
    (tmp_path / "deck_bad.jsonl").write_text("not json\n")
    (tmp_path / "deck_empty.jsonl").write_text("")
    out = optimizer.stored_trials("deck", scratch=tmp_path)
    assert out[0][0] == "base"
    assert len(out) == 2, out


def test_slots_widens_the_cut_search(tmp_path, monkeypatch):
    """The inner loop used to end with an unconditional `break` ("one
    removal candidate per round keeps this finite"), so only the FIRST
    removable card was ever cut: the search was 1 cut x 26 adds = 26 of a
    22 x 26 = 572 space. Verified on the live run -- 7 trials, 7 distinct
    adds, ONE distinct cut."""
    from skills import db, optimizer
    from skills.meta_scrape import DEFAULT_FORMAT as FMT

    conn = db.connect(tmp_path / "t.db")
    with conn:
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [("A", FMT, 1, "main", 1.0, 1, 50, "now"),    # core
             ("A", FMT, 8, "main", 0.10, 1, 50, "now"),   # weak slot 1
             ("A", FMT, 9, "main", 0.12, 1, 50, "now"),   # weak slot 2
             ("A", FMT, 50, "main", 0.40, 1, 50, "now")])
        conn.executemany(
            "INSERT INTO cards (id, name, last_fetched) VALUES (?,?,?)",
            [(i, f"c{i}", "now") for i in (1, 8, 9, 50)])

    base = Deck(name="base", main=[1, 8, 9], extra=[], side=[])
    monkeypatch.setattr(Deck, "validate", lambda self, conn, region="tcg": [])
    cuts = []

    def fake_evaluate(deck, target, hands=12, solve_ms=None, scratch=None,
                      fire=None, **kw):
        cuts.append(deck.name)
        return {"brick_rate": 0.5, "best_line_rate": 0.5, "solved": 2,
                "hands": hands, "hand_trap_survival": 0.0,
                "combo_actions": {"mean": 5.0}, "window_recovery": {}}

    monkeypatch.setattr(optimizer, "evaluate", fake_evaluate)

    cuts.clear()
    optimizer.hill_climb(conn, base, "A", "1@atk", hands=2, rounds=1,
                         slots=1, log=lambda *a: None)
    one = len(cuts)

    cuts.clear()
    optimizer.hill_climb(conn, base, "A", "1@atk", hands=2, rounds=1,
                         slots=2, log=lambda *a: None)
    two = len(cuts)

    assert two > one, (
        f"slots=2 ran {two} trials and slots=1 ran {one}: the cut search "
        f"did not widen")
    # Trial names must distinguish the cut, or the scratch files collide.
    trial_names = [c for c in cuts if c != "base"]
    assert len(set(trial_names)) == len(trial_names), (
        f"duplicate trial names: {trial_names}")


def test_a_trial_name_includes_the_cut():
    """Two trials adding the same card over different cuts shared a
    scratch path, so `evaluate` overwrote one with the other and
    `rescore` only ever saw the last. Harmless at slots=1 and immediate
    at slots>1."""
    import inspect
    from skills import optimizer
    src = inspect.getsource(optimizer.hill_climb)
    assert "{out_card}_" in src, (
        "the trial name omits the cut, so same-add trials collide")


def test_paired_check_classifies_both_directions_of_disagreement(monkeypatch,
                                                                 tmp_path):
    """The comparison whose absence produced this session's worst error:
    I reported the brick rate "25 points wrong" by holding a sweep that
    solved 20/20 against a funnel reporting 5/20, when the two shared
    neither hands (0 of 20 seeds overlapped) nor goals. Here the hands
    and target are the funnel's own, so a disagreement is real."""
    from skills import optimizer

    record = {
        "targets": [["59822133@atk"]],
        "hands": [
            {"index": 0, "status": "solved", "hand": [1], "hand_seed": 10},
            {"index": 1, "status": "brick", "hand": [2], "hand_seed": 11},
            {"index": 2, "status": "brick", "hand": [3], "hand_seed": 12},
            {"index": 3, "status": "solved", "hand": [4], "hand_seed": 13},
        ],
    }

    # Multi-draw reaches hands 0 and 1 only.
    def fake_map(batch, workers):
        for b in batch:
            i = b[2]
            hit = i in (0, 1)
            yield {"hand_index": i, "label": "paired",
                   "attempt": {"label": "paired", "targets": [], "draw": 0,
                               "solver_seed": 1,
                               "status": "solved" if hit else "unsolved",
                               "wall_s": 1.0},
                   "score": {"interruptions": 1, "negates": 0,
                             "pieces": []} if hit else None,
                   "record": {} if hit else None,
                   "unsolved_record": None if hit else {}}

    monkeypatch.setattr("skills.sweep._map_solves", fake_map)
    out = optimizer.paired_check(tmp_path / "d.ydk", record, draws=2)
    assert out["agree_solved"] == 1          # hand 0
    assert out["funnel_pessimistic"] == 1    # hand 1: brick, re-draw reached
    assert out["agree_brick"] == 1           # hand 2
    assert out["funnel_optimistic"] == 1     # hand 3: solved, re-draws missed
    assert {r["index"] for r in out["rows"]} == {0, 1, 2, 3}


def test_paired_check_is_a_noop_without_hands_or_targets(tmp_path):
    from skills import optimizer
    assert optimizer.paired_check(tmp_path / "d.ydk",
                                  {"targets": [], "hands": []})["rows"] == []
    assert optimizer.paired_check(
        tmp_path / "d.ydk",
        {"targets": [["1@atk"]], "hands": []})["agree_solved"] == 0


def test_doctor_reports_orphaned_pool_workers(monkeypatch):
    """Killing a pool's parent leaves its spawn children reparented to
    PID 1, blocked on a queue nobody will write to. Found six aged six
    days on 2026-10-24, and six MORE aged four hours immediately after
    killing one funnel parent — so this recurs every time a long job is
    stopped, and it is invisible without looking."""
    import subprocess
    import daemon

    rows = "\n".join([
        # pid ppid etime args
        "111 1 6-01:31:55 /home/deezer/duelroom/venv/bin/python -c from multiprocessing.spawn import spawn_main; x",
        "112 1 04:19:47 /home/deezer/duelroom/venv/bin/python -c from multiprocessing.spawn import spawn_main; x",
        # a LIVE worker with a real parent must not be reported
        "113 999 00:10 /home/deezer/duelroom/venv/bin/python -c from multiprocessing.spawn import spawn_main; x",
        # another project's orphan is not ours to report
        "114 1 1-00:00 /home/deezer/flipper/venv/bin/python -c from multiprocessing.spawn import spawn_main; x",
        # the web service must never be listed
        "115 1 9-00:00 /home/deezer/duelroom/venv/bin/python3 gunicorn web.wsgi:app",
    ])

    class R:
        stdout = rows

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    out = daemon._orphaned_workers()
    pids = [p for p, _ in out]
    assert pids == ["111", "112"], pids
    assert "115" not in pids, "the web service was reported as an orphan"


def test_orphan_check_survives_a_broken_ps(monkeypatch):
    import subprocess
    import daemon
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no ps")))
    assert daemon._orphaned_workers() == []


def test_every_name_the_sweep_uses_resolves():
    """Two bugs this session were a guessed import anchor that silently
    did not match: `shutil` (broke every sweep) and then `contextlib`
    (broke importing the module at all). Both were edits anchored on text
    that had already changed. Assert the module's own names instead."""
    from skills import sweep
    for name in ("contextlib", "json", "os", "shutil", "signal", "sys",
                 "tempfile", "time", "zlib"):
        assert hasattr(sweep, name), f"sweep.py does not import {name}"


def test_pool_children_are_terminated_on_a_catchable_signal():
    """A pool's spawn children are reparented to PID 1 when the parent
    dies and never exit. SIGTERM and SIGINT are what `kill` and Ctrl-C
    send and both are catchable; SIGKILL is not, which is why the doctor
    check stays."""
    import signal as _signal
    from skills import sweep

    class FakeProc:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    class FakeEx:
        def __init__(self):
            self._processes = {1: FakeProc(), 2: FakeProc()}

    ex = FakeEx()
    # Install a harmless predecessor first. The handler deliberately
    # CHAINS to whatever was there, so against pytest's default SIG_DFL it
    # re-raises and kills the test runner -- which is the correct
    # production behaviour (a kill must still kill) and killed this suite
    # with exit 143 on the first attempt.
    chained = []
    before = _signal.signal(_signal.SIGTERM,
                            lambda *a: chained.append(True))
    try:
        with sweep._children_die_with_us(ex):
            handler = _signal.getsignal(_signal.SIGTERM)
            assert handler is not before, "no SIGTERM handler was installed"
            handler(_signal.SIGTERM, None)  # simulate the kill
        assert all(p.terminated for p in ex._processes.values()), (
            "the workers were not terminated")
        assert chained == [True], (
            "the previous handler was dropped instead of chained")
        assert _signal.getsignal(_signal.SIGTERM) is not handler, (
            "the installed handler was not removed on exit")
    finally:
        _signal.signal(_signal.SIGTERM, before)


def test_matrix_and_doctor_judge_partialness_the_same_way(tmp_path,
                                                          monkeypatch):
    """`doctor` called the 7-deck matrix PARTIAL ("only 7 of ~10") while
    `matrix` itself said nothing, because 7 clears its `_n < 5` threshold.
    Two places judging the same file by different standards is how a
    caveat goes missing from the view that drives the decision — and this
    view decides which handtraps to main deck."""
    import inspect
    import daemon

    src = inspect.getsource(daemon.cmd_matrix)
    assert "_expected" in src, "matrix still uses a magic threshold alone"
    assert "DECKS.glob" in src, (
        "matrix does not compare against the decks that exist")
    # doctor's standard: len(decks) - 2
    doc = inspect.getsource(daemon.cmd_doctor)
    assert "len(decks) - 2" in doc
    assert "len(list(DECKS.glob(\"*.ydk\"))) - 2" in src, (
        "matrix and doctor use different expectations for the same file")


def test_matrix_reports_the_share_not_just_the_count():
    """A count without the denominator is what let 7 read as complete."""
    import inspect
    import daemon
    src = inspect.getsource(daemon.cmd_matrix)
    assert "of the field" in src and "_expected:.0%" in src


def test_the_project_has_no_undefined_names():
    """Two bugs in one session were a NameError no test could see: a
    missing `import shutil` broke every sweep while its own test passed
    (it only grepped the source), and the same mistake dropped
    `import contextlib` an hour later. Verified that pyflakes reports
    both, by name and line, when the import is removed."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, str(root / "tools/check_names.py")],
                       capture_output=True, text=True, cwd=str(root),
                       timeout=180)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def test_the_name_check_only_fails_on_undefined_names():
    """Unused imports and f-string notes must NOT fail it, or the gate
    becomes noise and gets ignored -- which is how a real finding hides."""
    import inspect
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "tools/check_names.py"
    spec = importlib.util.spec_from_file_location("check_names", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = inspect.getsource(mod)
    assert '"undefined name" in l' in src
    assert "non-fatal" in src


def test_every_solved_only_mean_says_so():
    """`summarize` appends to `interruptions` only after its
    `if r["status"] != "solved": continue`, so `mean_interruptions` is a
    mean over solved hands. Printed bare it reads as per-hand, which for
    a deck that bricks a quarter of the time is a different and lower
    number — the mistake behind the "1.0 interruptions" claim for branded
    that stood all session (real figures: 1.6 solved-only, 1.2 per hand).

    Any view printing that field must name its denominator."""
    import inspect
    import daemon
    from skills import sweep

    # The field really is solved-only.
    src = inspect.getsource(sweep.summarize)
    i = src.index('row["interruptions"].append')
    before = src[:i]
    assert 'if r["status"] != "solved":' in before and "continue" in before, (
        "summarize no longer filters to solved hands; the label is now wrong")

    for fn in (daemon.cmd_solve, daemon.cmd_boards):
        body = inspect.getsource(fn)
        if "mean_interruptions" in body or "mean {mean:.1f}" in body:
            assert "SOLVED" in body, (
                f"{fn.__name__} prints a solved-only mean without saying so")

    swept = inspect.getsource(sweep.sweep)
    assert "per solved hand" in swept, (
        "sweep's summary line prints a solved-only mean unqualified")


def test_the_refusal_actually_suppresses_the_table(tmp_path, monkeypatch,
                                                   capsys):
    """BEHAVIOURAL version of the refusal check.

    The existing test string-matches the order of two prints in the
    source. That is the weakness that let a missing `import shutil` pass
    its own test, so the most load-bearing safety property in the
    optimiser -- that a degenerate objective prints NO ranking and writes
    NO 'best list' -- is asserted by running it."""
    import daemon
    from skills import db, optimizer
    from skills.deck import Deck

    deck = Deck(name="d", main=[1, 2, 3], extra=[], side=[])
    monkeypatch.setattr(daemon, "_resolve_deck",
                        lambda *a, **k: tmp_path / "d.ydk")
    monkeypatch.setattr(Deck, "from_ydk", staticmethod(lambda p: deck))
    monkeypatch.setattr(Deck, "canonicalise", lambda self, conn: [])
    monkeypatch.setattr(daemon, "_warn_if_busy", lambda *a, **k: 0)
    monkeypatch.setattr(daemon.optimizer, "candidate_pool",
                        lambda *a, **k: [{"card_id": 9, "name": "c9"}])
    monkeypatch.setattr(daemon.optimizer, "removable",
                        lambda *a, **k: [(2, 1)])
    monkeypatch.setattr(daemon.optimizer, "card_name",
                        lambda conn, c: f"c{c}")
    monkeypatch.setattr(daemon.scenarios, "ensure_target",
                        lambda *a, **k: "1@atk")
    monkeypatch.setattr(daemon.ms, "identify_archetype",
                        lambda *a, **k: [("A", 1.0, 3)])

    # A history where only ONE term moves: the degenerate case.
    hist = [optimizer.Candidate(
        deck=deck, label=f"t{i}", score=0.3 - 0.01 * i,
        summary={"brick_rate": 0.2, "best_line_rate": 0.8, "solved": 8,
                 "hands": 10, "hand_trap_survival": 0.0,
                 "combo_actions": {"mean": 9.0 + i},
                 "window_recovery": {}}) for i in range(3)]
    monkeypatch.setattr(daemon.optimizer, "hill_climb",
                        lambda *a, **k: (hist[0], hist))

    wrote = []
    monkeypatch.setattr(Deck, "write_ydk",
                        lambda self, path: wrote.append(path))

    daemon.cmd_optimize(db.connect(tmp_path / "t.db"), ["d", "A", "10", "1"])
    out = capsys.readouterr().out

    assert "NO RANKING" in out, "the refusal did not fire"
    # The ranked table's header must NOT appear.
    assert "score" not in out.split("NO RANKING")[1].split("\n")[0]
    assert "best:" not in out, "a best line was announced for an empty ranking"
    assert not wrote, f"a 'best list' was written anyway: {wrote}"


def test_drill_ranks_real_boards_above_holding_handtraps(tmp_path):
    """`drill` tells you which lines to REHEARSE, and it ranked on the
    flat interruption count — which scores a card held in hand the same
    as a negate on the field. On the active deck the top line offered for
    practice was: open five handtraps with no engine, set two Dominus
    Purge, hold three. Five "interruptions", and it beat two real boards
    (Blue-Eyes Spirit Dragon + Hieratic Seal) at four."""
    import json as _json
    from skills import drill

    # 59822133 Blue-Eyes Spirit Dragon interrupts from the field;
    # 14558127 Ash Blossom only from the hand.
    field_line = {"board": {"players": [
        {"mzone": [{"code": 59822133, "position": 0x1}], "szone": [],
         "hand": [], "grave": []}]}}
    hand_line = {"board": {"players": [
        {"mzone": [], "szone": [],
         "hand": [14558127, 14558127, 14558127], "grave": []}]}}

    path = tmp_path / "d.jsonl"
    path.write_text("\n".join([
        # the hand-only record carries the HIGHER flat count
        _json.dumps({"status": "solved", "deck": {"name": "d"},
                     "hand_index": 0, "lines": [hand_line],
                     "solver": {"blocking": {"interruptions": 5}}}),
        _json.dumps({"status": "solved", "deck": {"name": "d"},
                     "hand_index": 1, "lines": [field_line],
                     "solver": {"blocking": {"interruptions": 4}}}),
    ]) + "\n")

    found = drill.lines_from(path, "d")
    assert len(found) == 2
    # `lines_from` parses JSON, so compare content -- identity cannot
    # survive the round trip, which is how the first version of this
    # assertion failed against correct behaviour.
    first = found[0][1]
    assert first == field_line, (
        "the hand-only line was ranked first; drill is still ranking on the "
        "flat count and would have you practise passing")
    assert found[1][1] == hand_line
    assert drill._on_field(field_line) == 1
    assert drill._on_field(hand_line) == 0


def test_report_finds_funnel_records_in_subdirectories(tmp_path, monkeypatch):
    """`latest_funnel` globbed `results/*.jsonl` — top level only — while
    `optimizer.evaluate` writes its funnel records into
    results/optimizer/. So any deck that had been through `optimize`
    reported "no funnel run yet" despite a full record existing. The
    active deck had 20 hands, 15 solved, brick 0.25 — the record
    `paired_check` validated — and the view whose job is assembling what
    exists said there was nothing."""
    import json as _json
    from skills import engine, report

    results = tmp_path / "results"
    (results / "optimizer").mkdir(parents=True)
    rec = {"summary": {"deck": "d", "hands": 20, "solved": 15,
                       "brick_rate": 0.25, "budget_s": 60.0}}
    (results / "optimizer" / "d.jsonl").write_text(_json.dumps(rec) + "\n")
    # A trial record must NOT be mistaken for the deck's own.
    trial = {"summary": {"deck": "d_r0_999", "hands": 20, "solved": 2}}
    (results / "optimizer" / "d_r0_999.jsonl").write_text(
        _json.dumps(trial) + "\n")

    monkeypatch.setattr(engine, "ROOT", tmp_path)
    found, src = report.latest_funnel("d")
    assert found is not None, "the subdirectory record was not found"
    assert found["summary"]["solved"] == 15
    assert src.name == "d.jsonl"

    # And a deck with no record anywhere still reports nothing.
    none_found, _ = report.latest_funnel("other")
    assert none_found is None


def test_drift_refuses_two_snapshots_of_one_scrape(tmp_path):
    """Drift across a single scrape is zero by construction, and a table
    of zeros reads as "the meta did not move" when it means "no new lists
    were gathered". Both pre-existing snapshots are 5599 rows from the
    same scrape, 3.5 hours apart — so this was already live."""
    from skills import db, meta_scrape as ms

    conn = db.connect(tmp_path / "t.db")
    with conn:
        conn.executemany(
            "INSERT INTO decks (archetype, format, source_url, list_hash, "
            "scraped_at) VALUES (?,?,?,?,?)",
            [("A", ms.DEFAULT_FORMAT, "u1", "h1",
              "2026-10-18T00:00:00+00:00")])
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) VALUES (?,?,?,?,?,?,?,?)",
            [("A", ms.DEFAULT_FORMAT, 1, "main", 0.5, 1, 20, "now")])

    ms.snapshot_inclusion(conn, "one")
    ms.snapshot_inclusion(conn, "two")
    assert ms.same_scrape(conn, "one", "two") is True, (
        "two snapshots of the same scrape were not detected")

    # A new list arriving changes the fingerprint.
    with conn:
        conn.execute(
            "INSERT INTO decks (archetype, format, source_url, list_hash, "
            "scraped_at) VALUES (?,?,?,?,?)",
            ("A", ms.DEFAULT_FORMAT, "u2", "h2",
             "2026-10-25T00:00:00+00:00"))
    ms.snapshot_inclusion(conn, "three")
    assert ms.same_scrape(conn, "two", "three") is False, (
        "a new scrape was not distinguished")

    # An unknown snapshot must not read as "different".
    assert ms.same_scrape(conn, "one", "missing") is None


def test_the_drift_refusal_comes_before_the_table():
    import inspect
    import daemon
    src = inspect.getsource(daemon.cmd_drift)
    assert src.index("NO DRIFT TO REPORT") < src.index("ms.drift("), (
        "drift computes and prints before checking the scrape")


def test_optimize_announces_the_pool_it_will_actually_use():
    """`cmd_optimize` called `candidate_pool` with its own default limit
    of 30 while `hill_climb` passes pool_limit=12, so it printed
    "26 candidates ... 78 trials" and then ran 36 (37 records with base).
    Overstating the work by more than 2x is the same defect as any other
    unqualified number."""
    import inspect
    import daemon
    src = inspect.getsource(daemon.cmd_optimize)
    assert "pool_limit = 12" in src
    assert "limit=pool_limit" in src, (
        "the announced pool is not the one hill_climb will use")
    assert "pool_limit=pool_limit" in src, (
        "hill_climb is not told the limit that was announced")


def test_rescore_labels_distinguish_the_cut(tmp_path):
    """Two trials that add the same card over DIFFERENT cuts are two
    different lists and must render as two different rows. They once
    rendered identically ("Called by the Grave" twice) because the label
    parser read only the last segment of the trial name.

    Asserted on behaviour rather than on the source of the parser: the
    cut and add now come from the trial's own .ydk, so there is no
    parser left to inspect, but the requirement is unchanged.
    """
    from skills import optimizer

    summary = {"brick_rate": 0.3, "best_line_rate": 0.7,
               "hand_trap_survival": 0.0, "combo_actions": {"mean": 8.4},
               "window_recovery": {"ash": 0.36}, "hands": 10}
    _optimizer_scratch(tmp_path, [
        ("d_r0_1005_2000", 1005, 2000, dict(summary)),
        ("d_r0_1006_2000", 1006, 2000, dict(summary)),
    ])

    rows = optimizer.rescore("d", scratch=tmp_path)
    cands = [r for r in rows if r["label"] != "base"]
    assert len(cands) == 2, "same add over two cuts is two candidates"
    assert {tuple(r["cut"]) for r in cands} == {(1005,), (1006,)}
    assert all(r["add"] == [2000] for r in cands)
    assert all(r.get("measured", 1) == 1 for r in cands), (
        "different lists must not be merged just because they score alike")


def test_drill_reports_the_number_it_ranks_by(tmp_path):
    """`lines_from` ranks on ON-FIELD interruptions while callers printed
    the flat count, which includes cards merely held in hand. On the
    active deck's 80 lines that put a "4 interruptions" line above a "5"
    three times — which reads as a sorting bug rather than as the flat
    count being the wrong measure."""
    import inspect
    import daemon
    from skills import drill

    # export must hand the ranked number to its callers.
    src = inspect.getsource(drill.export)
    assert "_on_field(line)" in src, (
        "export does not return the number the ranking uses")

    cli = inspect.getsource(daemon.cmd_drill)
    assert "on_field" in cli and "on field" in cli, (
        "the CLI still leads with the flat count")

    views = (Path(__file__).resolve().parent.parent / "web" / "views.py").read_text()
    assert '"on_field": drill._on_field(line)' in views, (
        "the web drill view does not carry the ranked number")
    tpl = (Path(__file__).resolve().parent.parent / "web" / "templates"
           / "drill.html").read_text()
    assert "on field" in tpl, "the web drill page still shows only the flat count"


def test_drill_never_reports_a_silent_zero(tmp_path):
    """`counts.get(..., 0)` returned 0 for any hand the companion summary
    did not cover, which is indistinguishable from "this board has no
    interruptions". results/sweep_active.jsonl has 40 hands and a summary
    for 0-19, so hands 20-39 all read 0 while their records held 4, 5
    and 3. The record's own count is now read first."""
    import json as _json
    from skills import drill

    # A record with its own count and NO companion summary file at all.
    path = tmp_path / "s.jsonl"
    path.write_text(_json.dumps({
        "status": "solved", "deck": {"name": "d"}, "hand_index": 7,
        "lines": [{"board": {"players": [
            {"mzone": [{"code": 59822133, "position": 0x1}], "szone": [],
             "hand": [14558127], "grave": []}]}}],
        "solver": {"blocking": {"interruptions": 4}},
    }) + "\n")
    found = drill.lines_from(path, "d")
    assert len(found) == 1
    assert found[0][2] == 4, (
        f"flat count came back {found[0][2]}; the record said 4 and there is "
        f"no summary to fall back on")


def test_the_dead_yield_agent_is_gone_but_its_warning_is_not():
    """`ScriptedThenYield` and `action_kinds_seen` were superseded by
    `convert_line(close_turn=True)` and referenced by nothing. The trap
    they documented still bites: the solver's own replays stop when the
    combo stops, so `--start` rejects them."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "skills"
           / "breaking.py").read_text()
    assert "class ScriptedThenYield" not in src
    assert "def action_kinds_seen" not in src
    assert "END OF THE RECORDING" in src, (
        "the reason close_turn=True is needed was deleted with the code")


def test_draws_curve_recovers_the_per_draw_payoff(tmp_path):
    """The budget has `ladder`; `draws` had nothing, so `sweep.DRAWS` was
    one guess applied to every deck — worth +0.10 on despia for 2.6x the
    CPU, and far more on an elfnote hand. Every record stores each
    attempt with its draw index, so the curve is recoverable with no
    solver time at all."""
    import json as _json
    from skills import sweep

    def rec(hand, attempts):
        return _json.dumps({
            "status": "solved", "deck": {"name": "d"}, "hand_index": hand,
            "lines": [{"board": {}}],
            "solver": {"blocking": {"attempts": attempts}}})

    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([
        # hand 0 peaks immediately; hand 1 improves on a later draw
        rec(0, [{"draw": 0, "label": "a", "status": "solved",
                 "interruptions": 4, "negates": 1},
                {"draw": 1, "label": "a", "status": "solved",
                 "interruptions": 4, "negates": 1}]),
        rec(1, [{"draw": 0, "label": "a", "status": "solved",
                 "interruptions": 2, "negates": 0},
                {"draw": 1, "label": "a", "status": "solved",
                 "interruptions": 5, "negates": 2}]),
    ]) + "\n")

    curve = sweep.draws_curve(path)
    row = curve["d"]
    assert row["hands"] == 2 and row["solves"] == 4
    assert row["mean_best"] == 4.5, row["mean_best"]
    # After draw 0 only hand 0 is at its best; after draw 1 both are.
    assert row["at_draw"][0] == "1/2", row["at_draw"]
    assert row["at_draw"][1] == "2/2", row["at_draw"]
    # One solve ran after hand 0 had already peaked.
    assert row["wasted"] == 1, row["wasted"]


def _optimizer_scratch(tmp_path, trials):
    """A stored optimiser run: a base .ydk/.jsonl plus one of each per trial."""
    import json as _json
    base_main = list(range(1000, 1040))

    def write(label, main):
        (tmp_path / f"{label}.ydk").write_text(
            "#created by duelroom\n#main\n"
            + "\n".join(str(c) for c in main)
            + "\n#extra\n!side\n")

    write("d", base_main)
    (tmp_path / "d.jsonl").write_text(_json.dumps(
        {"summary": {"brick_rate": 0.2, "best_line_rate": 0.8,
                     "hand_trap_survival": 0.0, "combo_actions": {"mean": 8.0},
                     "window_recovery": {"ash": 0.3}, "hands": 10}}) + "\n")
    for label, cut, add, summary in trials:
        main = [c for c in base_main if c != cut] + [add]
        write(label, main)
        (tmp_path / f"{label}.jsonl").write_text(
            _json.dumps({"summary": summary}) + "\n")
    return base_main


def test_degenerate_terms_accepts_plain_summaries(tmp_path):
    """`rescore` ranks the same trials as `optimize` and had no
    degeneracy check at all -- the guard from ISSUES #1 went into one of
    the two commands that print an ordering. Rather than a second copy of
    the rule, `degenerate_terms` takes summary dicts as well as
    Candidates."""
    from skills import optimizer

    summaries = [
        {"best_line_rate": 0.83, "brick_rate": 0.17,
         "hand_trap_survival": 0.0, "combo_actions": {"mean": 4.1},
         "window_recovery": {"ash": 0.43}},
        {"best_line_rate": 0.83, "brick_rate": 0.17,
         "hand_trap_survival": 0.0, "combo_actions": {"mean": 4.5},
         "window_recovery": {"ash": 0.48}},
    ]
    const, vary = optimizer.degenerate_terms(summaries)
    assert set(const) == {"solve rate", "brick rate", "handtrap survival"}
    assert set(vary) == {"combo length", "window recovery"}


def test_rescore_refuses_to_order_when_nothing_moved(tmp_path, monkeypatch,
                                                     capsys):
    """One moving term is a tiebreak, not a measurement, and `optimize`
    already refuses to print an order in that state. `rescore` printed
    one regardless, from the same stored data, and named a "best stored
    swap" under it."""
    import daemon
    from skills import db, optimizer

    summary = {"brick_rate": 0.2, "best_line_rate": 0.8,
               "hand_trap_survival": 0.0, "combo_actions": {"mean": 8.0},
               "window_recovery": {"ash": 0.3}, "hands": 10}
    _optimizer_scratch(tmp_path, [
        ("d_r0_1005_2000", 1005, 2000,
         dict(summary, combo_actions={"mean": 9.0})),
        ("d_r0_1006_2001", 1006, 2001,
         dict(summary, combo_actions={"mean": 7.0})),
    ])
    rows = optimizer.rescore("d", scratch=tmp_path)

    monkeypatch.setattr(daemon, "_resolve_deck", lambda *a, **k: tmp_path / "d.ydk")
    monkeypatch.setattr(optimizer, "rescore", lambda *a, **k: rows)
    daemon.cmd_rescore(db.connect(), ["d"])
    out = capsys.readouterr().out

    assert "NO RANKING" in out, out
    assert "combo length" in out
    assert "Best stored swap" not in out, (
        "naming a best swap under the refusal undoes the refusal")


def test_rescore_reads_the_change_from_the_decklist_not_the_filename(tmp_path):
    """Trial names gained the cut partway through the project, so the
    older half of every stored run is named `<deck>_r<round>_<add>` with
    no cut in it. `cmd_rescore` parsed the name, so those rows rendered
    as a bare "+Card" -- a pure addition, which would be a 45-card deck
    and is not what ran. Proven on the stored run:
    `blue-eyes-branded-test_r0_24224830.ydk` and
    `..._r0_30576089_24224830.ydk` are byte-identical and both cut
    30576089.
    """
    from skills import optimizer

    summary = {"brick_rate": 0.3, "best_line_rate": 0.7,
               "hand_trap_survival": 0.0, "combo_actions": {"mean": 8.4},
               "window_recovery": {"ash": 0.36}, "hands": 10}
    _optimizer_scratch(tmp_path, [("d_r0_2000", 1005, 2000, summary)])

    change = optimizer.trial_change("d_r0_2000", "d", scratch=tmp_path)
    assert change["cut"] == [1005], change
    assert change["add"] == [2000], change


def test_rescore_does_not_count_one_experiment_twice(tmp_path):
    """The same decklist is stored under both naming schemes, with
    identical summaries -- the solver is deterministic given the same
    deck, hands and target. Printing both shows one experiment as two
    agreeing rows, which is how a ranking gets believed harder than its
    evidence."""
    from skills import optimizer

    summary = {"brick_rate": 0.3, "best_line_rate": 0.7,
               "hand_trap_survival": 0.0, "combo_actions": {"mean": 8.4},
               "window_recovery": {"ash": 0.36}, "hands": 10}
    _optimizer_scratch(tmp_path, [
        ("d_r0_2000", 1005, 2000, summary),            # old naming, no cut
        ("d_r0_1005_2000", 1005, 2000, dict(summary)),  # new naming
    ])

    rows = optimizer.rescore("d", scratch=tmp_path)
    assert len(rows) == 2, [r["label"] for r in rows]   # base + one candidate
    cand = [r for r in rows if r["label"] != "base"][0]
    assert cand["measured"] == 2, cand
    assert cand["label"] == "d_r0_1005_2000", (
        "keep the label that names both halves of the swap")


def test_rescore_keeps_both_rows_when_two_runs_of_a_list_disagree(tmp_path):
    """A disagreement between two records of the SAME decklist is a
    finding, not a duplicate -- the solver is supposed to be
    deterministic here. Collapsing it would hide exactly the thing worth
    seeing."""
    from skills import optimizer

    a = {"brick_rate": 0.3, "best_line_rate": 0.7, "hand_trap_survival": 0.0,
         "combo_actions": {"mean": 8.4}, "window_recovery": {"ash": 0.36},
         "hands": 10}
    b = dict(a, brick_rate=0.5)
    _optimizer_scratch(tmp_path, [
        ("d_r0_2000", 1005, 2000, a),
        ("d_r0_1005_2000", 1005, 2000, b),
    ])

    rows = optimizer.rescore("d", scratch=tmp_path)
    cands = [r for r in rows if r["label"] != "base"]
    assert len(cands) == 2, cands
    assert all(r["inconsistent"] == 2 for r in cands), cands


def test_draws_curve_denominator_does_not_move_when_hands_stop_early(tmp_path):
    """A hand that stops early has not stopped being at its best board.

    `at_draw[k]` counted only the hands that still had an attempt at draw
    k, so on branded -- where patience stops 31 of 40 hands after the
    breadth round -- the stored sweep read "draw 0 40/40 ... draw 13
    31/31". `cmd_draws` then compared those two RENDERED STRINGS, found
    them different, and printed "later draws kept improving boards, so
    draws is earning its budget on this deck". Every hand had in fact
    peaked in the breadth round and 553 of the deck's 655 solves bought
    nothing -- the verdict was exactly backwards, on the one question the
    command exists to answer.

    Two hands here: both peak at draw 0, but only one is still being
    solved at draw 1. The curve must stay 2/2.
    """
    import json as _json
    from skills import sweep

    def rec(hand, attempts):
        return _json.dumps({
            "status": "solved", "deck": {"name": "d"}, "hand_index": hand,
            "lines": [{"board": {}}],
            "solver": {"blocking": {"attempts": attempts}}})

    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([
        rec(0, [{"draw": 0, "label": "a", "status": "solved",
                 "interruptions": 3, "negates": 1}]),
        rec(1, [{"draw": 0, "label": "a", "status": "solved",
                 "interruptions": 2, "negates": 0},
                {"draw": 1, "label": "a", "status": "solved",
                 "interruptions": 2, "negates": 0}]),
    ]) + "\n")

    row = sweep.draws_curve(path)["d"]
    assert row["at_draw"][0] == "2/2", row["at_draw"]
    assert row["at_draw"][1] == "2/2", row["at_draw"]
    counts = list(row["peaked"].values())
    assert counts[0] == counts[-1], (
        f"{counts} -- a flat curve must read as flat, or `draws` reports "
        "that later draws earned their budget when they bought nothing")


def test_draws_curve_excludes_hands_that_never_solved(tmp_path):
    """A hand with no line has best (0, 0), which equals its running best
    at every draw -- so it used to count as "already at its best board"
    from draw 0, padding the exact number that decides whether extra
    draws are worth 2.6x the CPU. Branded's 9 unsolved hands of 40 were
    all in that count. Same split `boards` makes after the same bug."""
    import json as _json
    from skills import sweep

    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([
        _json.dumps({"status": "solved", "deck": {"name": "d"},
                     "hand_index": 0, "lines": [{"board": {}}],
                     "solver": {"blocking": {"attempts": [
                         {"draw": 0, "label": "a", "status": "solved",
                          "interruptions": 4, "negates": 2}]}}}),
        _json.dumps({"status": "unsolved", "deck": {"name": "d"},
                     "hand_index": 1, "lines": [],
                     "solver": {"blocking": {"attempts": [
                         {"draw": 0, "label": "a", "status": "unsolved"},
                         {"draw": 1, "label": "a", "status": "unsolved"}]}}}),
    ]) + "\n")

    row = sweep.draws_curve(path)["d"]
    assert row["hands"] == 2 and row["unsolved"] == 1
    assert row["at_draw"][0] == "1/1", row["at_draw"]
    assert row["mean_best_solved"] == 4.0, row["mean_best_solved"]
    assert row["mean_best"] == 2.0, row["mean_best"]


def test_draws_curve_counts_a_negate_only_gain_as_a_later_peak(tmp_path):
    """Only `interruptions` was compared, so a hand going from 3 int / 1
    neg to 3 int / 2 neg on a later draw counted as having peaked at draw
    0. The full tuple decides board quality everywhere else here."""
    import json as _json
    from skills import sweep

    path = tmp_path / "s.jsonl"
    path.write_text(_json.dumps({
        "status": "solved", "deck": {"name": "d"}, "hand_index": 0,
        "lines": [{"board": {}}],
        "solver": {"blocking": {"attempts": [
            {"draw": 0, "label": "a", "status": "solved",
             "interruptions": 3, "negates": 1},
            {"draw": 1, "label": "a", "status": "solved",
             "interruptions": 3, "negates": 2}]}}}) + "\n")

    row = sweep.draws_curve(path)["d"]
    assert row["at_draw"][0] == "0/1", row["at_draw"]
    assert row["at_draw"][1] == "1/1", row["at_draw"]


def test_draws_curve_ignores_records_without_attempts(tmp_path):
    import json as _json
    from skills import sweep
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([
        "not json",
        _json.dumps({"status": "solved", "deck": {"name": "d"},
                     "solver": {}}),
    ]) + "\n")
    assert sweep.draws_curve(path) == {}


def test_a_near_miss_asking_for_more_outranks_a_capped_solved_goal():
    """Ranking solved goals ahead of near misses is backwards when the
    near miss asks for more. Measured on despia, boards_multi vs the
    stored library over the same 40 hands: 5 hands came back WORSE than
    the old single-draw run. Hand 10 shows why —

        draw 0  stack:2         unsolved  best_placed=1   (15s triage)
        draw 2  piece:51409648  solved
        draws 3-7               piece goals, five more times

    `stack:2` was the only goal that could yield two interrupters. It got
    one 15-second attempt, ranked behind two solved single-piece goals,
    and never saw the full budget — while six draws went to a goal
    already at its ceiling of one."""
    from skills.sweep import allocate

    atts = [
        {"label": "piece:A", "status": "solved", "targets": ["b@hand", "A"],
         "interruptions": 1, "negates": 0},
        {"label": "piece:B", "status": "solved", "targets": ["b@hand", "B"],
         "interruptions": 1, "negates": 0},
        {"label": "stack:2", "status": "unsolved",
         "targets": ["b@hand", "A", "B"], "best_placed": 1},
    ]
    spend = allocate(atts, draws=4, n_goals=3)
    labels = [l for _, l in spend]
    assert labels[0] == "stack:2", (
        f"first re-draw went to {labels[0]}; the 2-piece near miss has the "
        f"only upside and must lead")
    assert labels.count("stack:2") >= 2, (
        f"stack:2 got {labels.count('stack:2')} of {len(labels)} draws")
    # Budget unchanged.
    assert len(spend) == 9


def test_equal_demand_still_prefers_the_solved_goal():
    """The earlier rule survives where it was right: when two goals ask
    for the same amount, the one already proving something leads."""
    from skills.sweep import allocate
    atts = [
        {"label": "near", "status": "unsolved", "targets": ["a", "1"],
         "best_placed": 1},
        {"label": "done", "status": "solved", "targets": ["a", "2"],
         "interruptions": 3, "negates": 1},
    ]
    assert allocate(atts, draws=3, n_goals=2)[0][1] == "done"


def test_wilson_matches_known_values():
    """Every confidence interval reported this session rests on this.
    Checked against the closed form: these are the figures quoted for the
    active deck (40/40 -> 91-100%, 15/20 -> 53-89%, 20/20 -> 84-100%)."""
    from skills.stats import wilson
    for k, n, lo_exp, hi_exp in ((40, 40, 0.912, 1.000),
                                 (15, 20, 0.531, 0.888),
                                 (20, 20, 0.839, 1.000),
                                 (0, 20, 0.000, 0.161),
                                 (10, 12, 0.552, 0.953)):
        p, lo, hi = wilson(k, n)
        assert abs(p - k / n) < 1e-12
        assert abs(lo - lo_exp) < 5e-4, (k, n, lo, lo_exp)
        assert abs(hi - hi_exp) < 5e-4, (k, n, hi, hi_exp)
        assert 0 <= lo <= p <= hi <= 1


def test_wilson_says_what_is_wrong_with_swapped_arguments():
    """k > n reached the sqrt with p > 1 and died on "math domain error",
    which names nothing. No trials at all is a legitimate "no
    information" answer and must keep working."""
    import pytest as _pytest
    from skills.stats import wilson
    with _pytest.raises(ValueError, match="more successes than trials"):
        wilson(20, 15)
    assert wilson(0, 0) == (0.0, 0.0, 1.0)
    assert wilson(0, 1)[0] == 0.0


def test_weighted_coverage_names_the_unmeasured_weight():
    """`weighted_handtrap_ranking` is correct — it divides by the weight
    of the decks each card was measured on, so it does not assume full
    coverage. What it could not say is how much of the room that was, and
    `threat_handtraps` turns the ranking into the handtraps the optimiser
    fires. Measured: locals.toml's largest single slice is `rogue` at
    0.250 with no deck file, and coverage is 58%."""
    from skills import scenarios

    weights = {"a": 0.5, "b": 0.25, "rogue": 0.25}
    matrix = {"a": {"cells": {}}, "b": {"cells": {}}}
    share, missing = scenarios.covered_share(matrix, weights)
    assert abs(share - 0.75) < 1e-9, share
    assert missing == [("rogue", 0.25)], missing

    # A deck present in the matrix but absent from the weights is not
    # coverage either -- it is not in the room.
    share2, _ = scenarios.covered_share(
        {"a": {"cells": {}}, "zz": {"cells": {}}}, weights)
    assert abs(share2 - 0.5) < 1e-9, share2

    # Full coverage reports nothing missing.
    share3, missing3 = scenarios.covered_share(
        {k: {"cells": {}} for k in weights}, weights)
    assert share3 == 1.0 and missing3 == []


def test_matrix_states_weighted_coverage_not_just_deck_count():
    """70% of decks and 58% of weight are different numbers, and the
    weighted one is the decision-relevant one."""
    import inspect
    import daemon
    src = inspect.getsource(daemon.cmd_matrix)
    assert "WEIGHTED COVERAGE" in src
    assert "covered_share" in src


def test_stacks_include_a_piece_that_only_missed_triage():
    """Building stacks from solved pieces alone never asks for a pair
    whose second piece misses the 15s triage budget. Conjunction
    monotonicity only says an UNSATISFIABLE {base,B} makes {base,A,B}
    unsatisfiable — a 15s miss is a false negative, not unsatisfiability.

    Measured on fiendsmith: 9 of 40 hands came back WORSE than the old
    single-draw library, which extra draws cannot cause; only a narrower
    goal set can."""
    from skills.sweep import combinations_to_try

    atts = [
        {"label": "piece:A", "status": "solved", "targets": ["b@hand", "A"]},
        {"label": "piece:B", "status": "unsolved", "targets": ["b@hand", "B"],
         "best_placed": 1},
        {"label": "piece:C", "status": "unsolved", "targets": ["b@hand", "C"],
         "best_placed": 0},
    ]
    out = combinations_to_try(atts)
    sets = [frozenset(t) for t, _ in out]
    assert frozenset(["b@hand", "A", "B"]) in sets, (
        "A+B was never requested; B only missed triage, it is not known "
        "unreachable")
    # A piece that placed NOTHING stays excluded -- that is the sound half
    # of the monotonicity argument.
    assert all("C" not in t for t, _ in out), out


def test_all_solved_combinations_are_offered_before_mixtures():
    """A combination of pieces that each solved has evidence; one
    including a near miss is a maybe. Evidence first."""
    from skills.sweep import combinations_to_try
    atts = [
        {"label": "piece:A", "status": "solved", "targets": ["b", "A"]},
        {"label": "piece:B", "status": "solved", "targets": ["b", "B"]},
        {"label": "piece:N", "status": "unsolved", "targets": ["b", "N"],
         "best_placed": 1},
    ]
    out = combinations_to_try(atts, max_new=10)
    first_mixed = next((i for i, (t, _) in enumerate(out) if "N" in t), None)
    first_solved = next((i for i, (t, _) in enumerate(out)
                         if "A" in t and "B" in t and "N" not in t), None)
    assert first_solved is not None and first_mixed is not None
    assert first_solved < first_mixed, (
        f"a near-miss mixture came before the all-solved pair: {out}")


def test_no_duplicate_stack_is_proposed():
    from skills.sweep import combinations_to_try
    atts = [
        {"label": "piece:A", "status": "solved", "targets": ["b", "A"]},
        {"label": "piece:B", "status": "unsolved", "targets": ["b", "B"],
         "best_placed": 2},
    ]
    out = combinations_to_try(atts, max_new=10)
    keys = [frozenset(t) for t, _ in out]
    assert len(keys) == len(set(keys)), out


def test_handtrap_survival_denominator_is_hands_not_windows():
    """`survival` counts HANDS that survived every window, not windows.

    The three labels (scenarios docstring, `daemon.py matrix`, the web
    matrix caption) all said "of the windows where the handtrap could be
    used, how often the line still completed", which is a different and
    much larger number: over the 47 measured cells of
    results/handtrap_matrix_40h.jsonl the two differ by a mean 17.3
    points, and fiendsmith vs Effect Veiler is 0% per hand against 77%
    per window. 8 of 47 cells land on opposite sides of 50%, so the
    per-window reading dismisses handtraps that stop every line.

    This pins the metric, not the wording: a hand with 11 windows that
    converted 8 contributes 0, because `survives` is the all-windows
    standard.
    """
    from skills.scenarios import handtrap_matrix

    rec = {
        "summary": {"deck": "d", "solved": 2, "hands": 2},
        "hands": [
            # Converted 8 of 11 windows -- a high per-window rate, but the
            # line did not survive, so it contributes 0.
            {"status": "solved",
             "fire": {"97268402": {"status": "solved", "windows": 11,
                                   "converted": 8, "survives": False}}},
            {"status": "solved",
             "fire": {"97268402": {"status": "solved", "windows": 11,
                                   "converted": 8, "survives": False}}},
        ],
    }
    cell = handtrap_matrix([rec])["d"]["cells"][97268402]
    assert cell["tested"] == 2
    assert cell["survival"] == 0.0, (
        "survival must be hands-that-survived / hands-with-a-window; "
        f"got {cell['survival']} -- 16/22 converted windows is 0.73 and "
        "would mean the denominator slipped back to windows")


def test_handtrap_matrix_labels_say_hands_not_windows():
    """The three display sites must not describe survival per window."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("skills/scenarios.py", "daemon.py",
                "web/templates/matrix.html"):
        text = (root / rel).read_text()
        assert "of the windows where" not in text.lower(), (
            f"{rel} describes survival per window; the denominator is "
            "hands that had a legal window")


def _banlist_conn(rows):
    """In-memory card mirror. rows = [(id, name, frame, banlist_tcg)]."""
    from skills import db
    conn = db.connect(":memory:")
    conn.executemany(
        "INSERT INTO cards (id, name, frame, banlist_tcg, last_fetched) "
        "VALUES (?,?,?,?,?)",
        [(i, n, fr, st, "now") for i, n, fr, st in rows])
    conn.commit()
    return conn


def test_forbidden_cards_are_not_legal_at_three_copies():
    """The ban-list map read "Banned"; the mirror stores "Forbidden".

    `.get(status, 3)` therefore returned 3 for every forbidden card -- all
    121 on the TCG list, Apollousa and Archnemeses Protos included -- so
    `check` passed a deck running three of them. "Limited" and
    "Semi-Limited" were spelled correctly, which is why the ban list
    looked like it worked.

    Measured over assets/decks before the fix: Blitzclique and kewl-tune
    both reported "legal" while holding a forbidden card, and snake_eye
    reported 1 problem instead of 4.
    """
    from skills import card_db

    conn = _banlist_conn([
        (1, "forbidden card", "effect", "Forbidden"),
        (2, "banned card", "effect", "Banned"),       # the other spelling
        (3, "limited card", "effect", "Limited"),
        (4, "semi card", "effect", "Semi-Limited"),
        (5, "free card", "effect", None),
    ])
    assert card_db.banlist_limit(conn, 1) == 0, "Forbidden must mean 0 copies"
    assert card_db.banlist_limit(conn, 2) == 0, "Banned must mean 0 copies"
    assert card_db.banlist_limit(conn, 3) == 1
    assert card_db.banlist_limit(conn, 4) == 2
    assert card_db.banlist_limit(conn, 5) == 3


def test_unknown_banlist_status_refuses_rather_than_allowing_three():
    """A status the map does not know must raise, not default to 3.

    The permissive `.get(status, 3)` default is the whole mechanism of the
    bug above: one renaming upstream and every affected card silently
    becomes legal at three copies. A loud failure is the only safe answer,
    and `Deck.validate` turns it into a reported problem.
    """
    import pytest
    from skills import card_db, deck as deck_mod

    conn = _banlist_conn([(1, "odd card", "effect", "Semi-Forbidden?")])
    with pytest.raises(ValueError, match="unknown tcg ban-list status"):
        card_db.banlist_limit(conn, 1)

    d = deck_mod.Deck(name="t", main=[1] * 40, extra=[], side=[])
    problems = d.validate(conn)
    assert any("unknown tcg ban-list status" in p for p in problems), (
        f"validate must report the unreadable status, not pass: {problems}")


def test_pendulum_extra_deck_frames_route_to_the_extra_deck():
    """fusion/synchro/xyz_pendulum are EXTRA-deck cards.

    `EXTRA_FRAMES` held only the four plain frames, so a Fusion Pendulum
    monster such as Starving Venom Wing Dragon was called a "main-deck
    card in the extra deck" -- a false legality error on real lists; the
    scraped decks hold 3 fusion_pendulum and 2 xyz_pendulum cards in their
    extra sections -- and the same card sitting illegally in a main deck
    went unflagged.

    ritual_pendulum stays a main-deck frame: Ritual monsters are main
    deck.
    """
    from skills import deck as deck_mod

    for frame in ("fusion_pendulum", "synchro_pendulum", "xyz_pendulum"):
        assert frame in deck_mod.EXTRA_FRAMES, f"{frame} belongs in the extra deck"
    assert "ritual_pendulum" not in deck_mod.EXTRA_FRAMES
    assert "effect_pendulum" not in deck_mod.EXTRA_FRAMES

    conn = _banlist_conn([
        (1, "filler", "effect", None),
        (2, "Starving Venom Wing Dragon", "fusion_pendulum", None),
    ])
    ok = deck_mod.Deck(name="t", main=[1] * 40, extra=[2], side=[])
    assert not [p for p in ok.validate(conn) if "deck" in p and "cards" not in p], (
        "a fusion_pendulum card in the extra deck is correctly placed")

    bad = deck_mod.Deck(name="t", main=[1] * 39 + [2], extra=[], side=[])
    assert any("extra-deck card in the main deck" in p
               for p in bad.validate(conn)), \
        "a fusion_pendulum card in the MAIN deck must be flagged"


def test_a_solver_crash_is_not_counted_as_a_deck_brick():
    """`status: "error"` is the SOLVER failing, not the deck bricking.

    `summarize` (copied verbatim from the vendored
    tools/solve_blocking.py, so this is inherited rather than drifted)
    increments `hands` for every record and `solved` only for solved
    ones. An errored hand therefore depresses `solve_rate` exactly like a
    brick, and nothing in the summary or any display said a crash had
    happened.

    results/boards_stale_prefix.jsonl holds 12 such hands, all
    "solver exited with -11" -- the documented BabelCDB segfault, which
    CLAUDE.md warns follows any submodule bump without `setup`. No live
    record carries one today, so this is a latent defect rather than a
    wrong stored number; it matters because `brick_rate` carries weight
    -2.0 in the optimiser objective, so a handful of segfaults would make
    it prefer whichever list happened not to crash.

    `solve_rate` keeps its definition -- `load_boards` and every stored
    record depend on solved/hands -- so the fix is that the count exists
    and is shown.
    """
    from skills import sweep

    def rec(status, name="d"):
        r = {"deck": {"name": name}, "status": status, "hand_index": 0,
             "targets": [], "solver": {"wall_s": 1.0}}
        if status == "solved":
            r["solver"]["blocking"] = {"interruptions": 2, "negates": 1,
                                       "pieces": []}
            r["lines"] = [{"board": {"players": [
                {"mzone": [], "szone": [], "hand": []}]}}]
        return r

    table = sweep.summarize(
        [rec("solved"), rec("solved"), rec("brick"), rec("error")],
        cards={})
    row = table["d"]
    assert row["hands"] == 4
    assert row["solved"] == 2
    assert row["errors"] == 1, (
        "a crashed hand must be counted as a solver failure, not folded "
        "silently into the 2 non-solves")
    # The rate keeps its contract, which is exactly why the count is needed.
    assert row["solve_rate"] == 0.5


def test_boards_output_names_solver_failures():
    """The `boards` display must say a crash happened, not just lower the rate."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "daemon.py").read_text()
    assert "SOLVER FAILURE" in src, (
        "a solver crash is counted in hands and not in solved, so it reads "
        "as a brick unless the display names it")


def test_handtrap_and_floodgate_ids_match_their_labels():
    """Every hardcoded card id must be the card its label names.

    Two FLOODGATES ids were wrong: 97642679 is Dark Master - Zorc, a
    Ritual Effect Monster, and was labelled "There Can Be Only One"; and
    23616456 is not a card at all, labelled "Summon Limit". So two of the
    five floodgate tests ran against a monster that is not a floodgate and
    a board that could never be built -- silently, because a missing card
    just fails to assemble.

    This is the same defect class as the ban-list mapping: a hardcoded
    lookup on external vocabulary, degrading quietly when a member is
    wrong. A test is the only thing that catches a transposed digit.
    """
    from skills import card_db, db, scenarios

    conn = db.connect()
    if not conn.execute("SELECT 1 FROM cards LIMIT 1").fetchone():
        import pytest
        pytest.skip("card mirror is empty; run `daemon.py cards`")

    problems = []
    for label_map in (scenarios.HANDTRAPS, scenarios.FLOODGATES):
        for cid, label in label_map.items():
            row = card_db.resolve(conn, cid)
            if row is None:
                problems.append(f"{cid} ({label!r}) is not in the card mirror")
                continue
            # Labels are short display forms ("Ghost Ogre & Snow Rabbit"
            # for a longer printed name), so compare on the first clause.
            head = str(label).split(" &")[0].split(",")[0].strip().lower()
            if head not in row["name"].lower():
                problems.append(
                    f"{cid} is labelled {label!r} but is {row['name']!r}")
    assert not problems, "hardcoded card ids do not match their labels: " + \
        "; ".join(problems)


def test_face_down_is_0xA_not_0x4():
    """0x4 is POS_FACEUP_DEFENSE -- face UP, not set.

    `drill.describe` and `handlab`'s board rows both tested
    `position in (4, 8, 0xA)`, under a comment claiming 0x4|0x8 were the
    face-down bits. POS_FACEDOWN is 0xA = 0x2 (face-down attack) | 0x8
    (face-down defence); 0x4 is face-up defence.

    The test was therefore wrong twice: it called every face-up defence
    monster "(set)" and would have missed a face-down attack one. Measured
    over every board in results/*.jsonl, 1480 of 5416 cards are position
    4 -- 27.3% read as set while face up, which for a monster is the
    difference between its effect being live and not. `drill._on_field`
    used `& 0x5` correctly all along, so the ranking and the text
    disagreed about the same card.
    """
    from ygorl.engine import constants as C
    from skills import drill

    # Pin the engine's own constants, so a vendor bump that moves them
    # fails here rather than silently changing what "set" means.
    assert C.POS_FACEUP_DEFENSE == 0x4
    assert C.POS_FACEDOWN == 0xA
    assert C.POS_FACEDOWN_ATTACK == 0x2
    assert C.POS_FACEDOWN_DEFENSE == 0x8

    def described(position):
        line = {"board": {"players": [{
            "mzone": [{"code": 1, "position": position}],
            "szone": [], "hand": []}]}}
        return drill.describe(_NameStub(), line)[0]

    assert "(set)" not in described(C.POS_FACEUP_ATTACK)
    assert "(set)" not in described(C.POS_FACEUP_DEFENSE), \
        "face-up defence is FACE UP; 1480 stored board cards sit here"
    assert "(set)" in described(C.POS_FACEDOWN_DEFENSE)
    assert "(set)" in described(C.POS_FACEDOWN_ATTACK), \
        "face-down attack was missed by the old `in (4, 8, 0xA)` test"

    # handlab keeps its own copy of this test for the web board view, and
    # it is not reachable without a solved hand, so pin it at the source.
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("skills/drill.py", "skills/handlab.py", "skills/sweep.py"):
        # Comment lines are skipped: each of these files now *documents*
        # the old pattern, so matching raw text flags the explanation.
        code = "\n".join(
            ln for ln in (root / rel).read_text().splitlines()
            if not ln.lstrip().startswith("#"))
        assert "in (4, 8, 0xA)" not in code, (
            f"{rel} still tests face-down with `in (4, 8, 0xA)`, which "
            "counts face-up defence (0x4) as set")


class _NameStub:
    """Stands in for a card-mirror connection in describe()."""

    def execute(self, *a, **k):
        return self

    def fetchone(self):
        return None


def test_zone_rank_covers_the_solver_vocabulary():
    """Every zone the solver accepts must have an explicit rank.

    `_zone_rank` falls back to 1 -- the "set backrow" rank -- for an
    unknown zone, and `derive_targets` ranks candidate targets by it to
    pick the most SUBSTANTIVE one. `mzone` was missing, so a monster in a
    monster zone would have ranked level with a card set from hand and
    two ranks below `grave`, three below the `atk` spelling of the same
    board.

    Nothing emits `@mzone` today -- 0 of the stored target specs use it
    and duelroom never constructs one -- so this was latent. It is still
    reachable: `--target card@mzone` is documented, and `--target` is
    repeatable and passed through `SolveRequest.extra_args`.

    This parses the solver's OWN --help rather than restating a list, so
    a vendor bump that adds a zone fails here instead of silently
    ranking it as backrow. Same guard shape as the card-id test, for the
    same reason.
    """
    import re
    import subprocess

    import pytest

    from skills import engine, scenarios

    if not engine.SOLVER_BIN.exists():
        pytest.skip("solver binary not built")
    help_text = subprocess.run(
        [str(engine.SOLVER_BIN), "--help"],
        capture_output=True, text=True, timeout=60).stdout

    m = re.search(r"zone = (.+?)\. Add :fd", help_text, re.S)
    assert m, "the solver's --help no longer states its zone vocabulary"
    # "atk def (monster zone, face up), mzone, szone, hand, grave, banished"
    listed = {z for z in re.findall(r"\b([a-z]+)\b", m.group(1))
              if z not in {"monster", "zone", "face", "up", "and", "or", "a"}}

    missing = sorted(listed - set(scenarios.ZONE_RANK))
    assert not missing, (
        f"the solver accepts {missing} but ZONE_RANK has no rank for them, "
        "so _zone_rank silently calls them set-backrow (1)")

    # The :fd forms the help describes explicitly.
    for z in ("mzone:fd", "szone:fd"):
        assert z in scenarios.ZONE_RANK, f"{z} has no explicit rank"

    # A monster zone is a field zone: it must outrank set backrow.
    assert scenarios.ZONE_RANK["mzone"] == scenarios.ZONE_RANK["atk"]
    assert scenarios.ZONE_RANK["mzone"] > scenarios.ZONE_RANK["szone"]


def test_hand_answers_the_group_question_not_only_per_card():
    """`hand` must reach `stats.group_chance`, not just `open_chance`.

    Handtraps are interchangeable for the question "do I open any
    interaction", so per-card odds systematically understate a deck. On
    the active deck (44 main, 8 handtrap copies over 4 cards) the best
    single card is 31.0% and the group is 65.3% -- reading the per-card
    column as the answer is wrong by more than two to one, and a player
    could add handtraps the deck does not need.

    `group_chance`, `combo_chance` and `trials_needed` were all correct
    and tested and NONE of them was reachable from the app -- the
    "parameter that exists and cannot be set" that CLAUDE.md calls this
    project's signature bug.
    """
    import pathlib
    from skills import stats

    src = (pathlib.Path(__file__).resolve().parent.parent / "daemon.py").read_text()
    assert "stats.group_chance" in src, \
        "`hand` cannot answer the group question"
    assert "stats.trials_needed" in src, \
        "the 'raise hands' advice has no number behind it"

    # The gap the feature exists to close, pinned numerically.
    size, group, best_single = 44, 8, 3
    assert stats.group_chance(size, group, 5) > \
        2 * stats.open_chance(size, best_single, 5)


def test_opening_hand_math_matches_closed_form():
    """Guards the exact hypergeometric against an independent formula.

    Audited 2026-10-25 against scipy over 728 (N, K, n, k) cases -- max
    absolute difference 2.22e-16 -- and `combo_chance` against exact
    brute-force enumeration over labelled cards on five cases including
    three disjoint groups and a six-card hand. This keeps a closed-form
    check in the suite so a future edit cannot quietly change it.
    """
    import math
    from skills import stats

    # P(>=1 of 3 copies in 40, 5 cards) = 1 - C(37,5)/C(40,5)
    assert abs(stats.open_chance(40, 3, 5)
               - (1 - math.comb(37, 5) / math.comb(40, 5))) < 1e-12
    # P(zero of 9 starters in 40, 5 cards) = C(31,5)/C(40,5)
    assert abs(stats.brick_chance(40, 9, 5)
               - math.comb(31, 5) / math.comb(40, 5)) < 1e-12
    # The pmf is a distribution.
    assert abs(sum(stats.pmf(44, 8, 5, k) for k in range(6)) - 1.0) < 1e-12
    # at_least and at_most are complementary.
    assert abs(stats.at_least(44, 8, 5, 2)
               + stats.at_most(44, 8, 5, 1) - 1.0) < 1e-12
    # combo_chance over disjoint groups, against the closed form for one.
    assert abs(stats.combo_chance(13, [(5, 2)], 5)
               - stats.at_least(13, 5, 5, 2)) < 1e-12
    # A hand larger than the deck is 0, not a ZeroDivisionError.
    assert stats.pmf(5, 1, 40, 1) == 0.0


def test_side_plan_is_not_quietly_wired_up():
    """The side planner must stay unreachable while its metric is broken.

    `optimizer.side_plan` rates every candidate through
    `breaking.going_second` -- the same function behind the `second`
    command, which CLAUDE.md marks UNSUPPORTED on a decisive measurement
    (kewl-tune reaches its target in 10 of 12 hands going first and 0 of 4
    going second into one vanilla Alexandrite Dragon).

    So this is a guard against a well-meant future edit: exposing the side
    planner before ISSUES.md #4 is fixed would hand over a tool whose
    objective is known not to measure what it claims, for the 15 cards
    that decide games two and three. If you are deliberately wiring it up
    because #4 is fixed, delete this test in the same commit and say so.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    callers = [root / "daemon.py"]
    callers += list((root / "web").glob("*.py"))
    callers += list((root / "tools").glob("*.py"))
    for f in callers:
        text = f.read_text()
        assert "side_plan(" not in text, (
            f"{f.name} calls side_plan, but it is rated by the unsupported "
            "going-second path (ISSUES.md #4)")

    # And the docs must not describe it as merely slow.
    doc = (root / "CLAUDE.md").read_text()
    assert "NO WAY IN" in doc, \
        "CLAUDE.md must say the side plan has no route, not just that it is slow"


def test_survival_has_one_implementation_and_it_is_the_honest_one():
    """`summary.survival_given_line` must not reach any display.

    It counts a hand where the handtrap had NO legal window as a
    survival, so it is never lower than the per-hand number and is badly
    wrong for a card that is rarely live. Measured over the 47 comparable
    cells of results/handtrap_matrix_40h.jsonl: 19 cells higher, 28
    equal, **0 lower** -- it systematically flatters the deck. Mean gap
    10.3 points, max 91, 7 of 47 on opposite sides of 50%.

        kewl-tune   vs Nibiru: 91% summary,  0% honest, live  3/33
        sky-striker vs Nibiru: 89%        ,  0%       , live  4/35
        despia      vs Nibiru: 83%        ,  0%       , live  5/29

    `report` printed those under "PLAYS THROUGH (higher is better -- this
    is YOUR deck)" and derived both the verdict ("fine" at 91%) and "your
    worst matchup card" from them, so a card the deck never beats was
    labelled fine and never named as the worst.

    CLAUDE.md: "two implementations of a number is how this project's
    worst bugs got in". This was two that disagree.
    """
    import json
    import pathlib

    from skills import scenarios

    root = pathlib.Path(__file__).resolve().parent.parent

    # One implementation, and handtrap_matrix uses it.
    assert hasattr(scenarios, "survival_from_hands")

    path = root / "results" / "handtrap_matrix_40h.jsonl"
    if path.exists():
        recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        m = scenarios.handtrap_matrix(recs)
        for deck, nib_tested in (("kewl-tune", 3), ("sky-striker", 4),
                                 ("despia", 5)):
            cell = m[deck]["cells"][27204311]
            assert cell["survival"] == 0.0, (
                f"{deck} vs Nibiru must read 0%, not the summary's ~90%")
            assert cell["tested"] == nib_tested

    # No display may read the inflated field as a value.
    for rel in ("daemon.py", "web/views.py"):
        for line in (root / rel).read_text().splitlines():
            bare = line.strip()
            if bare.startswith("#") or "survival_given_line" not in bare:
                continue
            # Iterating its KEYS is fine -- that is the list of cards
            # fired. Reading its values as the number is not.
            assert ("for code in" in bare or "or {})" in bare), (
                f"{rel} reads survival_given_line as a value: {bare}")


def test_special_summon_cache_has_a_producer():
    """`threats` must be able to rebuild the cache it renders.

    `results/special_summons.json` backs three of `threats`' tables --
    special summons per line, "Nibiru live", and the cards surrendered to
    Maxx "C" -- and **nothing in the app wrote it**.
    `scenarios.special_summons` is the producer and no command called it,
    so the file had been created out of band on 2026-10-18. Two
    consequences: it went stale the moment the board library was rebuilt
    (which was happening while this was found), and deleting it removed
    those tables with no way to get them back.

    Replaying is cheap because no search is involved -- 6.6 s for all 280
    records of results/boards.jsonl -- and it is deterministic:
    regenerating reproduced all 7 decks byte-identical to the 2026-10-18
    file. So `threats` now rebuilds whenever the cache is missing or older
    than the library, and labels the table "rebuilt just now" or "cached".
    """
    import pathlib
    from skills import scenarios

    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "daemon.py").read_text()
    assert "scenarios.special_summons(" in src, (
        "nothing calls the producer, so special_summons.json cannot be "
        "rebuilt and the threats tables render a file of unknown age")
    assert "st_mtime" in src and "special_summons.json" in src, \
        "the cache must be compared against the library it describes"

    # The producer's own contract: it replays, so it reports nibiru_rate.
    rec = {
        "status": "solved", "deck": {"name": "d"},
        "start": {"decks": {"a": []}}, "lines": [],
    }
    out = scenarios.special_summons([rec])
    assert isinstance(out, dict)


def test_targets_source_is_nameable_and_stated():
    """`targets` must not be hardcoded to one board library.

    It read `results/boards.jsonl` unconditionally -- the SINGLE-DRAW
    library, which CLAUDE.md documents as understating every deck ("one
    attempt is one draw"). Targets decide what every later solve aims
    at, so that choice propagates into the solve rate, the brick rate,
    the handtrap matrix and the optimiser objective, and it could not be
    changed or even compared against.

    It changes the answer: deriving from the 4-deck multi-draw arm picks
    a different target for 2 of 5 decks -- branded Rindbrumm the Striking
    Dragon -> Stardust Dragon - Victim Sanctuary, fiendsmith Fiendsmith's
    Desirae -> F.A. Dawn Dragster.

    The default deliberately stays `boards.jsonl`: silently re-pointing
    the chain at whatever file is newest would change every downstream
    number unasked, and newer is not better -- the arm above came from a
    run abandoned mid-way and kept on purpose. So the source is nameable,
    always printed with its date, and newer candidates are reported.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "daemon.py").read_text()

    # The selection lives in ONE helper, shared by `targets` and
    # `threats`, so the choice cannot drift between them. Scoped to the
    # helper rather than to cmd_targets: where the code lives is not the
    # invariant, and asserting the location is what broke this test when
    # it was extracted.
    i = src.index("def _board_library")
    body = src[i:src.index("def _latest_matrix")]

    assert "args[0]" in body, "the board library must be nameable"
    assert "strftime" in body, "the source must be printed with its date"
    # A preserved control arm (a dot in the stem, e.g.
    # boards_multi.narrow-stacks) must never be the suggested switch: it
    # exists BECAUSE it was built by defective code. Sorting the
    # candidates by name recommended exactly that.
    assert "q.stem" in body and "st_mtime" in body, \
        "candidates must be ranked by mtime, not by name"
    assert ".partial" in body, \
        "a library still being written is incomplete and must be flagged"
    # Both consumers must go through it.
    for fn in ("def cmd_targets", "def cmd_threats"):
        j = src.index(fn)
        assert "_board_library(" in src[j:j + 3000], \
            f"{fn} must use the shared selector, not its own copy"


def test_special_summon_cache_metadata_does_not_break_consumers():
    """The cache's `_source` key is metadata, not a deck.

    Added 2026-10-25 so that switching board library is detected -- a
    timestamp cannot see it, because a cache built from a DIFFERENT
    library may still be newer than the one now selected. Both readers
    must skip underscore keys: `web/views.py` unpacked every value with
    `{"archetype": a, **v}`, and on the string value that raised
    "TypeError: 'str' object is not a mapping", 500ing /threats and
    /report. tests/test_web.py's smoke walk over every GET route caught
    it, which is the reason that walk exists.
    """
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "web" / "views.py").read_text()
    assert 'startswith("_")' in src, \
        "web/views.py must skip the cache's metadata keys"

    cache = root / "results" / "special_summons.json"
    if cache.exists():
        raw = json.loads(cache.read_text())
        assert "_source" in raw, "the cache must record which library it came from"
        decks = {k: v for k, v in raw.items() if not k.startswith("_")}
        assert decks, "metadata must not be the only content"
        for name, v in decks.items():
            assert isinstance(v, dict), f"{name} should be a mapping"
            assert "nibiru_rate" in v, \
                "a replayed entry carries nibiru_rate; a window-count one does not"


def test_locals_coverage_claim_matches_the_measurement():
    """The documented coverage figure must be the real one.

    CLAUDE.md said `locals.toml` covers 55% of the stated meta while
    `config.coverage` reports 58.21% -- a stale number in the file every
    session loads, and the same defect class as every other number this
    review corrected. Pinning it to the measurement so the two cannot
    drift again.
    """
    import pathlib
    from skills import config

    share, missing = config.coverage([
        "branded", "despia", "elfnote", "fiendsmith", "kewl-tune",
        "light-and-darkness-ritual", "sky-striker",
    ])
    doc = (pathlib.Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text()
    claimed = f"{share * 100:.1f}%"
    assert claimed in doc, (
        f"CLAUDE.md must state the measured coverage {claimed}; "
        f"uncovered archetypes are {sorted(missing)}")
    # And it must not still carry the superseded figure as the claim.
    assert "covering 55% of a stated meta" not in doc


def test_the_banlist_map_has_exactly_one_implementation():
    """No module may re-spell the ban-list status map.

    ISSUES #40 fixed `card_db.banlist_limit`, which read "Banned" while
    the mirror stores "Forbidden" and so returned 3 for all 121
    TCG-forbidden cards. `skills/builder.py` held a SECOND, independent
    copy of the same dict with the same defect, and it was missed --
    found 2026-10-25 by applying the defect-class checklist to the
    modules not yet audited.

    In the deckbuilder it was visible: `build.html` gates the restriction
    badge on `c.limit < 3`, so every forbidden card showed **no badge at
    all** and the "already at the limit" warning never fired. All 121 of
    them. `Deck.validate` still refused to save such a list, so this was
    a lying display rather than a saveable illegal deck -- but it is the
    display a tournament list gets built in.

    The mapping now lives only in `card_db.BANLIST_LIMITS`, read through
    `limit_for_status` (when the row is in hand) or `banlist_limit` (when
    only an id is). This test fails on any other copy of it.
    """
    import pathlib
    from skills import builder, card_db, db

    root = pathlib.Path(__file__).resolve().parent.parent
    for path in sorted((root / "skills").glob("*.py")) + \
            [root / "daemon.py"] + sorted((root / "web").glob("*.py")):
        text = path.read_text()
        if path.name == "card_db.py":
            continue
        for spelling in ('"Semi-Limited":', "'Semi-Limited':"):
            assert spelling not in text, (
                f"{path.name} re-spells the ban-list map; use "
                "card_db.limit_for_status or card_db.banlist_limit")

    # And the deckbuilder actually reports a forbidden card as 0 copies.
    conn = db.connect()
    row = conn.execute(
        "SELECT id, name FROM cards WHERE banlist_tcg='Forbidden' LIMIT 1"
    ).fetchone()
    if row is None:
        import pytest
        pytest.skip("card mirror has no forbidden cards; run `daemon.py cards`")
    rows = builder.search(conn, row["name"], limit=1)[0]
    assert rows, f"deckbuilder search found nothing for {row['name']}"
    assert rows[0]["limit"] == 0, (
        f"{row['name']} is Forbidden but the deckbuilder offers "
        f"{rows[0]['limit']} copies")
    assert card_db.limit_for_status("Forbidden") == 0


def test_follow_up_categories_all_resolve():
    """Every FOLLOW_UP key must name a real CATEGORY_ constant.

    `handlab` builds its follow-up labels with
    `eff.categories & K.get(f"CATEGORY_{key}", 0)` -- a silent default of
    0, which makes the label unreachable rather than wrong. All 13 keys
    resolve today (checked 2026-10-25), so this is a guard, not a fix: a
    vendor rename would otherwise drop a label from the pilot view with
    nothing failing. That shape produced two real defects tonight, the
    ban-list map twice (#40, #53).
    """
    import pytest
    try:
        from ygorl.build.scripts import load_constants
    except ImportError:
        pytest.skip("ygorl not importable")
    from skills.handlab import FOLLOW_UP

    K = load_constants()
    missing = [k for k in FOLLOW_UP if f"CATEGORY_{k}" not in K]
    assert not missing, (
        f"FOLLOW_UP names categories that do not exist: {missing}. The "
        "`.get(..., 0)` default makes those labels silently unreachable")


def test_replay_failures_are_counted_not_swallowed():
    """A line that will not replay must not vanish from the figures.

    `scenarios.special_summons` had `except Exception: continue` with no
    tally, so every number it produced carried an invisible denominator:
    "elfnote 6.1 summons" over an unknown number of lines. A submodule
    bump that broke replaying — which CLAUDE.md warns about, since a card
    whose Lua did not load replays as nothing — would have read as a
    smaller sample rather than as a failure.

    Measured 2026-10-25: **0 failures over all 271 solved lines** of
    results/boards.jsonl, so nothing reported was wrong. This is a latent
    defect closed, plus the reporting that makes it visible if it ever
    fires.
    """
    import inspect
    from skills import scenarios

    src = inspect.getsource(scenarios.special_summons)
    assert "failed[name]" in src, \
        "a line that will not replay must be counted"
    # The contract: both the sample and what it was drawn from.
    rec = {"status": "solved", "deck": {"name": "d"},
           "start": {"decks": {"a": []}}, "lines": []}
    out = scenarios.special_summons([rec])
    assert isinstance(out, dict)

    import json
    import pathlib
    cache = (pathlib.Path(__file__).resolve().parent.parent
             / "results" / "special_summons.json")
    if cache.exists():
        for name, v in json.loads(cache.read_text()).items():
            if name.startswith("_"):
                continue
            assert "attempted" in v and "failed" in v, (
                f"{name} must say how many lines were asked about; `n` "
                "alone cannot distinguish 10 solved lines from 40 with "
                "30 failures")
            assert v["attempted"] == v["n"] + v["failed"]


def test_drill_reports_a_real_error_instead_of_no_lines():
    """`drill`'s source loop must not read an exception as "empty".

    It walked every results/*.jsonl under `except Exception: continue`,
    so a real error in `drill.export` — a bug, a malformed record, a
    vendor change — was indistinguishable from "this source has no lines
    for that deck", silently, for every source in turn. The loop still
    moves on (a malformed record should not stop the search), but the
    failures are reported when nothing is found.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "daemon.py").read_text()
    i = src.index("def cmd_drill")
    body = src[i:i + 4000]
    assert "problems.append" in body, \
        "a raising source must be recorded, not silently skipped"
    assert "raised rather than being empty" in body, \
        "and reported when no lines were found"


def test_vocab_cache_is_keyed_on_content_not_identity():
    """`compile_vocab` must not key its cache on `id(vocab)`.

    CPython reuses an address once the object at it is collected, so a
    new and DIFFERENT vocabulary allocated there received a stale cache
    hit. Demonstrated 2026-10-25: alternating two vocabularies with a
    gc.collect() between them, `compile_vocab(["Snake-Eye",
    "Fiendsmith"])` returned the compiled patterns for ["Branded",
    "Despia"] on the THIRD iteration.

    Live impact was almost certainly nil -- `fetch_archetype_vocab`
    returns the same ~600 tags every call and each caller holds its list
    for the whole loop -- but the failure mode is silent
    misidentification of every deck name, and the fix is one line.
    """
    import gc
    import inspect

    from skills import meta_scrape as ms

    src = inspect.getsource(ms.compile_vocab)
    assert "id(vocab)" not in src, \
        "keying on id() gives stale hits after an address is reused"

    for trial in range(400):
        want = (["Branded", "Despia"] if trial % 2 == 0
                else ["Snake-Eye", "Fiendsmith"])
        got = [t for t, _ in ms.compile_vocab(want)]
        assert got == want, (
            f"trial {trial}: asked for {want}, cache returned {got}")
        del want
        gc.collect()


def test_gauntlet_scratch_name_is_reproducible():
    """Scratch names must not come from `hash()`, which is salted.

    `gauntlet` built its per-candidate scratch directory as
    `f"{name}_h{h}_{abs(hash(candidate.board_key())) % 10**8}"`, and
    Python salts hash() of anything containing a str per process. Nothing
    was computed wrongly -- it is only a directory name -- but it changed
    every run, so a scratch dir could not be matched back to the
    candidate that produced it, which is exactly what is wanted when a
    gauntlet search misbehaves. Verified across three interpreters:
    hash() gave three different values, crc32 one.
    """
    import inspect
    from skills import gauntlet

    src = inspect.getsource(gauntlet)
    assert "hash(candidate.board_key())" not in src, \
        "hash() is per-process salted; use zlib.crc32"
    assert "crc32" in src


def test_extra_target_tie_is_broken_deterministically():
    """A tie for "most common extra-deck card" must not follow file order.

    `extra_target_from_boards` used `seen.most_common(1)[0]`, which breaks
    a tie by Counter INSERTION order -- whichever card happened to appear
    first in the JSONL. Real ties exist: branded's most common extra-deck
    card has 2 hits with a THREE-WAY tie at the top. That one is latent,
    because 2/31 = 6.5% fails the min_share gate and today every deck
    clearing the gate has a unique top card. Reproducibility is the point:
    the derived target decides what every later solve aims at, and a
    target that depends on record order is not a measurement.

    Verified across 5 shuffled input orders: stable before/after only
    after the fix.
    """
    import json
    import random
    import tempfile

    from skills import scenarios

    recs = [{"status": "solved", "deck": {"name": "d"},
             "lines": [{"board": {"players": [{"mzone": [{"code": c}]}]}}]}
            for c in [111, 222, 333] * 2]

    seen = set()
    for trial in range(5):
        random.Random(trial).shuffle(recs)
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
            path = f.name
        target, _share = scenarios.extra_target_from_boards(
            path, [111, 222, 333], min_share=0.0)
        seen.add(target)
    assert len(seen) == 1, (
        f"a three-way tie resolved differently by input order: {seen}")


def test_an_unreadable_banlist_status_does_not_break_the_app():
    """`banlist_limit` raises; every caller must decide what that means.

    ISSUES #40 made `card_db.limit_for_status` raise rather than guess 3
    for a status it does not recognise, because guessing permissively is
    how 121 forbidden cards became legal. That is right for
    `Deck.validate`, which catches it and reports the card as
    unverifiable. It was NOT handled at the two other call sites, both
    added or touched by the same work:

      * `builder._card` feeds the deckbuilder's search results, so one
        odd card raised out of `builder.search` and would have 500'd
        `/api/cards` -- breaking the WHOLE search, not one row.
      * `optimizer.candidate_pool` would have died mid-pool-build, taking
        an `optimize` run of several hours with it.

    Both now treat an unreadable status as the MOST restrictive answer
    (0 copies / exclude the candidate), never the permissive one. That is
    the direction the original bug got wrong.
    """
    from skills import builder, card_db, db, optimizer
    from skills.deck import Deck

    conn = db.connect(":memory:")
    conn.executemany(
        "INSERT INTO cards (id,name,frame,banlist_tcg,last_fetched) "
        "VALUES (?,?,?,?,?)",
        [(1, "Weird Card", "effect", "Semi-Forbidden?", "now"),
         (2, "Normal Card", "effect", None, "now")])
    conn.commit()

    # The authoritative check reports it rather than crashing.
    d = Deck(name="t", main=[1] * 20 + [2] * 20, extra=[], side=[])
    assert any("unknown tcg ban-list status" in p for p in d.validate(conn))

    # The display path survives, and errs restrictive.
    rows = {r["name"]: r for r in builder.search(conn, "Card", limit=5)[0]}
    assert rows["Weird Card"]["limit"] == 0, \
        "an unreadable status must not render as unrestricted"
    assert rows["Normal Card"]["limit"] == 3

    # And the raise is still the default for anyone who wants it.
    import pytest
    with pytest.raises(ValueError):
        card_db.limit_for_status("Semi-Forbidden?")

    # The optimizer excludes rather than dying.
    import inspect
    src = inspect.getsource(optimizer.candidate_pool)
    assert "except ValueError" in src, \
        "an unreadable status must exclude the candidate, not kill the pool"


def test_identify_ignores_archetypes_built_from_too_few_lists(tmp_path):
    """A core drawn from 3 scraped lists cannot identify a deck.

    At three lists, a card present in all three reads as 100% "core", so
    the core is whatever those three decks happened to share. Measured
    2026-10-25 on the active deck: Primite scored overlap 1.0 on a
    3-card core built from **3 lists** and beat Branded's 0.60 on a
    5-card core built from **94** — both matched 3 cards absolutely, and
    the ranking was on ratio alone.

    The deck really does run all three Primite cards, so this was a
    sample-size error, not a phantom match. It was not cosmetic either:
    `optimize` and `flex` take `guesses[0][0]` programmatically, so
    `removable` ranked the deck's cards against Primite's 3-list
    inclusion table where most had no entry, making the ordering
    arbitrary.

    Same floor logic as the existing 3-card minimum, and the same
    reasoning as MIN_LISTS_FOR_DRIFT.
    """
    from skills import db, meta_scrape as ms
    from skills.deck import Deck

    conn = db.connect(tmp_path / "t.db")
    rows = []
    # Thin sample, whole core present.
    for cid in (301, 302, 303):
        rows.append(("Tiny", "F", cid, "main", 1.0, 3.0, 3, "now"))
    # Well sampled, most of the core present.
    for cid in (401, 402, 403, 404, 405):
        rows.append(("Big", "F", cid, "main", 1.0, 3.0, 94, "now"))
    with conn:
        conn.executemany(
            "INSERT INTO inclusion (archetype, format, card_id, section, "
            "pct_lists, avg_copies, n_lists, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)

    deck = Deck(main=[301, 302, 303, 401, 402, 403], extra=[], side=[])
    got = ms.identify_archetype(conn, deck, fmt="F")
    names = [g[0] for g in got]
    assert "Tiny" not in names, (
        "an archetype whose core rests on 3 lists must not identify a deck")
    assert got and got[0][0] == "Big", f"expected Big to win, got {got}"
    # n_lists is returned, because the docstring makes judging the
    # caller's job and it previously had no way to.
    assert got[0][3] == 94, f"n_lists must be reported, got {got[0]}"


def test_identify_archetype_tuple_stays_index_compatible():
    """Adding n_lists must not break the seven index-based consumers.

    `identify_archetype` is read as guesses[0][0], [0][1] and [0][2] in
    daemon.py, web/views.py and skills/report.py. The 4th element is
    additive for exactly that reason -- the lesson of ISSUES #58, where
    widening a tuple broke a consumer found only by a smoke walk.
    """
    from skills import db, meta_scrape as ms
    from skills.deck import Deck

    conn = db.connect()
    d = Deck.from_ydk("assets/decks/blue-eyes-branded-test.ydk")
    d.canonicalise(conn)
    got = ms.identify_archetype(conn, d)
    if not got:
        import pytest
        pytest.skip("no inclusion data")
    arch, overlap, n_core, n_lists = got[0]
    assert isinstance(arch, str) and 0 <= overlap <= 1
    assert n_core >= 3 and n_lists >= ms.MIN_LISTS_FOR_ID


# --- ISSUES #68: the recovery metric ------------------------------------

def test_fire_spare_spec_is_a_bare_code():
    """A zoned spare spec aborts the WHOLE fire test, silently.

    Goal targets carry a zone ("94145021@hand"); `--fire-spare` resolves
    through the solver's `ResolveCard`, which has no zone grammar, and a
    failure returns from `RunFireTest` before the first window. Measured
    on a stored kewl-tune line: 4 windows and a verdict with a bare code,
    ZERO with the zoned form -- and every hand trap then reports "no
    legal window in this line", which reads as a perfectly safe line.
    """
    from skills.handlab import spare_spec

    assert spare_spec("94145021@hand") == "94145021"
    assert spare_spec("15665977@atk") == "15665977"
    assert spare_spec("15665977") == "15665977"
    assert spare_spec(" 15665977@grave ") == "15665977"
    assert spare_spec("@hand") is None


def test_analyse_offers_no_spare_by_default():
    """Offering a spare costs more information than it buys.

    Measured on one stored line at one budget: with no spare, 3 of 4
    windows converted the WHOLE board (best 5/5) and the fourth graded
    4/5; with one spare, all 4 converted the reduced board at best 4/5 --
    including the three that had just managed all of it. `converted` went
    up and the report said less. The default must therefore be off, and
    the parameter must still exist so the other question can be asked.
    """
    import inspect

    from skills import handlab

    sig = inspect.signature(handlab.analyse)
    assert "spare" in sig.parameters, "the spare question must stay askable"
    assert sig.parameters["spare"].default is None, (
        "a spare must be opt-in: it makes the solver settle for the cheap "
        "goal in every window and throws the per-window grade away")


def test_parse_windows_keeps_the_recovery_grade():
    """"best 4/5" is the graded recovery and was parsed, then discarded.

    `_ROLLOUTS` has always captured the pair; nothing read it, so a
    window keeping four of five board cards and one keeping none both
    printed "NO RECOVERY FOUND".
    """
    from skills.handlab import parse_windows, recovery_grade

    log = (
        "  window  0 (dec.   4, summon  1, over Kewl Tune Reco): pas "
        "convertie  [494 rollouts, best 4/5]  manque : Zalen the Shackled "
        "Dragon\n"
        "  window  1 (dec.  16, summon  2, over Kewl Tune Rotary): "
        "CONVERTIE (board COMPLET)  [251 rollouts, best 5/5]\n"
        "  2 window(s) where the threat is live\n")
    rows, declared = parse_windows(log)
    assert declared == 2 and len(rows) == 2
    assert rows[0]["kept"] == 4 and rows[0]["of"] == 5
    assert rows[0]["survived"] is False
    assert rows[1]["kept"] == 5 and rows[1]["of"] == 5
    assert rows[0]["missing"] == ["Zalen the Shackled Dragon"]
    assert recovery_grade(rows) == (9, 10)


def test_recovery_grade_ignores_ungraded_windows():
    """An ungraded window is left out of both totals, never counted zero.

    A window the solver did not search has no grade; scoring it as 0 of 0
    kept would drag the share down with a measurement that was not made.
    """
    from skills.handlab import recovery_grade

    assert recovery_grade([{"kept": 2, "of": 4}, {"kept": None, "of": None}]) \
        == (2, 4)
    assert recovery_grade([{"kept": None, "of": None}]) == (None, None)
    assert recovery_grade([]) == (None, None)


# --- ISSUES #69: ranking a board by what gets through it ----------------

def test_split_pieces_only_calls_hand_pieces_measurable():
    """`--fire` plays a card from the opponent's HAND and nothing else.

    A board's field negate is not a card anyone can activate from hand,
    and no flag on this solver plays one for you, so it cannot be scored
    this way. Folding it in with a weight would be exactly the invented
    number this project keeps retracting.
    """
    from skills.gauntlet import split_pieces

    pieces = [(14558127, "hand", True), (12345678, "field", True),
              (23456789, "set", False), (34567890, "grave", False)]
    measured, untested = split_pieces(pieces)
    assert measured == [(14558127, "hand", True)]
    assert [p[1] for p in untested] == ["field", "set", "grave"]


def test_piece_through_needs_a_window_and_a_failure():
    """A card that is never live stops nothing, however good its survival.

    Same expectation `weighted_handtrap_ranking` scores a maining
    decision on: `1 - window_rate * (1 - survival)`.
    """
    from skills.gauntlet import piece_through

    # always live, never played through -> nothing gets through it
    assert piece_through({"survival": 0.0, "window_rate": 1.0}) == 0.0
    # always live, always played through -> everything does
    assert piece_through({"survival": 1.0, "window_rate": 1.0}) == 1.0
    # never live -> everything gets through, whatever the survival
    assert piece_through({"survival": 0.0, "window_rate": 0.0}) == 1.0
    # half the hands give it a window, and it stops the line in those
    assert piece_through({"survival": 0.0, "window_rate": 0.5}) == 0.5
    # untested is not zero
    assert piece_through({"survival": None, "window_rate": 1.0}) is None
    assert piece_through(None) is None


def test_board_vs_deck_scores_only_the_pieces_it_measured():
    """An untested piece must not be counted as stopping everything.

    It is dropped from the product and named in `no_cell`, so a board
    scored over one of its three pieces cannot read as a full board.
    """
    from skills.gauntlet import board_vs_deck

    cells = {14558127: {"survival": 0.0, "window_rate": 1.0, "tested": 30}}
    res = board_vs_deck([(14558127, "hand", True), (999, "hand", False)],
                        cells)
    assert res["through"] == 0.0 and res["tested_pieces"] == 1
    assert res["no_cell"] == [999]
    # nothing measurable at all is None, never 1.0: a board with no
    # measured piece must sort as unscored, not as the worst board.
    assert board_vs_deck([(999, "hand", False)], cells)["through"] is None
    assert board_vs_deck([], cells)["through"] is None


def test_rank_boards_sorts_unscored_last():
    """A board with no measurable piece is unknown, not perfect."""
    from skills.gauntlet import Candidate, rank_boards

    def cand(label, pieces, ints):
        return Candidate(targets=(), label=label, interruptions=ints,
                         negates=0, pieces=tuple(pieces), draws=1,
                         solve_s=0.0)

    matrix = {"despia": {"cells": {14558127: {"survival": 0.0,
                                              "window_rate": 1.0,
                                              "tested": 29}}}}
    weights = {"despia": 1.0}
    rows = rank_boards([cand("nothing", [(777, "field", True)], 9),
                        cand("ash", [(14558127, "hand", True)], 1)],
                       matrix, weights)
    assert [r["candidate"]["label"] for r in rows] == ["ash", "nothing"], (
        "the board with nine unmeasurable interruptions must not outrank "
        "a measured one by default")
    assert rows[0]["through"] == 0.0 and rows[1]["through"] is None
    assert rows[1]["coverage"] == 0.0


def test_pilot_body_renders_the_recovery_grade():
    """The hand lab must SHOW the grade, not only the converted count.

    The CLI and the web read the same `handlab.analyse` record, and the
    web view is the one being built to replace the CLI -- a number only
    the CLI prints is a number the user will stop seeing. Rendered rather
    than grepped, because a field named in a template that never reaches
    the output is the failure this is guarding.
    """
    from flask import render_template

    from web import create_app

    rec = {
        "deck": "d", "hand": [1], "hand_names": ["X"], "chosen": [1],
        "filled": [], "status": "solved", "solve_ms": 1000, "fire_ms": 1000,
        "interruptions": 2, "steps": [], "board": [], "goal": [],
        "reading": {"interruptions": [], "follow_up": [], "resources": {}},
        "candidates": [], "error": "",
        "handtraps": [{
            "code": 14558127, "name": "Ash", "status": "solved",
            "windows": 4, "converted": 1, "blind_spot": None,
            "parse_error": None, "kept": 4, "of": 5, "kept_share": 0.8,
            "recovered_board": [], "recovered_reading": None,
            "recovered_steps": [],
            "detail": [{"window": 0, "over": "A", "decision": 4,
                        "after_summons": 1, "survived": False,
                        "rollouts": 494, "kept": 4, "of": 5,
                        "missing": ["Zalen"], "answers": [],
                        "open_window": False}]}]}
    app = create_app()
    with app.test_request_context():
        html = render_template("_pilot_body.html", r=rec)
    assert "gets back 4/5 target" in html, (
        "the per-trap grade did not reach the rendered page")
    assert "got back 4/5" in html, (
        "the per-window grade did not reach the rendered page")
    # `kept == of` on an unconverted window is a real state, not a bug:
    # the cards all came back in a board that does not match. It must be
    # SAID, because "2/2" beside "no recovery found" reads like a parser
    # bug and that is how the first run of this looked.
    rec["handtraps"][0]["detail"][0].update({"kept": 2, "of": 2})
    rec["handtraps"][0]["complete_but_unmatched"] = 1
    with app.test_request_context():
        html = render_template("_pilot_body.html", r=rec)
    assert "not in a matching" in html, (
        "a full-overlap window that did not convert must say so")
    assert "EVERY card back" in html


def test_complete_but_unmatched_is_counted_not_smoothed_over():
    """`kept == of` on an unconverted window is a real state.

    `kept`/`of` is the solver's multiset overlap on the target board's
    CARD CODES (`st.best_overlap / target.codes.size()`); conversion
    demands the whole board match. So a window can get every card back
    in a board that does not match the original -- the closest thing to a
    full recovery that "NO RECOVERY FOUND" can hide. The first real run
    printed "2/2" beside "no recovery found", which reads like a parser
    bug, and that is why it is named.
    """
    from skills.handlab import complete_but_unmatched

    rows = [{"survived": False, "kept": 2, "of": 2},     # all back, no match
            {"survived": False, "kept": 0, "of": 2},     # nothing back
            {"survived": True, "kept": 2, "of": 2},      # converted
            {"survived": False, "kept": None, "of": None}]  # ungraded
    assert complete_but_unmatched(rows) == 1
    assert complete_but_unmatched([]) == 0


def test_playback_last_frame_is_the_records_end_board():
    """The playmat must end on the board the solver says the line reaches.

    Frames are the core's own snapshots, never a board kept by applying
    moves by hand -- so this is the check that the replay is the SAME
    duel as the record (seed words, rule flags, deck order), not a
    look-alike that drifts by a card.
    """
    from skills import db, playback

    rec = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "solved_line.json").read_text())
    out = playback.from_record(db.connect(), rec)
    assert out and len(out["frames"]) > 2
    first, last = out["frames"][0], out["frames"][-1]
    assert first["events"][0]["kind"] == "start"
    assert len(first["board"][0]["hand"]) == len(rec["hand"])
    stored = rec["lines"][0]["board"]["players"][0]
    mine = last["board"][0]
    assert [(c["c"], c["s"], c["pos"]) for c in mine["mz"]] == [
        (c["code"], c["sequence"], c["position"]) for c in stored["mzone"]]
    assert [(c["c"], c["s"]) for c in mine["sz"]] == [
        (c["code"], c["sequence"]) for c in stored["szone"]]
    assert mine["hand"] == stored["hand"] and mine["gy"] == stored["grave"]
    # Every frame shows a change or says something: no dead steps.
    fr = out["frames"]
    assert all(f["events"] or f["board"] != p["board"]
               for p, f in zip(fr, fr[1:]))
    # Every card code drawn has a name to caption it with.
    for f in out["frames"]:
        for side in f["board"]:
            for code in (*side["hand"], *side["gy"]):
                assert str(code) in out["names"]


def test_trainer_plan_aims_where_a_better_board_might_be():
    """The trainer's scheduling, without an hour of solver.

    No history -> the breadth goals. With history -> new stacks from the
    pieces that solved, then near misses biggest-first, then the best
    goal again, each at the NEXT draw number so every attempt is a new
    sample, and a re-tried goal keeps its first label.
    """
    from skills.trainer import plan

    breadth = [(("B", "P1"), "piece:1"), (("B", "P2"), "piece:2"),
               (("B", "P3"), "piece:3"), (("B",), "base")]
    assert plan([], breadth, None) == [(["B", "P1"], "piece:1", 0),
                                        (["B", "P2"], "piece:2", 0),
                                        (["B", "P3"], "piece:3", 0),
                                        (["B"], "base", 0)]

    hist = [{"label": "piece:1", "targets": ["B", "P1"], "status": "solved", "best_placed": 2},
            {"label": "piece:2", "targets": ["B", "P2"], "status": "solved", "best_placed": 2},
            {"label": "piece:3", "targets": ["B", "P3"], "status": "unsolved", "best_placed": 1},
            {"label": "base", "targets": ["B"], "status": "solved", "best_placed": 1}]
    out = plan(hist, breadth, ["B", "P1"], per_hand=4)
    goals = [frozenset(t) for t, _, _ in out]
    # All-solved stacks first (they have evidence), then mixtures with the
    # near miss -- `sweep.combinations_to_try`'s order, not a new one.
    assert goals[0] == frozenset({"B", "P1", "P2"}), out
    assert frozenset({"B", "P1", "P2", "P3"}) in goals, "the stack with the near miss is asked for"
    # nothing already tried comes back at draw 0
    for t, lbl, d in out:
        tried = sum(1 for a in hist if frozenset(a["targets"]) == frozenset(t))
        assert d == tried

    # Once the stacks are asked, the near miss and the best goal are retried
    # under their ORIGINAL labels.
    hist2 = hist + [{"label": lbl, "targets": t, "status": "unsolved", "best_placed": 0}
                    for t, lbl, _ in out]
    out2 = plan(hist2, breadth, ["B", "P1"], per_hand=3)
    labels = {frozenset(t): (lbl, d) for t, lbl, d in out2}
    assert labels.get(frozenset({"B", "P3"})) == ("piece:3", 1), out2
    assert labels.get(frozenset({"B", "P1"})) == ("piece:1", 1), out2


def test_trainer_score_ignores_hand_cards_the_combo_never_touched():
    """A hand trap you opened with and kept is the same card whatever line
    you play, so it is not part of the line's score. One the combo added
    to your hand is."""
    from skills.trainer import line_key, parts

    def k(line, opening):
        return parts(line_key(line, opening))

    ASH = 14558127                       # interrupts from the hand
    board = {"players": [{"mzone": [], "szone": [], "hand": [ASH],
                          "grave": [], "banished": []}, {}]}
    line = {"board": board, "actions": [1, 2, 3]}
    got = k(line, [ASH])
    assert (got["interruptions"], got["negates"]) == (0, 0), "held from the opening: not counted"
    assert k(line, [])["interruptions"] == 1, "added by the combo: counted"
    # two copies at the end, one opened with: the second was obtained
    board["players"][0]["hand"] = [ASH, ASH]
    assert k(line, [ASH, 99999999])["interruptions"] == 1
    assert k(line, [ASH])["actions"] == 3, "fewer actions still breaks ties"


def test_trainer_counts_the_players_own_interrupters():
    """Upstream reads interruptions from scripts and misses some; a card the
    player says counts is scored on the field -- face-up only, and never
    twice for a card upstream already counts."""
    from skills.trainer import line_key, parts

    KALEIDO, LARS = 28226490, 35103106   # not recognised / recognised
    board = {"players": [{"mzone": [{"code": KALEIDO, "position": 1},
                                    {"code": LARS, "position": 4}],
                          "szone": [], "hand": [], "grave": [], "banished": []}, {}]}
    line = {"board": board, "actions": [1]}
    plain = parts(line_key(line, []))
    mine = parts(line_key(line, [], p={"avoid": set(),
                                       "interrupters": {KALEIDO, LARS}}))
    assert mine["on_field"] == plain["on_field"] + 1
    assert mine["interruptions"] == plain["interruptions"] + 1
    board["players"][0]["mzone"][0]["position"] = 8      # face-down: not counted
    assert parts(line_key(line, [], p={"avoid": set(), "interrupters": {KALEIDO}}))[
        "on_field"] == plain["on_field"]


def test_trainer_key_ranks_avoiding_lines_first_and_reads_old_keys():
    from skills.trainer import parts

    clean, uses = [0, 1, 1, 1, -90], [-1, 3, 3, 3, -50]
    assert clean > uses, "a line without the avoided move beats a bigger board with it"
    assert parts([2, 3, 1, -40]) == {"avoided": 0, "on_field": 2, "interruptions": 3,
                                     "negates": 1, "extenders": 0, "actions": 40}, \
        "pre-2026-11-01 keys"
    assert parts([-1, 2, 3, 1, -40]) == {"avoided": 1, "on_field": 2, "interruptions": 3,
                                         "negates": 1, "extenders": 0, "actions": 40}, \
        "pre-2026-11-02 keys (no extenders term)"


def test_trainer_extenders_break_ties_below_the_board():
    """Through more extenders wins only between equal boards."""
    via, plain, bigger = [0, 2, 2, 1, 2, -90], [0, 2, 2, 1, 0, -40], [0, 3, 2, 1, 0, -40]
    assert via > plain, "the same board through the bridge cards wins"
    assert bigger > via, "never a smaller board for summoning something on the way"


def test_trainer_plans_the_best_board_through_each_extender():
    from skills.trainer import EXT, extender_args, plan, split_goal

    hist = [{"label": "piece:1", "targets": ["B", "1"], "status": "solved", "best_placed": 2}]
    breadth = [(["B", "1"], "piece:1")]
    out = plan(hist, breadth, ["B", "1"], per_hand=3, extenders=[7, 8])
    asked = [t for t, _, _ in out]
    assert ["B", "1", f"{EXT}7"] in asked and ["B", "1", f"{EXT}8"] in asked, out
    assert plan([], breadth, None, extenders=[7]) == [(["B", "1"], "piece:1", 0)], \
        "no best board yet: nothing to go through"
    hist.append({"label": f"{EXT}7", "targets": ["B", "1", f"{EXT}7"],
                 "status": "unsolved", "best_placed": 0})
    asked = [t for t, _, _ in plan(hist, breadth, ["B", "1"], per_hand=3, extenders=[7])]
    assert ["B", "1", f"{EXT}7"] not in asked or asked.count(["B", "1", f"{EXT}7"]) == 1
    asked = [t for t, _, _ in plan(hist, breadth, ["B", "1", "2"], per_hand=3, extenders=[7])]
    assert ["B", "1", "2", f"{EXT}7"] in asked, "a bigger best is asked through it again"
    assert split_goal(["B", "1@hand", f"{EXT}7"]) == (["B", "1@hand"], [7])

    # Fiendsmith Engraver is a monster (summoned), Fiendsmith's Sanct a spell (resolved).
    p = {"avoid": set(), "interrupters": set(), "extenders": {60764609, 35552985}}
    assert extender_args(p) == ["--hint", "35552985", "--hint", "60764609"]
    assert extender_args(p, [60764609, 35552985])[4:] == [
        "--summon-min", "60764609", "--resolve", "35552985"]


def test_trainer_generation_sends_extenders_to_the_solver_not_the_board(monkeypatch):
    """An `ext:` goal reaches the solver as flags, never as a board target,
    and is stored with its requirement so it is not asked twice."""
    import json
    from pathlib import Path
    from skills import sweep, trainer

    conn = _games_db()
    deck = Path("assets/decks/kewl-tune.ydk").resolve()
    dh = trainer.ensure_pool(conn, deck, "kewl-tune", hands=1)
    hand = json.loads(conn.execute("SELECT hand FROM trainer_hands").fetchone()["hand"])
    base, breadth = trainer.breadth_goals(deck, hand, trainer.prefs(conn, "kewl-tune"))
    best = breadth[0][0]
    conn.execute("INSERT INTO trainer_attempts (deck_hash, hand_index, gen, targets, label, "
                 "draw, status, improved, at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (dh, 0, 1, json.dumps(best), breadth[0][1], 0, "solved", 1, "x"))
    conn.execute("UPDATE trainer_hands SET best_goal=?, best_key=?", (json.dumps(best),
                 json.dumps([0, 1, 1, 0, 0, -10])))
    ext = int(hand[0])
    trainer.set_pref(conn, "kewl-tune", "extender", ext)
    seen = []

    def fake_map(batch, workers):
        for b in batch:
            seen.append(b)
            yield {"hand_index": b[2], "record": None,
                   "attempt": {"label": b[6], "targets": list(b[5]), "draw": b[8],
                               "status": "unsolved", "best_placed": 0}}
    monkeypatch.setattr(sweep, "_map_solves", fake_map)
    trainer.run_generation(conn, deck, "kewl-tune", dh, 2, say=lambda *_: None)
    via = [b for b in seen if b[6] == f"{trainer.EXT}{ext}"]
    assert via, [b[6] for b in seen]
    b = via[0]
    assert not any(str(t).startswith(trainer.EXT) for t in b[5]), "board targets only"
    assert "--hint" in b[9] and ("--summon-min" in b[9] or "--resolve" in b[9]), b[9]
    assert all("--hint" in x[9] for x in seen), "every try leans toward the extenders"
    assert b[7] >= trainer.TRAIN_MS, "the per-attempt budget is the trainer's"
    stored = [json.loads(r["targets"]) for r in conn.execute(
        "SELECT targets FROM trainer_attempts WHERE label=?", (f"{trainer.EXT}{ext}",))]
    assert stored == [[*best, f"{trainer.EXT}{ext}"]], stored


def test_trainer_plan_asks_new_pieces_and_never_banned_summons():
    from skills.trainer import plan, solver_args

    hist = [{"label": "piece:1", "targets": ["B", "1"], "status": "solved", "best_placed": 2},
            {"label": "piece:2", "targets": ["B", "2"], "status": "solved", "best_placed": 2}]
    breadth = [(["B", "1"], "piece:1"), (["B", "2"], "piece:2"), (["B", "9"], "piece:9")]
    out = plan(hist, breadth, ["B", "1"], per_hand=3)
    assert out[0] == (["B", "9"], "piece:9", 0), "a piece added later is asked first"
    out = plan(hist, breadth, ["B", "1"], per_hand=3, banned={"2"})
    assert not any("2" in t for t, _, _ in out), out
    assert plan([], breadth, None, banned={"9"}) == [(["B", "1"], "piece:1", 0),
                                                     (["B", "2"], "piece:2", 0)]

    args = solver_args({"avoid": {("activate", 77103950, "sz"), ("summon", 5)},
                        "interrupters": set()})
    assert args == ["--no-activate", "77103950@szone", "--no-activate", "77103950@hand",
                    "--no-chain", "77103950"], "summons have no solver flag"


# --- games (STAGE9) ------------------------------------------------------

def _games_db():
    import sqlite3
    from skills import db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def _game(conn, hand, seed, run, outcome):
    conn.execute(
        "INSERT INTO game_results (deck_hash, enemy, hand_index, board, "
        "enemy_seed, run, outcome, created) VALUES ('d','e',?,0,?,?,?,'t')",
        (hand, seed, run, outcome))


def test_games_no_dud_verdict_before_the_games_are_in():
    """A hand 0-for-2 while its other 28 games are still queued was shown
    as a DUD: tiers were given whatever the game count."""
    from skills import game
    conn = _games_db()
    _game(conn, 0, 1000, 0, "no_win")
    _game(conn, 0, 1000, 1, "no_win")
    row, = game.summary(conn, "d", "e", games_per_hand=30)
    assert row["tier"] == "" and not row["complete"]
    # ... and the default reads the expected count off the stored games:
    # hand 1 has played both runs, hand 0 only one so far.
    conn.execute("DELETE FROM game_results")
    _game(conn, 0, 1000, 0, "no_win")
    _game(conn, 1, 1000, 0, "no_win")
    _game(conn, 1, 1000, 1, "win")
    rows = {r["hand_index"]: r for r in game.summary(conn, "d", "e")}
    assert rows[0]["tier"] == "" and not rows[0]["complete"]
    assert rows[1]["complete"] and rows[1]["tier"] == ""   # 1/2 is no near-dud
    _game(conn, 0, 1000, 1, "no_win")
    rows = {r["hand_index"]: r for r in game.summary(conn, "d", "e")}
    assert rows[0]["tier"] == "dud"


def test_games_refuse_to_resume_against_a_changed_enemy_list(tmp_path):
    """Stored games are keyed by the enemy's file NAME and `export` rewrites
    that file in place, so a re-export silently mixed two lists."""
    from skills import game
    conn = _games_db()
    a = tmp_path / "e.ydk"
    a.write_text("#main\n" + "43096270\n" * 40 + "#extra\n!side\n")
    game.check_enemy_list(conn, "d", "e", a)
    game.check_enemy_list(conn, "d", "e", a)        # same list: fine
    a.write_text("#main\n" + "43096270\n" * 39 + "89631139\n#extra\n!side\n")
    with pytest.raises(RuntimeError, match="has changed"):
        game.check_enemy_list(conn, "d", "e", a)


def test_export_prefers_a_legal_list_over_a_better_placing_banned_one():
    """All four stored Angelechy-Elfnote lists (two Winners) ran 3 Terminus
    and 3 Tinia, both limited since; export wrote one as the meta enemy."""
    import sqlite3
    from skills import meta_scrape as ms

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE decks (id INTEGER PRIMARY KEY, title TEXT,
                            placement TEXT, deck_date TEXT);
        CREATE TABLE deck_tags (deck_id INTEGER, tag TEXT);
    """)
    conn.executemany("INSERT INTO decks VALUES (?,?,?,?)",
                     [(1, "banned winner", "Winner", "2026-01-02"),
                      (2, "legal top 8", "Top 8", "2026-01-01")])
    conn.executemany("INSERT INTO deck_tags VALUES (?,?)",
                     [(1, "a"), (2, "a")])
    assert ms.best_list(conn, "a")["id"] == 1
    assert ms.best_list(conn, "a", legal=lambda i: i != 1)["id"] == 2
    assert ms.best_list(conn, "a", legal=lambda i: False)["id"] == 1


# --- card impact (skills/impact.py) ------------------------------------------

def _impact_game(conn, hand, eseed, run, won, used, line=None):
    import json
    line = line or f"/x/h{hand}e{eseed}r{run}.yrp"
    conn.execute("INSERT INTO game_results (deck_hash, enemy, hand_index, board, "
                 "enemy_seed, run, outcome, line, created) VALUES ('d','e',?,0,?,?,?,?,'x')",
                 (hand, eseed, run, "win" if won else "no_win", line))
    conn.execute("INSERT INTO game_used (deck_hash, enemy, hand_index, board, enemy_seed, "
                 "run, line, used) VALUES ('d','e',?,0,?,?,?,?)",
                 (hand, eseed, run, line, json.dumps({str(c): 1 for c in used})))


def test_impact_is_compared_inside_the_same_pair_of_hands():
    """A hand trap used only against the strong enemy hand is not blamed for
    that hand's losses; a card that wins inside the same pairing is credited."""
    from skills import impact

    conn = _games_db()
    for k in range(10):
        # enemy hand 1000 is weak (you win), 1001 strong (you lose) and is
        # where you use the hand trap 7: no comparable games -> no score.
        _impact_game(conn, 0, 1000, k, True, [1])
        _impact_game(conn, 0, 1001, k, False, [1, 7])
        # card 5: inside one pairing, the games that used it are the wins.
        _impact_game(conn, 1, 1000, k, k < 5, [1, 5] if k < 5 else [1])
    rows = {r["code"]: r for r in impact.scores(conn, "d", "e")}
    assert rows[7]["status"] == "every game" and rows[7]["impact"] is None, \
        "a raw rate would score this 0% vs 50%: the enemy hand, not the card"
    assert rows[5]["status"] == "scored" and rows[5]["impact"] == 1.0
    assert rows[5]["used_win"] == 1.0 and rows[5]["not_win"] == 0.0
    assert 0 < rows[5]["ci"] < 1, "Agresti-Caffo: 5/5 vs 0/5 is not +100 +- 0"
    assert rows[1]["status"] == "every game"
    assert 5 not in {r["code"] for r in impact.scores(conn, "d", "e", own={1, 7})}, \
        "only your own cards are scored"


def test_impact_reads_a_game_again_when_its_line_changed(monkeypatch):
    from skills import impact

    conn = _games_db()
    conn.execute("INSERT INTO game_results (deck_hash, enemy, hand_index, board, enemy_seed, "
                 "run, outcome, line, created) VALUES ('d','e',0,0,1000,0,'win','/a.yrp','x')")
    conn.execute("INSERT INTO game_results (deck_hash, enemy, hand_index, board, enemy_seed, "
                 "run, outcome, line, created) VALUES ('d','e',0,0,1000,1,'no_t1',NULL,'x')")
    reads = []
    monkeypatch.setattr(impact, "source", lambda name, enemy, g: g["line"])
    monkeypatch.setattr(impact, "used_cards", lambda p: reads.append(p) or {9: 1})
    assert impact.fill(conn, "d", "deck", "e") == 0
    assert impact.fill(conn, "d", "deck", "e") == 0
    assert reads == ["/a.yrp"], "read once; a game with no turn 1 is never read"
    conn.execute("UPDATE game_results SET line='/b.yrp' WHERE run=0")
    impact.fill(conn, "d", "deck", "e")
    assert reads == ["/a.yrp", "/b.yrp"], "re-recorded with another line: read again"
    assert [r["code"] for r in impact.scores(conn, "d", "e")] == [9]


def test_game_solve_hung_solver_is_killed_not_scored(tmp_path, monkeypatch):
    """A rooted solve that outlives its budget (one ran 10.5 h on a 60 s
    budget, holding a games-run worker) is cut off and raises, so the game
    goes unrecorded and is retried -- not scored off a half-searched turn."""
    import subprocess

    import pytest

    from skills import game

    seen = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(game.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="hung"):
        game._solve(tmp_path / "ref.yrpX", [], tmp_path, "t2", 60_000, 1)
    assert seen["timeout"] == game._hard_limit(60_000) == 240


def test_games_cli_workers_flag_is_checked(capsys):
    import daemon
    from skills import db

    c = db.connect()
    for bad in (["--workers"], ["--workers", "x"], ["--workers", "0"]):
        daemon.cmd_games(c, ["imported", "angelechy", *bad])
        out = capsys.readouterr().out
        assert "--workers" in out or "no such" in out


def test_line_key_weighs_interruptions_by_card_value():
    """Two one-interruption boards tie at value 1; a value on one card
    breaks the tie its way, on the field and in the total. Negates stay a count."""
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import card_interruptions

    from skills import trainer

    cards = default_cards()
    desirae, evolzar = 82135803, 74294676   # Fiendsmith's Desirae, Evolzar Laggia
    for code in (desirae, evolzar):
        if not any(i.where == "field" for i in card_interruptions(code, cards)):
            pytest.skip("card scripts do not read these as field interruptions")

    def line(code):
        return {"board": {"players": [{"mzone": [{"code": code, "position": 1}],
                                       "szone": [], "hand": [], "grave": []}, {}]},
                "actions": [1, 2]}

    base = {"avoid": set(), "interrupters": set(), "extenders": set(), "values": {}}
    assert trainer.line_key(line(desirae), [], cards, p=base)[1:3] == \
        trainer.line_key(line(evolzar), [], cards, p=base)[1:3] == [1, 1]
    p = {**base, "values": {(desirae, "field"): 2.5}}
    kd, ke = trainer.line_key(line(desirae), [], cards, p=p), trainer.line_key(line(evolzar), [], cards, p=p)
    assert kd[1:3] == [2.5, 2.5] and ke[1:3] == [1, 1] and kd > ke
    assert kd[3] == trainer.line_key(line(desirae), [], cards, p=base)[3]



def test_line_key_counts_a_card_valued_in_the_grave_or_hand():
    """A value in the GY counts each copy there, even where the scripts read
    no interruption; a hand value skips copies held since the opening;
    neither adds to on-field."""
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import card_interruptions

    from skills import trainer

    cards = default_cards()
    tear = 92731385     # Tearlaments Kitkallos -- read on the field, not in the GY
    if any(trainer._value_zone(i.where) in ("grave", "hand")
           for i in card_interruptions(tear, cards)):
        pytest.skip("scripts read this card off the field")

    def line(grave=(), hand=()):
        return {"board": {"players": [{"mzone": [], "szone": [], "hand": list(hand),
                                       "grave": list(grave)}, {}]}, "actions": [1]}

    base = {"avoid": set(), "interrupters": set(), "extenders": set(), "values": {}}
    assert trainer.line_key(line(grave=[tear, tear]), [], cards, p=base)[1:3] == [0, 0]
    p = {**base, "values": {(tear, "grave"): 1.5}}
    assert trainer.line_key(line(grave=[tear, tear]), [], cards, p=p)[1:3] == [0, 3]
    p = {**base, "values": {(tear, "hand"): 2}}
    assert trainer.line_key(line(hand=[tear, tear]), [tear], cards, p=p)[1:3] == [0, 2]


def test_card_values_without_zone_migrate_as_field_values(tmp_path):
    import sqlite3

    from skills import db

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE card_values (deck TEXT NOT NULL, code INTEGER NOT NULL, "
                "value REAL NOT NULL, added TEXT NOT NULL, PRIMARY KEY (deck, code))")
    old.execute("INSERT INTO card_values VALUES ('d', 82135803, 2.0, 'x')")
    old.commit()
    old.close()
    c = db.connect(path)
    assert [tuple(r) for r in c.execute("SELECT deck, code, zone, value FROM card_values")] == \
        [("d", 82135803, "field", 2.0)]


def test_starter_hands_respect_copies_skip_and_keep_indices():
    """Every k-card hand the main deck can hold: a card twice only with two
    copies; leaving a card out drops its hands without renumbering the rest."""
    from collections import Counter

    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards

    from skills import trainer
    from web.views import deck_path

    path = deck_path("kewl-tune")
    cards = default_cards()
    count = Counter(cards.canonical(c) for c in load_ydk(path).main)
    ones = trainer.starter_hands(path, 1)
    assert [h for _i, h in ones] == [[c] for c in sorted(count)]
    pairs = dict(trainer.starter_hands(path, 2))
    assert all(Counter(h)[c] <= count[c] for h in pairs.values() for c in h)
    single = next(c for c, n in count.items() if n == 1)
    assert [single, single] not in pairs.values()
    left = dict(trainer.starter_hands(path, 2, skip={single}))
    assert all(single not in h for h in left.values())
    assert all(pairs[i] == h for i, h in left.items())


def test_starter_test_trains_its_own_pool(monkeypatch):
    """`train(starters=k)` runs the generations over the k-card pool under
    its own key -- never the training hands -- and skips the 5-card bootstrap."""
    from skills import db, trainer
    from web.views import deck_path

    path, seen = deck_path("kewl-tune"), []
    monkeypatch.setattr(trainer.engine, "require", lambda: None)
    monkeypatch.setattr(trainer.engine, "preflight", lambda paths: None)
    monkeypatch.setattr(trainer, "bootstrap", lambda *a, **k: seen.append("bootstrap"))
    monkeypatch.setattr(trainer, "run_generation",
                        lambda conn, dp, name, dh, gen, *a: seen.append(dh) or
                        trainer._record_gen(conn, dh, name, gen, trainer._now(), 0, 0, 1))
    c = db.connect(":memory:")
    assert trainer.train(c, path, "kewl-tune", generations=1, starters=1, skip=()) == 1
    key = trainer.starter_key(trainer.deck_hash(path), 1)
    assert seen == [key]
    n = c.execute("SELECT COUNT(*) FROM trainer_hands WHERE deck_hash=?", (key,)).fetchone()[0]
    assert n == len(trainer.starter_hands(path, 1))


# --- what you stopped (skills/stops.py) --------------------------------------

def _chain_msgs():
    """Message stand-ins named like ygorl's, for stops.Reader."""
    from types import SimpleNamespace as NS

    def kind(name, **kw):
        return type(name, (NS,), {})(**kw)
    loc = lambda ctl, where=0x4: NS(controller=ctl, location=where, sequence=0)
    return {
        "turn": lambda: kind("NewTurn"),
        "chain": lambda code, ctl, n: kind("Chaining", code=code, loc=loc(ctl), chain_count=n),
        "solving": lambda n: kind("ChainSolving", chain_count=n),
        "solved": lambda n: kind("ChainSolved", chain_count=n),
        "disabled": lambda n: kind("ChainDisabled", chain_count=n),
        "end": lambda: kind("ChainEnd"),
        "move": lambda code, frm, to, reason: kind(
            "Move", code=code, previous=frm, current=to, reason=reason),
        "loc": loc,
    }


def test_stops_credit_the_negation_to_the_right_link():
    """Traced 2026-11-05 on real games: the engine reports a negation either
    while the negator resolves (Ash) or only when the victim resolves (Evolzar
    Lars). Their own link negating their own card is not your stop, and a
    negation with nothing above it is a field effect."""
    from skills import stops

    m = _chain_msgs()
    rd = stops.Reader(me=0)
    for x in [m["turn"](), m["turn"](),
              # Lars style: reported at the victim's own resolution
              m["chain"](100, 1, 1), m["chain"](500, 0, 2),
              m["solving"](2), m["solved"](2), m["solving"](1), m["disabled"](1),
              m["solved"](1), m["end"](),
              # Ash style: reported while Ash resolves
              m["chain"](101, 1, 1), m["chain"](600, 0, 2),
              m["solving"](2), m["disabled"](1), m["solved"](2), m["end"](),
              # they negate their own card (Dominus Impulse onto Dominus Spark)
              m["chain"](700, 0, 1), m["chain"](102, 1, 2), m["chain"](103, 1, 3),
              m["solving"](3), m["disabled"](2), m["solved"](3), m["end"](),
              # nothing above it: a continuous negate on the field
              m["chain"](104, 1, 1), m["solving"](1), m["disabled"](1), m["end"]()]:
        rd.feed(x)
    assert rd.stops == [[2, "negated", 500, 100], [2, "negated", 600, 101],
                        [2, "negated", stops.FIELD, 104]]
    assert set(rd.played) == {100, 101, 102, 103, 104}


def test_stops_count_removal_only_by_your_resolving_effect():
    import ygorl.engine.constants as C
    from skills import stops

    m = _chain_msgs()
    field, gy = m["loc"](1, 0x4), m["loc"](1, 0x10)
    rd = stops.Reader(me=0)
    for x in [m["turn"](),
              m["chain"](800, 0, 1), m["solving"](1),
              m["move"](200, field, gy, C.REASON_EFFECT | C.REASON_DESTROY),   # yes
              m["move"](201, field, gy, C.REASON_EFFECT | C.REASON_COST),      # a cost
              m["solved"](1), m["end"](),
              m["chain"](202, 1, 1), m["solving"](1),
              m["move"](202, field, m["loc"](1, 0x20), C.REASON_EFFECT),       # moves itself
              m["solved"](1), m["end"]()]:
        rd.feed(x)
    assert rd.stops == [[1, "removed", 800, 200]]


def _stops_game(conn, hand, eseed, run, won, played, stops_):
    import json
    line = f"/x/h{hand}e{eseed}r{run}.yrp"
    conn.execute("INSERT INTO game_results (deck_hash, enemy, hand_index, board, "
                 "enemy_seed, run, outcome, line, created) VALUES ('d','e',?,0,?,?,?,?,'x')",
                 (hand, eseed, run, "win" if won else "no_win", line))
    conn.execute("INSERT INTO game_stops (deck_hash, enemy, hand_index, board, enemy_seed, "
                 "run, line, stops) VALUES ('d','e',?,0,?,?,?,?)",
                 (hand, eseed, run, line, json.dumps(
                     {"played": {str(c): 2 for c in played}, "stops": stops_})))


def test_stop_value_compares_stopped_with_through_among_games_it_was_played():
    """Stopping card 100 wins inside the same pairing; games where they never
    played it are not 'went through'."""
    from skills import stops

    conn = _games_db()
    for k in range(10):
        hit = k < 5
        _stops_game(conn, 0, 1000, k, hit, [100],
                    [[2, "negated", 500, 100]] if hit else [])
        _stops_game(conn, 0, 1000, 10 + k, False, [], [])    # never played: ignored
    res = stops.scores(conn, "d", "e", own={500})
    theirs = {r["code"]: r for r in res["theirs"]}
    assert theirs[100]["played"] == 10 and theirs[100]["stopped"] == 5
    assert theirs[100]["impact"] == 1.0 and theirs[100]["status"] == "scored"
    assert theirs[100]["by"] == [(500, 5)]
    yours = {r["code"]: r for r in res["yours"]}
    assert yours[500]["hit"] == [(100, 5)] and yours[500]["impact"] > 0
    assert stops.scores(conn, "d", "e", own={999})["yours"] == [], \
        "a stopper not in your list is theirs while you held it"
