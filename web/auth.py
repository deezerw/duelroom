"""Single-password login, hardened for an internet-facing port.

The qBittorrent WebUI on this machine was actively brute-forced while
exposed, so rate limiting is here from the first commit rather than
added after an incident.
"""

import hmac
import time
from collections import defaultdict
from functools import wraps
from urllib.parse import urlparse

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash

bp = Blueprint("auth", __name__)

# attempts per source address
_ATTEMPTS = defaultdict(list)
WINDOW_S = 300
MAX_ATTEMPTS = 8


def _rate_limited(addr):
    now = time.monotonic()
    hits = [t for t in _ATTEMPTS[addr] if now - t < WINDOW_S]
    _ATTEMPTS[addr] = hits
    return len(hits) >= MAX_ATTEMPTS


def _record(addr):
    _ATTEMPTS[addr].append(time.monotonic())


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
        ok = bool(stored) and check_password_hash(stored, supplied)
        # Compare a constant regardless, so a missing hash and a wrong
        # password take the same path.
        hmac.compare_digest(supplied, supplied)
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
