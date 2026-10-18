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
