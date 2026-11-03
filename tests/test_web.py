"""Web UI tests. Security behaviours first, because this port faces the
internet and every item here corresponds to a real finding on this
machine: a session cookie clash between stockroom and flipper, an open
redirect in both, and a brute-forced qBittorrent WebUI."""

import sys
import json
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import create_app  # noqa: E402
from web.auth import MAX_ATTEMPTS, safe_next  # noqa: E402


def flat(html):
    """HTML with runs of whitespace collapsed.

    Prose in a template wraps wherever the line got long, so asserting on
    a sentence matches or not depending on indentation. Three assertions
    in this file have failed that way against correct pages.
    """
    import re as _re
    return _re.sub(r"\s+", " ", html)


@pytest.fixture(autouse=True)
def keep_the_installation_intact():
    """Tests run against the real database. Put back what they change.

    Several of these drive routes that set the active deck -- /import and
    the matrix test do it deliberately -- and the suite used to leave the
    installation pointing at a deck called "evil" that another test had
    just deleted. Every page defaulting to the active deck then broke,
    and nothing in the suite noticed, because the suite had already
    passed.
    """
    from skills import db
    conn = db.connect()
    was = db.get_meta(conn, "active_deck")
    yield
    conn = db.connect()
    if db.get_meta(conn, "active_deck") != was:
        db.set_meta(conn, "active_deck", was)
        conn.commit()


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


# --- card art ------------------------------------------------------------

def test_card_art_requires_login(client):
    assert client.get("/img/small/14558127.jpg").status_code == 302


def test_uncached_art_is_a_placeholder_that_is_not_cached(loggedin):
    """A missing image must not be remembered as missing by the browser,
    or the art never appears once the fetch job reaches that card."""
    r = loggedin.get("/img/small/999999999.jpg")
    assert r.status_code == 200
    assert r.mimetype == "image/svg+xml"
    assert "no-store" in r.headers["Cache-Control"]


def test_cached_art_is_served_locally_and_cached_hard(loggedin):
    from pathlib import Path
    from skills import images
    have = sorted(images.dir_for("small").glob("*.jpg"))
    if not have:
        pytest.skip("no art cached yet")
    r = loggedin.get(f"/img/small/{Path(have[0]).stem}.jpg")
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
    assert "max-age=31536000" in r.headers["Cache-Control"]


def test_unknown_image_size_is_not_a_path(loggedin):
    assert loggedin.get("/img/bogus/1.jpg").status_code == 404
    assert loggedin.get("/img/..%2f..%2fetc/1.jpg").status_code in (404, 308)


def test_pages_never_reference_ygoprodecks_image_host(loggedin):
    """Their terms forbid hotlinking. Every <img> must point at us."""
    for path in ("/build", "/deck/my-despia", "/maintenance"):
        h = loggedin.get(path).get_data(as_text=True)
        assert "images.ygoprodeck.com" not in h, f"{path} hotlinks card art"


# --- jobs that take no deck ----------------------------------------------

def test_deckless_route_refuses_a_job_that_needs_a_deck(loggedin):
    r = loggedin.post("/run/gate", follow_redirects=False)
    assert r.status_code == 302 and "/maintenance" in r.headers["Location"]


def test_image_job_rejects_an_unknown_scope(loggedin):
    from skills import db
    from web import jobs
    c = db.connect()
    before = len(jobs.recent(c, 100))
    r = loggedin.post("/run/images", data={"scope": "../../etc"})
    assert "/maintenance" in r.headers["Location"]
    assert len(jobs.recent(c, 100)) == before, "a bad scope was queued anyway"


def test_progress_reads_an_explicit_denominator(tmp_path):
    from web import jobs
    log = tmp_path / "j.log"
    log.write_text("starting\nprogress 300/14580 — 300 fetched\n")
    p = jobs.progress({"log_path": str(log)})
    assert (p["hands"], p["total"]) == (300, 14580)


# --- hardening pass ------------------------------------------------------

def test_an_uploaded_filename_cannot_escape_the_temp_directory(
        loggedin, tmp_path, monkeypatch):
    """"duelroom-upload-../../../etc/x" normalises to /etc/x, because the
    prefix becomes a directory component the following ".." pops off.

    DECKS is redirected because the first version of this test imported
    into the real assets/decks and left a deck called "evil" sitting in
    the user's list.
    """
    import io
    from pathlib import Path

    from web import views
    monkeypatch.setattr(views, "DECKS", tmp_path)
    monkeypatch.setattr("skills.importer.DECKS", tmp_path)
    evil = "../../../../tmp/duelroom-pwned.ydk"
    loggedin.post("/import", data={
        "file": (io.BytesIO(b"#main\n46986414\n"), evil),
        "name": "evil"}, content_type="multipart/form-data")
    assert not Path("/tmp/duelroom-pwned.ydk").exists()
    assert not (Path(__file__).parent.parent / "assets" / "decks"
                / "evil.ydk").exists(), "the test wrote into the real deck list"


def test_deck_routes_refuse_a_name_that_is_not_a_plain_filename(loggedin):
    for name in ("..", "....", "%2e%2e", "a/../b"):
        for route in (f"/deck/{name}", f"/deck/{name}/ydk", f"/report/{name}",
                      f"/build/{name}"):
            r = loggedin.get(route)
            assert r.status_code in (302, 404, 308), f"{route} -> {r.status_code}"


def test_import_will_not_fetch_arbitrary_urls(loggedin):
    """This box also runs Pi-hole, qBittorrent and three other Flask apps,
    several on plain HTTP behind the firewall."""
    from skills import importer
    for url in ("http://127.0.0.1:8446/", "http://10.0.0.96/admin",
                "file:///etc/passwd", "http://evil.test/ygoprodeck.com"):
        with pytest.raises(importer.ImportError_):
            importer.check_url(url)
    assert importer.check_url("https://ygoprodeck.com/deck/x-1")


def test_import_url_rejection_reaches_the_user(loggedin):
    r = loggedin.post("/import", data={"source": "http://10.0.0.96/admin"})
    assert r.status_code == 400
    assert "ygoprodeck" in r.get_data(as_text=True)


def test_security_headers_are_set(loggedin):
    r = loggedin.get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "img-src 'self' data:" in csp, "card art could be hotlinked"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "no-store" in r.headers["Cache-Control"]


def test_card_art_keeps_its_own_cache_header(loggedin):
    from pathlib import Path
    from skills import images
    have = sorted(images.dir_for("small").glob("*.jpg"))
    if not have:
        pytest.skip("no art cached yet")
    r = loggedin.get(f"/img/small/{Path(have[0]).stem}.jpg")
    assert "max-age=31536000" in r.headers["Cache-Control"], \
        "the blanket no-store header clobbered the art cache"


def test_a_job_cannot_be_queued_for_a_deck_that_does_not_exist(loggedin):
    from skills import db
    from web import jobs
    c = db.connect()
    before = len(jobs.recent(c, 200))
    loggedin.post("/deck/no-such-deck/run/gate")
    assert len(jobs.recent(c, 200)) == before


# --- pilot ---------------------------------------------------------------

def test_pilot_requires_login(client):
    # `loggedin` is the same client object as `client`, so the gate check
    # has to live in a test that never logs in.
    assert client.get("/pilot/my-despia").status_code == 302
    assert client.post("/pilot/my-despia", data={"card": "1"}).status_code == 302


def test_pilot_page_renders(loggedin):
    h = loggedin.get("/pilot/my-despia").get_data(as_text=True)
    assert 'class="pick"' in h
    assert h.count('name="trap"') == 7, "the hand traps are not selectable"
    assert 'name="lines"' in h, "no option to rank candidate lines"
    assert "not your pick" in h, "the filled-slot caveat is missing"


def test_pilot_report_template_renders_the_fixture(loggedin, monkeypatch):
    """Every other test here would pass against a template Jinja cannot
    compile. This one renders the real report shape."""
    import json
    from pathlib import Path
    from web import views
    rep = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "pilot_report.json").read_text())
    app = loggedin.application
    with app.test_request_context():
        from flask import render_template
        h = render_template("pilot_report.html", r=rep, name="my-despia",
                            stored=True, running=None)
    assert "Branded Opening" in h
    assert "no recovery found" in h.lower()
    assert "not</strong> a proof" in h, "the sampling caveat is missing"
    assert "SURVIVAL measures completion" in h, "the Maxx C blind spot vanished"
    assert "Per-window detail unavailable" in h, "a parse error was swallowed"
    assert "no legal window" in h
    assert "What the board does" in h, "the board reading is missing"
    assert "Rindbrumm the Striking Dragon" in h
    assert "can still act on their turn" in h
    assert "Playing around the most" in h, "the candidate ranking is missing"
    assert "fewest openings" in h
    assert "you still reach" in h, "the recovered board is missing"
    assert "searched" in h, "the line lost its detail"


def test_pilot_refuses_a_hand_the_deck_cannot_hold(loggedin):
    from skills import db
    from web import jobs
    c = db.connect()
    before = len(jobs.recent(c, 200))
    # Albion the Shrouded Dragon is a 1-of in my-despia.
    one_of = c.execute("SELECT id FROM cards WHERE name = ?",
                       ("Albion the Shrouded Dragon",)).fetchone()["id"]
    r = loggedin.post("/pilot/my-despia", data={"card": [str(one_of)] * 2})
    assert r.status_code == 302
    assert len(jobs.recent(c, 200)) == before, "an impossible hand was queued"


def test_pilot_refuses_six_cards_and_an_empty_hand(loggedin):
    from skills import db
    from web import jobs
    c = db.connect()
    before = len(jobs.recent(c, 200))
    loggedin.post("/pilot/my-despia", data={"card": []})
    loggedin.post("/pilot/my-despia", data={"card": ["1984618"] * 6})
    loggedin.post("/pilot/my-despia", data={"card": ["999999999"]})
    assert len(jobs.recent(c, 200)) == before


def test_dealing_puts_a_hand_in_the_picker_not_in_the_queue(loggedin):
    """You see the five cards and can re-deal before spending five
    minutes of solver on them."""
    import json
    import re
    from skills import db
    from web import jobs
    c = db.connect()
    before = len(jobs.recent(c, 200))
    h = loggedin.get("/pilot/my-despia?deal=1").get_data(as_text=True)
    preset = json.loads(re.search(r"const hand = (\[.*?\])\.map", h).group(1))
    assert len(preset) == 5
    assert len(jobs.recent(c, 200)) == before, "dealing queued a job"
    assert "repeat this deal" in h


def test_a_dealt_hand_is_repeatable_from_its_link(loggedin):
    import json
    import re

    def deal(q):
        h = loggedin.get(f"/pilot/my-despia?deal={q}").get_data(as_text=True)
        return json.loads(re.search(r"const hand = (\[.*?\])\.map", h).group(1))
    assert deal(42) == deal(42)
    assert deal(42) != deal(43)


def test_a_stored_report_opens_from_its_own_url(loggedin, tmp_path, monkeypatch):
    """The "already measured" list linked back to the picker, so clicking
    a finished report just reloaded the form that produced it."""
    import json
    import re
    from skills import handlab
    monkeypatch.setattr(handlab, "REPORTS", tmp_path)
    rep = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "pilot_report.json").read_text())
    folder = tmp_path / "my-despia"
    folder.mkdir()
    (folder / "abcdef0123456789.json").write_text(json.dumps(rep))

    page = loggedin.get("/pilot/my-despia").get_data(as_text=True)
    links = re.findall(r"/pilot/my-despia/r/([0-9a-f]+)", page)
    assert links == ["abcdef0123456789"], "the listing does not link the report"

    r = loggedin.get(f"/pilot/my-despia/r/{links[0]}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "The line" in body and "Where it breaks" in body


def test_a_report_id_cannot_be_a_path(loggedin, tmp_path, monkeypatch):
    from skills import handlab
    monkeypatch.setattr(handlab, "REPORTS", tmp_path)
    for bad in ("../../../etc/passwd", "..", "zz", "a" * 64, ""):
        r = loggedin.get(f"/pilot/my-despia/r/{bad}")
        assert r.status_code in (302, 404), f"{bad!r} -> {r.status_code}"


def test_a_missing_report_says_so_rather_than_erroring(loggedin, tmp_path,
                                                       monkeypatch):
    from skills import handlab
    monkeypatch.setattr(handlab, "REPORTS", tmp_path)
    r = loggedin.get("/pilot/my-despia/r/0123456789abcdef",
                     follow_redirects=True)
    assert r.status_code == 200
    assert "no longer stored" in r.get_data(as_text=True)


def test_a_step_with_a_threat_can_be_opened(loggedin):
    """The line and the hand-trap list were the same story told twice.
    A step now carries the windows that land on IT."""
    import json
    from pathlib import Path
    rep = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "pilot_report.json").read_text())
    app = loggedin.application
    with app.test_request_context():
        from flask import render_template
        h = render_template("pilot_report.html", r=rep, name="my-despia",
                            stored=True, running=None)
    expected = sum(1 for st in rep["steps"] if st["threats"])
    assert expected >= 3
    assert h.count('<details class="branch">') == expected, (
        "every step with a window should open, and only those")
    assert "Ash Blossom &amp; Joyous Spring" in h
    assert "on this card" in h, "a named window lost its framing"
    assert "after summon" in h, "an empty-chain window lost its summon count"
    assert "summon #1" in h, "the line does not number its summons"
    assert "never assembled" in h
    # A step with no window must not pretend to be safe OR be clickable.
    assert "The Fallen &amp; The Virtuous" in h


def test_an_old_report_is_joined_on_read_and_says_what_is_missing(
        loggedin, tmp_path, monkeypatch):
    """Reports written before the line carried its threats render as a
    plain list with nothing to click. They are joined by card name on
    read; their empty-chain windows cannot be placed without summon
    ordinals, and the page has to say that rather than show a bare
    count."""
    import json
    from skills import handlab
    monkeypatch.setattr(handlab, "REPORTS", tmp_path)
    rep = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "pilot_report.json").read_text())
    for st in rep["steps"]:          # back to the old shape
        st.pop("threats", None)
        st.pop("summon_index", None)
    rep.pop("unplaced", None)
    folder = tmp_path / "my-despia"
    folder.mkdir()
    (folder / "0011223344556677.json").write_text(json.dumps(rep))

    loaded = handlab.load(folder / "0011223344556677.json")
    assert loaded["legacy"] is True
    assert any(s["threats"] for s in loaded["steps"]), "nothing was joined"

    h = loggedin.get("/pilot/my-despia/r/0011223344556677").get_data(as_text=True)
    assert '<details class="branch">' in h
    if loaded["unplaced"]:
        assert "before the line numbered its summons" in flat(h)


def test_a_report_that_contradicts_itself_says_so(loggedin, tmp_path,
                                                  monkeypatch):
    """Runs measured before the verdict parser was fixed stored every
    window as a failure, including the ones their own totals say
    converted. Rendering that as-is shows "3 of 3 converted" above three
    dead ends."""
    import json
    from skills import handlab
    monkeypatch.setattr(handlab, "REPORTS", tmp_path)
    rep = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "pilot_report.json").read_text())
    for h in rep["handtraps"]:
        if h["name"] == "Effect Veiler":
            h["converted"] = 3
            for d in h["detail"]:
                d["survived"] = False      # the bug's output
    folder = tmp_path / "my-despia"
    folder.mkdir()
    (folder / "aabbccddeeff0011.json").write_text(json.dumps(rep))

    loaded = handlab.load(folder / "aabbccddeeff0011.json")
    veiler = next(h for h in loaded["handtraps"] if h["name"] == "Effect Veiler")
    assert veiler["detail_stale"] is True
    ash = next(h for h in loaded["handtraps"]
               if h["name"].startswith("Ash"))
    assert ash["detail_stale"] is False, "a sound verdict was called stale"

    h = loggedin.get("/pilot/my-despia/r/aabbccddeeff0011").get_data(as_text=True)
    assert "before a parser bug was fixed" in flat(h)
    assert "verdict not stored" in h, "a stale window still shows a verdict"


def test_partial_recovery_is_shown_as_partial(loggedin):
    """"No recovery found" was answering "can you rebuild the IDENTICAL
    board", so a line keeping most of its board read like one keeping
    none. Measured: Ash went from 0 of 3 to 2 of 4, both partial."""
    import json
    from pathlib import Path
    rep = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "pilot_report.json").read_text())
    app = loggedin.application
    with app.test_request_context():
        from flask import render_template
        h = render_template("pilot_report.html", r=rep, name="my-despia",
                            stored=True, running=None)
    assert "Mirrorjade the Iceblade Dragon" in h
    assert "without Mirrorjade" in flat(h), "the partial split is not shown"
    assert "rebuild it without" in flat(h), "the caveat is missing"
    # A trap with no partials must not grow a misleading badge.
    assert "whole board" in flat(h)


def test_every_read_only_route_answers(client):
    """A smoke walk over every GET route.

    The suite asserted specific copy on a handful of pages and never
    checked that the rest answer at all. The web UI is what the user
    opens, and it imports `skills/` that changes underneath it -- a
    route raising 500 would have been invisible here. Walked manually
    first: 15 routes 200, two missing-deck routes 302, no 5xx.
    """
    from skills import db
    _logged_in(client)
    name = db.get_meta(db.connect(), "active_deck") or "blue-eyes-branded-test"

    urls = ["/", "/report", "/matrix", "/threats", "/drill", "/jobs",
            "/build", "/maintenance", "/api/cards?q=blue",
            f"/report/{name}", f"/drill/{name}", f"/deck/{name}",
            f"/deck/{name}/ydk", f"/build/{name}", f"/pilot/{name}"]

    bad = []
    for url in urls:
        code = client.get(url).status_code
        if code >= 500 or code >= 400:
            bad.append((url, code))
    assert not bad, f"routes did not answer: {bad}"


def test_a_missing_deck_redirects_rather_than_erroring(client):
    """It must not 500, and it must not render a page about nothing."""
    _logged_in(client)
    for url in ("/report/does-not-exist", "/deck/does-not-exist"):
        code = client.get(url).status_code
        assert code < 500, f"{url} returned {code}"
        assert code in (302, 303, 404), f"{url} returned {code}"


def test_build_page_takes_its_section_caps_from_deck_py(client):
    """build.html must not hardcode the deck size limits.

    It had `const CAP = {main:[40,60], extra:[0,15], side:[0,15]}` --
    display only (legality is /api/validate, "from the server and only
    from the server", and that holds), but a second copy of the numbers
    in skills/deck.py. The values matched, so nothing was wrong; the risk
    is drift, where the browser keeps colouring 60 green while the server
    rejects it. Found by sweeping for duplicated computations after the
    ban-list map turned up twice (ISSUES #40, #53).
    """
    import json
    import re

    from skills import deck as deck_mod

    _logged_in(client)
    body = client.get("/build?name=kewl-tune", follow_redirects=True).get_data(as_text=True)
    m = re.search(r"const CAP = (\{.*?\});", body)
    assert m, "build.html no longer renders a CAP object"
    caps = json.loads(m.group(1))
    assert caps == {
        "main": [deck_mod.MAIN_MIN, deck_mod.MAIN_MAX],
        "extra": [0, deck_mod.EXTRA_MAX],
        "side": [0, deck_mod.SIDE_MAX],
    }, f"the page's caps must come from skills/deck.py, got {caps}"
    # And the literals must be gone from the template source.
    import pathlib
    tpl = (pathlib.Path(__file__).resolve().parent.parent
           / "web" / "templates" / "build.html").read_text()
    assert "main:[40,60]" not in tpl


# Each entry: (path, what it guards, regex that must appear).
#
# The route walk above proves a page answers 200. It does not prove the
# page says the right thing, and every correction this review made to a
# user-facing number lives in a template where a later edit could drop it
# without failing anything. These pin the corrections themselves.
CORRECTED_COPY = [
    # ISSUES #38: survival is per HAND with a window, not per window.
    ("/matrix", "survival denominator is hands",
     r"hands</strong> where the"),
    ("/matrix", "the all-windows standard is named",
     r"every</strong> window"),
    # ISSUES #49: the honest per-hand survival, with its live count. Pinned
    # to a deck WITH a measured matrix: bare /report follows the active
    # deck, and these failed whenever that deck had no matrix rows.
    ("/report/kewl-tune", "plays-through is of LIVE hands", r"live</strong>"),
    ("/report/kewl-tune", "never-live is unmeasured, not 0",
     r"unmeasured|never had a legal window"),
    # ISSUES #50/#52: which board library the summon figures came from.
    ("/threats", "source library is stated", r"Derived from"),
    ("/threats", "single-draw understatement noted", r"understate"),
    # ISSUES #54: section caps come from skills/deck.py.
    ("/build?name=kewl-tune", "caps are server-rendered JSON",
     r"const CAP = \{[^}]*\"main\": \[40, 60\]"),
]


@pytest.mark.parametrize(
    "path,what,pattern", CORRECTED_COPY,
    ids=[f"{p.split('?')[0]}-{w}" for p, w, _ in CORRECTED_COPY])
def test_corrected_numbers_still_say_what_they_mean(client, path, what, pattern):
    import re
    _logged_in(client)
    body = client.get(path, follow_redirects=True).get_data(as_text=True)
    assert re.search(pattern, body), (
        f"{path} no longer states: {what}. A number shown without what it "
        "is over is the defect this review spent its time on")


# Every page a person navigates to, excluding fragments and downloads.
CHROME_PAGES = [
    "/", "/deck/kewl-tune", "/report", "/matrix", "/threats", "/drill",
    "/build?name=kewl-tune", "/import", "/jobs", "/maintenance",
]


@pytest.mark.parametrize("path", CHROME_PAGES)
def test_every_page_has_the_same_header_pattern(client, path):
    """One page-head per page, not a hand-rolled h1 plus a muted <p>.

    Before 2026-10-25 each page invented its own heading: an <h1>, a
    muted paragraph, sometimes a `prov` line, sometimes action buttons
    mixed into a sentence. Nothing looked like anything else, which is
    most of what "cluttered" meant.
    """
    _logged_in(client)
    body = client.get(path, follow_redirects=True).get_data(as_text=True)
    assert 'class="page-head"' in body, f"{path} has no page-head block"
    assert body.count('class="page-head"') == 1, \
        f"{path} has more than one page header"


@pytest.mark.parametrize("path", CHROME_PAGES)
def test_chrome_names_the_active_deck_and_the_current_page(client, path):
    """The header must say which deck, and which page you are on.

    `report`, `matrix`, `threats` and `drill` all operate on the ACTIVE
    deck when no name is given, and the chrome never said which one that
    was — a page of numbers about a deck that did not name the deck.
    """
    import re
    _logged_in(client)
    body = client.get(path, follow_redirects=True).get_data(as_text=True)
    # Three labelled nav groups, so a link's purpose is visible.
    for group in ("decks", "analyse", "system"):
        assert f'class="lbl">{group}<' in body, \
            f"{path} is missing the '{group}' nav group"
    # Exactly one nav link marked current.
    assert len(re.findall(r'class="on"', body)) == 1, \
        f"{path} does not mark exactly one nav link as current"


def test_metric_captions_are_disclosures_not_walls_of_text(client):
    """The long captions fold, and are still present.

    Every one of them exists because a number was once read as something
    else (ISSUES #38, #49, #52), so none may be deleted to tidy the page
    — they move into a <details> whose summary stays visible. This checks
    both halves: the disclosure exists, and the wording survives inside
    it (the parametrised copy guards above assert the wording itself).
    """
    _logged_in(client)
    for path, least in (("/report", 3), ("/matrix", 2), ("/threats", 2)):
        body = client.get(path, follow_redirects=True).get_data(as_text=True)
        assert body.count('details class="note"') >= least, (
            f"{path} should fold at least {least} metric captions")
        assert "<summary>" in body


def test_every_job_button_on_the_deck_page_is_actually_startable(client):
    """A button that renders and then refuses is worse than no button.

    The deck page built its list as `KINDS.items() if k != "meta"` — the
    literal string, not `jobs.DECKLESS`, which is `("meta", "images")`.
    So it offered "cache card art", and POSTing it to the per-deck route
    answered **"Unknown job type"**. Clicking a button and being told the
    job does not exist reads as the whole feature being broken, which is
    how this was reported.

    This walks every button the page actually renders and asserts the
    route accepts it, so the two lists cannot drift apart again.
    """
    import re

    _logged_in(client)
    deck = "kewl-tune"
    body = client.get(f"/deck/{deck}", follow_redirects=True).get_data(as_text=True)
    kinds = re.findall(rf'/deck/{deck}/run/([a-z]+)"', body)
    assert kinds, "the deck page renders no job buttons at all"

    from skills import db
    from web import jobs

    conn = db.connect()
    for kind in kinds:
        assert kind not in jobs.DECKLESS, (
            f"the deck page offers {kind!r}, which is deckless and will be "
            "refused by the per-deck route")
        r = client.post(f"/deck/{deck}/run/{kind}", follow_redirects=True)
        page = r.get_data(as_text=True)
        assert "Unknown job type" not in page, \
            f"the deck page offers {kind!r} but the route refuses it"
        # Started it, so put it back -- a test must not leave hours of
        # solver queued on the machine it runs on.
        row = conn.execute(
            "SELECT id FROM jobs WHERE kind=? ORDER BY id DESC LIMIT 1",
            (kind,)).fetchone()
        if row:
            jobs.cancel(conn, row["id"])


def test_deckless_jobs_live_on_the_setup_page(client):
    """...and the deckless ones must still be reachable somewhere."""
    import re
    from web import jobs

    _logged_in(client)
    body = client.get("/maintenance", follow_redirects=True).get_data(as_text=True)
    offered = set(re.findall(r'action="/run/([a-z]+)"', body))
    for kind in jobs.DECKLESS:
        assert kind in offered, \
            f"{kind} is deckless and has no button on the setup page either"


def test_watch_drill_plays_a_stored_line_and_refuses_paths(loggedin):
    """The playmat for a drill line: served from a results file named
    bare, never a path, and every frame drawn from the replay."""
    from skills import drill
    root = Path(__file__).resolve().parent.parent
    src = next((p for p in sorted((root / "results").glob("*.jsonl"))
                if drill.lines_from(p, "kewl-tune")), None)
    if src is None:
        pytest.skip("no stored kewl-tune lines")
    r = loggedin.get(f"/watch/drill/kewl-tune?src={src.name}&i=0")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "const DATA" in html and '"frames"' in html
    for bad in ("../CLAUDE.md", "/etc/passwd", "x.jsonl/../y.jsonl"):
        assert loggedin.get(f"/watch/drill/kewl-tune?src={bad}&i=0").status_code == 404
    assert loggedin.get(f"/watch/drill/kewl-tune?src={src.name}&i=9999").status_code == 404
    assert loggedin.get(f"/watch/drill/no-such-deck?src={src.name}&i=0").status_code == 404


def test_watch_pilot_without_a_stored_line_says_why(loggedin, tmp_path, monkeypatch):
    """A report from before lines were stored cannot be watched. It must
    say so, not 500 and not silently show nothing."""
    from skills import handlab
    rep = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "pilot_report.json").read_text())
    rep.pop("replay", None)
    (tmp_path / "kewl-tune").mkdir()
    (tmp_path / "kewl-tune" / "0123456789abcdef.json").write_text(json.dumps(rep))
    monkeypatch.setattr(handlab, "REPORTS", tmp_path)
    r = loggedin.get("/watch/pilot/kewl-tune/0123456789abcdef")
    assert r.status_code == 302 and "/pilot/kewl-tune/r/0123456789abcdef" in r.headers["Location"]

    # With the line stored, the same report plays.
    rec = json.loads((Path(__file__).resolve().parent / "fixtures"
                      / "solved_line.json").read_text())
    rep["replay"] = handlab.replay_of(rec)
    (tmp_path / "kewl-tune" / "0123456789abcdef.json").write_text(json.dumps(rep))
    r = loggedin.get("/watch/pilot/kewl-tune/0123456789abcdef")
    assert r.status_code == 200 and "const DATA" in r.get_data(as_text=True)
    assert loggedin.get("/watch/pilot/kewl-tune/0123456789abcdef?line=nope").status_code == 404


def test_train_page_start_and_stop(loggedin, monkeypatch):
    """The trainer page renders, starts a job, and stops without killing.

    `jobs.enqueue` is stubbed: these tests run against the real database,
    and the live worker would claim a real train job and start solving.
    """
    from skills import trainer
    from web import jobs as J

    assert loggedin.get("/train/kewl-tune").status_code == 200
    assert loggedin.get("/train/no-such-deck").status_code == 302

    seen = {}
    monkeypatch.setattr(J, "enqueue", lambda c, kind, deck=None, **p:
                        seen.update(kind=kind, deck=deck, **p) or 999999)
    monkeypatch.setattr("web.views._training_job", lambda c, name: None)
    r = loggedin.post("/train/kewl-tune/start", data={"hours": "500", "hands": "2"})
    assert r.status_code == 302
    # clamped, never trusted: 72 h at most, a pool of at least 4
    assert seen == {"kind": "train", "deck": "kewl-tune", "hours": 72, "hands": 4,
                    "seconds": 180}

    try:
        assert loggedin.post("/train/kewl-tune/stop").status_code == 302
        assert trainer.stop_requested("kewl-tune")
    finally:
        trainer.clear_stop("kewl-tune")
    # A job kind with its own page never gets the generic run button.
    assert loggedin.post("/deck/kewl-tune/run/train").status_code == 302
    assert "train" in J.OWN_PAGE and "pilot" in J.OWN_PAGE


def test_watch_train_without_a_best_line_is_404(loggedin):
    assert loggedin.get("/watch/train/kewl-tune/9999").status_code == 404


def test_train_pref_avoid_and_count(loggedin):
    """The playmat's ⊘ (fetch, JSON) and the train page's forms (redirect)
    both land in trainer_prefs; anything malformed is refused."""
    from skills import db, trainer

    c = db.connect()
    try:
        r = loggedin.post("/train/kewl-tune/pref", headers={"X-Requested-With": "fetch"},
                          data={"kind": "avoid_activate", "code": "77103950", "zone": "sz"})
        assert r.status_code == 200 and r.get_json() == {"on": True}
        assert ("activate", 77103950, "sz") in trainer.prefs(c, "kewl-tune")["avoid"]
        r = loggedin.post("/train/kewl-tune/pref", data={"kind": "interrupter", "code": "28226490"})
        assert r.status_code == 302
        assert 28226490 in trainer.prefs(c, "kewl-tune")["interrupters"]
        page = loggedin.get("/train/kewl-tune").get_data(as_text=True)
        assert "Moves to avoid" in page and "spell &amp; trap zone" in page
        for bad in ({"kind": "drop table", "code": "1"},
                    {"kind": "avoid_activate", "code": "1"},              # no zone
                    {"kind": "avoid_activate", "code": "1", "zone": "../x"},
                    {"kind": "avoid_summon", "code": "1", "zone": "hand"},
                    {"kind": "interrupter", "code": "abc"}):
            assert loggedin.post("/train/kewl-tune/pref", data=bad).status_code == 400, bad
        assert loggedin.post("/train/no-such-deck/pref",
                             data={"kind": "interrupter", "code": "1"}).status_code == 404
        r = loggedin.post("/train/kewl-tune/pref", headers={"X-Requested-With": "fetch"},
                          data={"kind": "avoid_activate", "code": "77103950", "zone": "sz",
                                "on": "0"})
        assert r.get_json() == {"on": False}
        assert not trainer.prefs(c, "kewl-tune")["avoid"]
    finally:
        with c:
            c.execute("DELETE FROM trainer_prefs WHERE deck='kewl-tune'")


@pytest.mark.parametrize("route", ["/games/nosuchdeck/start", "/train/nosuchdeck/start"])
def test_start_routes_refuse_a_missing_deck(client, route):
    """deck_path returns a Path for any safe name, and a Path is truthy:
    `if not deck_path(name)` let a job be queued for a deck that does not
    exist (the worker then ran it and marked it done)."""
    from skills import db
    client.post("/login", data={"password": "pw"})
    before = db.connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    r = client.post(route, data={"enemy": "snake_eye"})
    assert r.status_code == 404
    assert db.connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before


def test_watch_current_best_opens_the_top_ranked_line(loggedin):
    """The train page's "watch current best" resolves at click time to the
    hand ranked first by the trainer's own key (the table's top row)."""
    from skills import db, trainer
    from web.views import deck_path

    c = db.connect()
    best = trainer.best_hand(c, trainer.deck_hash(deck_path("kewl-tune")))
    r = loggedin.get("/watch/train/kewl-tune/best")
    if best is None:
        assert r.status_code == 302 and r.headers["Location"].endswith("/train/kewl-tune")
    else:
        assert r.status_code == 302
        assert r.headers["Location"].endswith(f"/watch/train/kewl-tune/{best['i']}")
        assert "Watch current best" in loggedin.get("/train/kewl-tune").get_data(as_text=True)
    assert loggedin.get("/watch/train/no-such-deck/best").status_code == 404
    rows = [{"key": None}, {"key": trainer.parts([0, 2, 3, 3, -50])},
            {"key": trainer.parts([0, 3, 2, 2, -90])}, {"key": trainer.parts([0, 3, 2, 2, -80])}]
    assert sorted(rows, key=trainer.rank) == [rows[3], rows[2], rows[1], rows[0]]


def test_games_page_shows_card_impact(loggedin):
    page = loggedin.get("/games/imported")
    if page.status_code != 200 or "vs " not in page.get_data(as_text=True):
        return      # no games stored for this deck on this machine
    assert "Card impact" in page.get_data(as_text=True)


def test_watch_latest_game_opens_the_newest_one(loggedin):
    """"Watch latest game" resolves at click time to the newest watchable game."""
    from skills import db, impact, trainer
    from web.views import deck_path

    assert loggedin.get("/watch/game/no-such-deck/angelechy/latest").status_code == 404
    path = deck_path("imported")
    if not path or not path.exists():
        return
    c = db.connect()
    g = next((g for g in c.execute(
        "SELECT * FROM game_results WHERE deck_hash=? AND enemy='angelechy' AND "
        "outcome != 'no_t1' ORDER BY created DESC, rowid DESC LIMIT 20",
        (trainer.deck_hash(path),)) if impact.source("imported", "angelechy", g)), None)
    r = loggedin.get("/watch/game/imported/angelechy/latest")
    assert r.status_code == 302
    if g is None:
        assert r.headers["Location"].endswith("/games/imported")
    else:
        assert r.headers["Location"].endswith(
            f"/watch/game/imported/angelechy/{g['hand_index']}/{g['enemy_seed']}/{g['run']}")
        assert "Watch latest game" in loggedin.get("/games/imported").get_data(as_text=True)


def test_train_card_value_reweights_the_board(loggedin):
    """A card's value scales its interruptions in the trainer's key, and
    the train page sets it (1 / blank clears it)."""
    from skills import db, trainer

    c = db.connect()
    p = trainer.prefs(c, "kewl-tune")
    opts = trainer.value_options(c, __import__("web.views", fromlist=["x"]).deck_path("kewl-tune"), p)
    if not opts:
        return
    code = opts[0]["code"]
    try:
        assert loggedin.post("/train/kewl-tune/value", data={"code": code, "value": "2.5"}).status_code == 302
        assert trainer.prefs(c, "kewl-tune")["values"][code] == 2.5
        assert loggedin.post("/train/kewl-tune/value", data={"code": code, "value": "9"}).status_code == 400
        assert "Card values" in loggedin.get("/train/kewl-tune").get_data(as_text=True)
    finally:
        loggedin.post("/train/kewl-tune/value", data={"code": code, "value": ""})
    assert code not in trainer.prefs(c, "kewl-tune")["values"]
