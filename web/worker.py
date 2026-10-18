#!/usr/bin/env python
"""The single job worker.

Run exactly one of these. One process is what makes "one solver job at
a time" structural: the engine takes 6 of 8 cores, and two concurrent
sweeps make both slower and the machine unusable.

    venv/bin/python web/worker.py

Stage 5 turns this into a systemd unit alongside gunicorn.
"""

import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skills import db  # noqa: E402
from web import jobs  # noqa: E402

POLL_S = 3
_stop = False


def _handle(signum, frame):
    global _stop
    _stop = True
    print("worker: finishing the current job, then stopping", flush=True)


def main():
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    conn = db.connect()

    # A job left 'running' by a crashed worker would block the queue
    # forever, since nothing else ever clears that state.
    with conn:
        n = conn.execute(
            "UPDATE jobs SET state='failed', error='worker restarted while "
            "this job was running', finished_at=datetime('now') "
            "WHERE state='running'").rowcount
    if n:
        print(f"worker: reset {n} orphaned running job(s)", flush=True)

    print("worker: ready", flush=True)
    while not _stop:
        job = jobs.claim(conn)
        if not job:
            time.sleep(POLL_S)
            continue
        print(f"worker: job {job['id']} {job['kind']} {job['deck'] or ''}",
              flush=True)
        state = jobs.run(conn, job)
        print(f"worker: job {job['id']} -> {state}", flush=True)
    print("worker: stopped", flush=True)


if __name__ == "__main__":
    main()
