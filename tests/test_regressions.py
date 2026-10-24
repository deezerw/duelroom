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


def _stub_defend(monkeypatch, rates):
    """defend() keyed by candidate label, so no solver is needed."""
    from skills import gauntlet

    def fake(our_deck, our_hand, candidate, their_decks, **kw):
        rate = rates[candidate.label]
        return {"candidate": candidate.to_json(), "rows": [],
                "tested": 10, "stopped": int(rate * 10), "stop_rate": rate}
    monkeypatch.setattr(gauntlet, "defend", fake)


def _stub_floor(monkeypatch, rate):
    """The do-nothing zero point, also solver-free."""
    from skills import gauntlet
    monkeypatch.setattr(gauntlet, "floor", lambda *a, **k: {
        "rows": [], "tested": 10, "through": int((1 - rate) * 10),
        "stopped": int(rate * 10), "stop_rate": rate})


def _cand(label, interruptions, code):
    from skills.gauntlet import Candidate
    return Candidate(targets=(str(code),), label=label,
                     interruptions=interruptions, negates=0,
                     pieces=((code, "field", False),), draws=1, solve_s=1.0)


def test_the_do_nothing_board_is_carried_as_a_control(monkeypatch, tmp_path):
    """Without it a stop rate has no scale: "stopped 8 of 12" says nothing
    unless the board that builds nothing stops fewer."""
    from skills import gauntlet
    cands = [_cand("stack:2", 5, 1), _cand("piece:9", 4, 2),
             _cand("base", 2, 3)]
    _stub_candidates(monkeypatch, cands)
    _stub_defend(monkeypatch, {"stack:2": 0.8, "piece:9": 0.6, "base": 0.2})
    _stub_floor(monkeypatch, 0.1)
    rep = gauntlet.rank("deck.ydk", [1, 2, 3, 4, 5], [("them.ydk", "1")],
                        keep=2)
    labels = [r["candidate"]["label"] for r in rep["results"]]
    assert "base" in labels, "the control was dropped by the keep cut"
    assert rep["control_rate"] == 0.2
    assert rep["discriminated"] is True


def test_identical_stop_rates_are_reported_as_no_resolution(monkeypatch):
    """Three boards that all stop everything is a test with no power, not
    a three-way tie to read an ordering into."""
    from skills import gauntlet
    cands = [_cand("stack:2", 5, 1), _cand("base", 2, 3)]
    _stub_candidates(monkeypatch, cands)
    _stub_defend(monkeypatch, {"stack:2": 1.0, "base": 1.0})
    _stub_floor(monkeypatch, 0.5)
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
    assert 'acc["through"] > 0' in src, (
        "the opponent no longer gets the benefit of every draw")


def test_a_board_that_only_matches_the_floor_is_not_a_result(monkeypatch):
    """gauntlet's first real run had two boards stopping 18/18, which is
    either two excellent boards or a task nobody could complete. The
    `base` rung cannot answer that -- "do nothing, hold what the hand
    holds" held Droll and Effect Veiler, two live interactions. Only a
    board of vanilla monsters is a zero."""
    from skills import gauntlet
    cands = [_cand("stack:2", 5, 1), _cand("base", 2, 3)]
    _stub_candidates(monkeypatch, cands)
    _stub_defend(monkeypatch, {"stack:2": 1.0, "base": 0.9})
    _stub_floor(monkeypatch, 1.0)
    rep = gauntlet.rank("deck.ydk", [1, 2, 3, 4, 5], [("them.ydk", "1")],
                        keep=2)
    assert rep["beats_floor"] is False, (
        "a board no better than 40 vanilla monsters was called a result")
    _stub_floor(monkeypatch, 0.5)
    rep = gauntlet.rank("deck.ydk", [1, 2, 3, 4, 5], [("them.ydk", "1")],
                        keep=2)
    assert rep["beats_floor"] is True


def test_the_floor_board_interrupts_nothing_and_can_be_summoned():
    """Two halves, and the first version only had one.

    It used Blue-Eyes White Dragon -- no effect, so it passed the
    interruption half -- and level 8, so a hand of five of them cannot
    normal summon one without two tributes. All three decks came back
    "inert board not built" and the floor measured nothing, in exactly
    the shape of a real result."""
    from skills import db
    from skills.gauntlet import INERT
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import card_interruptions

    assert card_interruptions(INERT, default_cards()) == ()
    row = db.connect().execute(
        "SELECT name, type, level FROM cards WHERE id = ?", (INERT,)).fetchone()
    assert row is not None, "the floor card is not in the card mirror"
    assert "Normal Monster" in row["type"], row["type"]
    assert row["level"] and row["level"] <= 4, (
        f"{row['name']} is level {row['level']}: it cannot be normal summoned "
        f"from a hand holding nothing else, so the floor never builds")


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


def test_both_going_second_callers_deal_through_the_decklist():
    """gauntlet._attack and breaking.going_second both pass a rewritten
    decklist, not the original, or the hand they sampled is ignored."""
    import inspect
    from skills import breaking, gauntlet
    for fn in (gauntlet._attack, breaking.going_second):
        src = inspect.getsource(fn)
        assert "deck_with_hand" in src, (
            f"{fn.__name__} still deals the hand with --hand alone")


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


def test_the_dealt_deck_goes_into_the_start_replay():
    """Player 1's deck AND hand come from the start replay; `--deck` and
    `--hand` do not reach them. Proven 2026-10-24: with `--deck` pointing
    at a rewritten copy, the solver still dealt the last five entries of
    the ORIGINAL light-and-darkness-ritual.ydk (3x Pot of Sloth, 2x Black
    Chaos). So the reordered list must be built into the REPLAY, which
    means one board build per hand."""
    import inspect
    from skills import breaking, gauntlet

    for fn in (gauntlet.defend, gauntlet.floor):
        src = inspect.getsource(fn)
        i_deal = src.index("deck_with_hand")
        i_start = min((src.index(x) for x in ("inert_start(", "our_board_start(")
                       if x in src))
        assert i_deal < i_start, (
            f"{fn.__name__} builds the start before dealing the hand into "
            f"the decklist, so the replay carries the original order")
        assert "for h in range(hands)" in src
        # the start builder must receive `dealt`, not the original path
        assert ("inert_start(dealt" in src or "our_board_start(our_deck, dealt"
                in src), f"{fn.__name__} passes the original deck to the start"

    gs = inspect.getsource(breaking.going_second)
    assert "deck_with_hand" in gs and "build_start(their_deck, dealt_pre" in gs, (
        "going_second builds its start from the undealt deck")


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
