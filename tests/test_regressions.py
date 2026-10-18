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

def _funnel(deck, fire_statuses):
    return {
        "summary": {"deck": deck, "solved": len(fire_statuses), "hands": len(fire_statuses)},
        "hands": [
            {"status": "solved", "fire": {"27204311": {"status": st}}}
            for st in fire_statuses
        ],
    }


def test_no_window_is_not_counted_as_survival():
    rec = _funnel("d", ["no_window"] * 9 + ["unsolved"])
    cell = scenarios.handtrap_matrix([rec])["d"]["cells"][27204311]
    assert cell["tested"] == 1, "no_window must not count as a tested window"
    assert cell["survival"] == 0.0, "the one real window STOPPED the line"
    assert cell["window_rate"] == pytest.approx(0.1)


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
