"""Single-password login, hardened for an internet-facing port.

The qBittorrent WebUI on this machine was actively brute-forced while
exposed, so rate limiting is here from the first commit rather than
added after an incident.
"""

import time
from functools import wraps
from urllib.parse import urlparse

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint("auth", __name__)

WINDOW_S = 300
MAX_ATTEMPTS = 8

# Same KDF and parameters werkzeug's generate_password_hash defaults to,
# so verifying against it costs what a real check costs.
_DUMMY_HASH = generate_password_hash("duelroom-no-password-configured")


def _conn():
    from skills import db
    return db.connect()


def _rate_limited(addr):
    """Shared across processes, deliberately.

    The first version kept attempts in a module-level dict. Under
    gunicorn that gives every worker its own allowance, so the real
    limit becomes 8 x workers and the limiter barely inconveniences a
    brute force. It also leaked between tests, which is how it was
    found.
    """
    c = _conn()
    cutoff = time.time() - WINDOW_S
    with c:
        c.execute("DELETE FROM login_attempts WHERE at < ?", (cutoff,))
    n = c.execute("SELECT COUNT(*) AS n FROM login_attempts WHERE addr = ?",
                  (addr,)).fetchone()["n"]
    return n >= MAX_ATTEMPTS


def _record(addr):
    c = _conn()
    with c:
        c.execute("INSERT INTO login_attempts (addr, at) VALUES (?,?)",
                  (addr, time.time()))


def clear_attempts(addr=None):
    """Test hook and a manual unlock if you lock yourself out."""
    c = _conn()
    with c:
        if addr:
            c.execute("DELETE FROM login_attempts WHERE addr = ?", (addr,))
        else:
            c.execute("DELETE FROM login_attempts")


def safe_next(target):
    """Only ever redirect within this site.

    An open redirect on the post-login `next` parameter was a real
    finding in both stockroom and flipper. Anything absolute, or with a
    host, is discarded rather than sanitised.
    """
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return None
    if target.startswith("//"):
        return None
    return target


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("in"):
            return redirect(url_for("auth.login", next=request.full_path))
        return fn(*a, **kw)
    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.args.get("next"))
    if request.method == "POST":
        addr = request.remote_addr or "?"
        if _rate_limited(addr):
            flash("Too many attempts. Wait five minutes.")
            return render_template("login.html", next=nxt), 429

        supplied = request.form.get("password", "")
        stored = current_app.config.get("PASSWORD_HASH") or ""
        # With no hash configured, returning immediately would answer
        # "is this install even set up?" in a few microseconds. Verify
        # against a throwaway hash instead, so both paths pay the same
        # KDF cost. (The earlier version called hmac.compare_digest on a
        # value with itself, which cost nothing and proved nothing.)
        ok = (check_password_hash(stored, supplied) if stored
              else (check_password_hash(_DUMMY_HASH, supplied) and False))
        if ok:
            session.clear()
            session["in"] = True
            session.permanent = True
            return redirect(safe_next(request.form.get("next")) or url_for("views.index"))
        _record(addr)
        flash("Wrong password.")
    return render_template("login.html", next=nxt)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
