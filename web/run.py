#!/usr/bin/env python
"""Development launcher. Production uses gunicorn; see web/PLAN.md."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from web import create_app  # noqa: E402

if __name__ == "__main__":
    app = create_app()
    if not app.config["PASSWORD_HASH"]:
        print("No password set. Generate one:\n"
              "  venv/bin/python -c \"from werkzeug.security import "
              "generate_password_hash as g; print(g(input('password: ')))\"\n"
              "then export DUELROOM_PASSWORD_HASH='...'", file=sys.stderr)
        sys.exit(1)
    # Localhost by default: binding 0.0.0.0 is a deployment decision,
    # not a development default.
    app.run(host=os.environ.get("DUELROOM_HOST", "127.0.0.1"),
            port=int(os.environ.get("DUELROOM_PORT", "8446")), debug=False)
