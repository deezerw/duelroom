"""Web UI tests. Security behaviours first, because this port faces the
internet and every item here corresponds to a real finding on this
machine: a session cookie clash between stockroom and flipper, an open
redirect in both, and a brute-forced qBittorrent WebUI."""

import sys
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import create_app  # noqa: E402
from web.auth import MAX_ATTEMPTS, safe_next  # noqa: E402


@pytest.fixture
def client():
    # The limiter is shared state by design (see auth._rate_limited), so
    # each test starts from a clean slate or the brute-force test locks
    # out every test after it.
    from web.auth import clear_attempts
    clear_attempts()
    app = create_app({"PASSWORD_HASH": generate_password_hash("pw"),
                      "TESTING": True})
    yield app.test_client()
    clear_attempts()


def test_pages_require_login(client):
    for path in ("/", "/import", "/deck/kewl-tune"):
        r = client.get(path)
        assert r.status_code == 302, f"{path} served without a session"
        assert "/login" in r.headers["Location"]


def test_wrong_password_does_not_grant_access(client):
    client.post("/login", data={"password": "nope"})
    assert client.get("/").status_code == 302


def test_right_password_grants_access(client):
    client.post("/login", data={"password": "pw"})
    assert client.get("/").status_code == 200


def test_rate_limit_survives_a_process_restart(client):
    # An in-process counter resets when a worker recycles, and gives each
    # gunicorn worker its own allowance. This one is in the database.
    from web.auth import _rate_limited, _record
    for _ in range(MAX_ATTEMPTS):
        _record("10.9.9.9")
    assert _rate_limited("10.9.9.9") is True
    assert _rate_limited("10.9.9.8") is False, "limits are per address"


def test_rate_limit_blocks_brute_force_including_the_real_password(client):
    for _ in range(9):
        client.post("/login", data={"password": "x"})
    # the correct password must ALSO be refused while limited, or the
    # limiter only slows an attacker down between guesses
    assert client.post("/login", data={"password": "pw"}).status_code == 429


@pytest.mark.parametrize("target,expect", [
    ("https://evil.example", None),
    ("//evil.example", None),
    ("javascript:alert(1)", None),
    ("/decks?x=1", "/decks?x=1"),
    ("", None),
    (None, None),
])
def test_safe_next_only_allows_local_paths(target, expect):
    assert safe_next(target) == expect


def test_session_cookie_is_not_the_flask_default():
    app = create_app({"PASSWORD_HASH": "x"})
    assert app.config["SESSION_COOKIE_NAME"] == "duelroom_session", (
        "stockroom and flipper both used the default and logged users "
        "between apps")


# --- job queue -----------------------------------------------------------
# Solver runs take hours and take 6 of 8 cores. The web app only
# enqueues; a single worker process claims and runs, which makes
# one-at-a-time structural rather than a convention.

def _logged_in(client):
    client.post("/login", data={"password": "pw"})
    return client


def test_only_known_job_kinds_can_be_queued(client):
    _logged_in(client)
    r = client.post("/deck/kewl-tune/run/rm-rf", follow_redirects=True)
    assert b"Unknown job type" in r.data
    from skills import db
    from web import jobs
    assert not any(j["kind"] == "rm-rf" for j in jobs.recent(db.connect(), 50))


def test_queued_jobs_cancel_but_running_ones_do_not(tmp_path):
    # Killing a running solver mid-sweep leaves partial records that read
    # as complete — the exact bug this queue exists to avoid.
    from skills import db
    from web import jobs
    conn = db.connect(tmp_path / "j.db")
    qid = jobs.enqueue(conn, "gate", deck="d", hands=2)
    assert jobs.cancel(conn, qid) is True
    assert jobs.get(conn, qid)["state"] == "cancelled"

    rid = jobs.enqueue(conn, "gate", deck="d", hands=2)
    with conn:
        conn.execute("UPDATE jobs SET state='running' WHERE id=?", (rid,))
    assert jobs.cancel(conn, rid) is False, "a running job must not be cancellable"
    assert jobs.get(conn, rid)["state"] == "running"


def test_active_reports_the_running_job_for_views_to_check(tmp_path):
    from skills import db
    from web import jobs
    conn = db.connect(tmp_path / "j2.db")
    assert jobs.active(conn) is None
    jid = jobs.enqueue(conn, "gate", deck="d")
    with conn:
        conn.execute("UPDATE jobs SET state='running' WHERE id=?", (jid,))
    assert jobs.active(conn)["id"] == jid


def test_claim_takes_the_oldest_queued_job_once(tmp_path):
    from skills import db
    from web import jobs
    conn = db.connect(tmp_path / "j3.db")
    first = jobs.enqueue(conn, "gate", deck="a")
    jobs.enqueue(conn, "gate", deck="b")
    got = jobs.claim(conn)
    assert got["id"] == first
    assert jobs.claim(conn)["deck"] == "b", "claim must not re-serve a running job"


# --- analysis views ------------------------------------------------------
# The point of these pages is that uncertainty survives the trip from
# terminal to browser. A stat tile looks authoritative in a way a
# terminal warning beside its own number does not.

def test_analysis_pages_render(client):
    _logged_in(client)
    for path in ("/report", "/matrix", "/threats", "/drill"):
        assert client.get(path, follow_redirects=True).status_code == 200


def test_report_draws_an_interval_not_just_a_number(client):
    _logged_in(client)
    html = client.get("/report/kewl-tune").data.decode()
    assert 'class="iv' in html, "solve rate must render as a confidence band"
    assert "hands" in html, "provenance must state the sample size"


def test_report_greys_a_deck_whose_target_is_untrustworthy(client):
    _logged_in(client)
    html = client.get("/report/Blitzclique").data.decode()
    assert "untrusted" in html, "a flagged target must grey its own panel"
    assert "must be DRAWN" in html, "and state why"


def test_report_pairs_maxx_c_survival_with_its_cost(client):
    # Survival alone reads as "fine" — lines do complete through it.
    _logged_in(client)
    html = client.get("/report/kewl-tune").data.decode()
    assert "Surviving it is not beating it" in html


def test_matrix_states_which_direction_is_good(client):
    _logged_in(client)
    html = " ".join(client.get("/matrix").data.decode().split())
    assert "Lower is better for you" in html
    assert "unmeasured, not harmless" in html, "a dash must not read as harmless"


def test_matrix_says_when_it_excludes_your_deck(client):
    # Otherwise the table reads as your matchup picture while containing
    # nothing about your deck.
    from skills import db
    c = db.connect()
    db.set_meta(c, "active_deck", "my-despia")
    c.commit()
    _logged_in(client)
    html = client.get("/matrix").data.decode()
    assert "does not include" in html


# --- deckbuilder ---------------------------------------------------------
#
# The builder's whole risk is that it becomes a second, divergent opinion
# about what a legal deck is. These check that it has no opinion at all.

@pytest.fixture
def loggedin(client):
    client.post("/login", data={"password": "pw"})
    return client


def test_builder_requires_login(client):
    for path in ("/build", "/api/cards"):
        assert client.get(path).status_code == 302, f"{path} served without a session"
    assert client.post("/api/validate", json={}).status_code == 302
    assert client.post("/build/save", json={}).status_code == 302


def test_card_search_ranks_meta_played_first(loggedin):
    d = loggedin.get("/api/cards?q=ash").get_json()
    names = [c["name"] for c in d["cards"]]
    assert "Ash Blossom & Joyous Spring" in names
    played = [i for i, c in enumerate(d["cards"]) if c["play"]]
    unplayed = [i for i, c in enumerate(d["cards"]) if not c["play"]]
    assert played and unplayed
    assert max(played) < min(unplayed), "an unplayed card outranks a played one"


def test_blank_search_returns_the_meta_not_nothing(loggedin):
    d = loggedin.get("/api/cards?q=").get_json()
    assert d["total"] > 10000, "blank search should see the whole pool"
    assert d["cards"][0]["play"], "blank search should lead with played cards"


def test_search_sorts_by_support_not_by_raw_rate(loggedin):
    """100% of two lists must not outrank 85% of forty."""
    from skills import builder, db
    c = db.connect()
    idx = builder.play_index(c)
    thin = [v for v in idx.values() if v["n"] <= 2 and v["pct"] >= 0.99]
    thick = [v for v in idx.values() if v["n"] >= 40 and 0.8 <= v["pct"] < 1.0]
    if not (thin and thick):
        pytest.skip("no thin/thick pair in the current scrape")
    assert max(t["rank"] for t in thin) < max(t["rank"] for t in thick)


def test_extra_deck_filter_excludes_main_deck_cards(loggedin):
    d = loggedin.get("/api/cards?q=dragon&section=extra").get_json()
    assert d["cards"], "no extra-deck dragons found"
    assert all(c["extra"] for c in d["cards"])


def test_validate_is_the_same_code_the_cli_runs(loggedin):
    from skills import db
    from skills.deck import Deck
    c = db.connect()
    ash = c.execute("SELECT id FROM cards WHERE name = ?",
                    ("Ash Blossom & Joyous Spring",)).fetchone()["id"]
    payload = {"main": [ash] * 4, "extra": [], "side": []}
    web_said = loggedin.post("/api/validate", json=payload).get_json()
    cli_said = Deck(main=[ash] * 4).validate(c)
    assert web_said["problems"] == cli_said
    assert web_said["legal"] is False


def test_validate_rejects_an_absurd_payload(loggedin):
    r = loggedin.post("/api/validate", json={"main": list(range(5000))})
    assert r.status_code == 400
    r = loggedin.post("/api/validate", json={"main": "forty cards"})
    assert r.status_code == 400


def test_saving_an_illegal_list_asks_first(loggedin, tmp_path, monkeypatch):
    from web import views
    monkeypatch.setattr(views, "DECKS", tmp_path)
    monkeypatch.setattr("skills.importer.DECKS", tmp_path)
    r = loggedin.post("/build/save", json={"name": "scratch", "main": [46986414]})
    assert r.status_code == 409
    assert r.get_json()["confirm"] is True
    assert not list(tmp_path.glob("*.ydk")), "an illegal list was saved silently"

    r = loggedin.post("/build/save",
                      json={"name": "scratch", "main": [46986414], "force": True})
    assert r.status_code == 200
    assert (tmp_path / "scratch.ydk").exists()


def test_save_cannot_write_outside_the_deck_directory(loggedin, tmp_path, monkeypatch):
    from skills import db
    from web import views
    monkeypatch.setattr(views, "DECKS", tmp_path)
    monkeypatch.setattr("skills.importer.DECKS", tmp_path)
    c = db.connect()
    main = [r["id"] for r in c.execute("SELECT id FROM cards LIMIT 1")] * 40
    r = loggedin.post("/build/save", json={"name": "../../../etc/pwned",
                                           "main": main, "force": True})
    assert r.status_code == 200
    assert not Path("/etc/pwned.ydk").exists()
    written = list(tmp_path.glob("*.ydk"))
    assert len(written) == 1 and ".." not in written[0].name


def test_save_round_trips_through_the_deck_model(loggedin, tmp_path, monkeypatch):
    from skills import db
    from skills.deck import Deck
    from web import views
    monkeypatch.setattr(views, "DECKS", tmp_path)
    monkeypatch.setattr("skills.importer.DECKS", tmp_path)
    c = db.connect()
    ash = c.execute("SELECT id FROM cards WHERE name = ?",
                    ("Ash Blossom & Joyous Spring",)).fetchone()["id"]
    main = [ash] * 3 + [r["id"] for r in c.execute(
        "SELECT id FROM cards WHERE frame NOT IN "
        "('fusion','synchro','xyz','link','token','skill') AND id != ? LIMIT 37",
        (ash,))]
    r = loggedin.post("/build/save", json={"name": "round-trip", "main": main,
                                           "force": True})
    assert r.status_code == 200, r.get_json()
    back = Deck.from_ydk(tmp_path / "round-trip.ydk")
    assert sorted(back.main) == sorted(main)


def test_builder_page_actually_renders(loggedin):
    """The API tests above all pass with a broken template. This one does not."""
    import json
    import re
    r = loggedin.get("/build")
    assert r.status_code == 200
    h = r.get_data(as_text=True)
    seed = json.loads(re.search(r'<script id="seed"[^>]*>(.*?)</script>',
                                h, re.S).group(1))
    assert set(seed["sections"]) == {"main", "extra", "side"}
    assert "/api/validate" in h, "the page does not ask the server about legality"


def test_a_mistyped_deck_name_says_so(loggedin):
    h = loggedin.get("/build/no-such-deck").get_data(as_text=True)
    assert "No saved deck called" in h
    assert "No saved deck called" not in loggedin.get("/build").get_data(as_text=True)


def test_search_ranks_the_decks_own_archetype_first(loggedin):
    """Building Branded, a Branded card outranks an equally-played one
    from somewhere else — and shows Branded's own rate, not the other's."""
    d = loggedin.get("/api/cards?q=dragon&arch=Branded").get_json()
    played = [c for c in d["cards"] if c["play"]]
    assert played, "no played dragons"
    own = [i for i, c in enumerate(played) if c["play"]["own"]]
    other = [i for i, c in enumerate(played) if not c["play"]["own"]]
    assert own, "Branded plays no card matching 'dragon'"
    assert not other or max(own) < min(other)
    assert all(c["play"]["archetype"] == "Branded"
               for c in played if c["play"]["own"])
