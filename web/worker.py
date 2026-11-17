#!/usr/bin/env python
"""The single job worker.

Run exactly one of these. One process is what makes "one solver job at
a time per machine" structural: the engine takes 6 of 8 cores, and two
concurrent sweeps on one box make both slower and the machine unusable.
It runs one lane per device (skills/remote.py): the server's jobs one
after another, and beside them, one after another, the jobs sent to
another machine -- whose searches run there, not here.

    venv/bin/python web/worker.py

Stage 5 turns this into a systemd unit alongside gunicorn.
"""

import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skills import db, remote  # noqa: E402
from web import jobs  # noqa: E402

POLL_S = 3
_stop = threading.Event()


def _handle(signum, frame):
    _stop.set()
    print("worker: finishing the current job(s), then stopping", flush=True)


def lane(device):
    conn = db.connect()     # sqlite3 binds a connection to its thread
    while not _stop.is_set():
        job = jobs.claim(conn, device)
        if not job:
            _stop.wait(POLL_S)
            continue
        print(f"worker[{device}]: job {job['id']} {job['kind']} {job['deck'] or ''}",
              flush=True)
        state = jobs.run(conn, job)
        print(f"worker[{device}]: job {job['id']} -> {state}", flush=True)


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

    lanes = [threading.Thread(target=lane, args=(d,), name=d, daemon=True)
             for d in remote.DEVICES]
    for t in lanes:
        t.start()
    print(f"worker: ready ({', '.join(remote.DEVICES)})", flush=True)
    while any(t.is_alive() for t in lanes):
        for t in lanes:
            t.join(timeout=1)
    print("worker: stopped", flush=True)


if __name__ == "__main__":
    main()
