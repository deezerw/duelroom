"""duelroom web UI.

Lives inside the duelroom repo rather than beside it so it imports
`skills.*` directly and reads the same SQLite database. Two copies of
the analysis code, or two databases, would drift — and the thing this
project is worst at is noticing when a number stopped meaning what it
says.

Design rule carried over from the CLI: uncertainty is drawn, not
footnoted. A solve rate renders as an interval, a flagged target greys
out the panel it poisons, and every measured number states the run,
hand count and solver budget it came from. If a number cannot carry its
caveat it does not get a tile.
"""

import os
import secrets
import sys
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=False)

    # A DISTINCT cookie name. stockroom and flipper both used the Flask
    # default "session" and logged people between the two apps; that was
    # a real finding in the stockroom security audit.
    app.config.update(
        SESSION_COOKIE_NAME="duelroom_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Set by the launcher when serving over TLS.
        SESSION_COOKIE_SECURE=bool(int(os.environ.get("DUELROOM_TLS", "0"))),
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,   # a .ydk is a few KB
        SECRET_KEY=_secret_key(),
        PASSWORD_HASH=os.environ.get("DUELROOM_PASSWORD_HASH", ""),
    )
    if config:
        app.config.update(config)

    from . import auth, views
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    return app


def _secret_key():
    """Stable across restarts, so a reload does not log you out.

    Generated once into a mode-600 file rather than hardcoded or
    regenerated per boot.
    """
    env = os.environ.get("DUELROOM_SECRET_KEY")
    if env:
        return env
    path = ROOT / "data" / "secret_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()
