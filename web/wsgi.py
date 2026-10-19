"""gunicorn entry point: `gunicorn ... web.wsgi:app` from the repo root.

Kept separate from run.py so the development launcher's localhost default
and its "no password set" guard cannot be mistaken for production config.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web import create_app  # noqa: E402

app = create_app()

if not app.config["PASSWORD_HASH"]:
    # Refuse to start rather than serve an installation nobody can log
    # into -- or, worse, leave the shape of that failure to be discovered
    # from the outside.
    raise SystemExit(
        "DUELROOM_PASSWORD_HASH is not set. Run:\n"
        "  venv/bin/python web/manage.py set-password")

if not int(os.environ.get("DUELROOM_TLS", "0")):
    print("duelroom: DUELROOM_TLS=0 — the session cookie will NOT be marked "
          "Secure. Set it when serving over TLS.", file=sys.stderr)
