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
from web.auth import safe_next  # noqa: E402


@pytest.fixture
def client():
    app = create_app({"PASSWORD_HASH": generate_password_hash("pw"),
                      "TESTING": True})
    return app.test_client()


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
