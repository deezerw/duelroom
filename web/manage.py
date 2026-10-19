#!/usr/bin/env python
"""Operational bits that are not pages.

    venv/bin/python web/manage.py set-password
    venv/bin/python web/manage.py unlock [address]
    venv/bin/python web/manage.py check
"""

import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from werkzeug.security import generate_password_hash  # noqa: E402

ENV = ROOT / "data" / "web.env"


def set_password():
    pw = getpass.getpass("new password: ")
    if len(pw) < 10:
        # This port faces the internet and the qBittorrent WebUI on this
        # machine was brute-forced while exposed.
        print("at least 10 characters, please", file=sys.stderr)
        return 1
    if pw != getpass.getpass("again: "):
        print("they do not match", file=sys.stderr)
        return 1

    lines = []
    if ENV.exists():
        lines = [l for l in ENV.read_text().splitlines()
                 if not l.startswith("DUELROOM_PASSWORD_HASH=")]
    lines.append(f"DUELROOM_PASSWORD_HASH='{generate_password_hash(pw)}'")
    ENV.parent.mkdir(parents=True, exist_ok=True)
    # Written before the chmod on a fresh file would be a window where the
    # hash is world-readable, so create it closed and then fill it.
    ENV.touch(mode=0o600, exist_ok=True)
    ENV.chmod(0o600)
    ENV.write_text("\n".join(lines) + "\n")
    print(f"written to {ENV} (mode 600)")
    return 0


def unlock(addr=None):
    from web.auth import clear_attempts
    clear_attempts(addr)
    print(f"cleared login attempts for {addr or 'every address'}")
    return 0


def check():
    """What a deploy needs, and whether it is there."""
    ok = True
    rows = [
        ("password hash", bool(os.environ.get("DUELROOM_PASSWORD_HASH"))
         or (ENV.exists() and "DUELROOM_PASSWORD_HASH=" in ENV.read_text())),
        ("env file mode 600", not ENV.exists()
         or (ENV.stat().st_mode & 0o077) == 0),
        ("secret key", (ROOT / "data" / "secret_key").exists()),
        ("secret key mode 600",
         not (ROOT / "data" / "secret_key").exists()
         or ((ROOT / "data" / "secret_key").stat().st_mode & 0o077) == 0),
        ("tls cert", (ROOT / "certs" / "fullchain.pem").exists()),
        ("tls key mode 600", not (ROOT / "certs" / "key.pem").exists()
         or ((ROOT / "certs" / "key.pem").stat().st_mode & 0o077) == 0),
        ("gunicorn", (ROOT / "venv" / "bin" / "gunicorn").exists()),
    ]
    for label, good in rows:
        print(f"  {'ok  ' if good else 'MISS'}  {label}")
        ok = ok and good
    return 0 if ok else 1


def main(argv):
    cmds = {"set-password": lambda a: set_password(),
            "unlock": lambda a: unlock(a[0] if a else None),
            "check": lambda a: check()}
    if not argv or argv[0] not in cmds:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return cmds[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
