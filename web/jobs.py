"""Queued solver work.

Solver runs take minutes to hours and occupy 6 of 8 cores, so a web
request cannot hold one and two must never run at once ON ONE MACHINE.
The web app only ENQUEUES; `web/worker.py` is a single process that
claims and runs jobs, one at a time per device (a `games` job can run its
searches on another machine, STAGE10.md), which makes the guarantee
structural rather than a convention someone has to remember.

The other thing this has to get right is partial state. The CLI shipped
a bug where a half-written sweep was read as complete and reported one
deck of seven as the whole meta. A UI makes that mistake prettier and
harder to notice, so `active()` exists for every view that shows derived
numbers to check before it renders them.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from skills import db, engine, remote

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "results" / "joblogs"

# kind -> (argv builder, human label, roughly how long)
KINDS = {
    "gate":    (lambda d, p: ["gate", d, str(p.get("hands", 12))],
                "validation gate", "~15 min"),
    "solve":   (lambda d, p: ["solve", d, str(p.get("hands", 8))],
                "turn-1 line search", "~10 min"),
    "ladder":  (lambda d, p: ["ladder", d, str(p.get("hands", 12))],
                "solver budget calibration", "~45 min"),
    "optimize": (lambda d, p: ["optimize", d, p.get("archetype", ""),
                               str(p.get("hands", 12)), str(p.get("rounds", 2))],
                 "flex-slot search", "hours"),
    "meta":    (lambda d, p: ["meta", str(p.get("days", 120)), str(p.get("cap", 500))],
                "scrape the meta", "~1 min"),
    "images":  (lambda d, p: ["images", p.get("scope", "small-all")],
                "cache card art", "~25 min"),
    "pilot":   (lambda d, p: ["pilot", d, p.get("cards", ""),
                              str(p.get("solve_s", 60)), str(p.get("fire_s", 30)),
                              p.get("traps", "all"), str(p.get("lines", 1))],
                "solve one chosen hand", "~5 min"),
    "train":   (lambda d, p: ["train", d, str(p.get("hours", 8)),
                              str(p.get("hands", 24))]
                + ([p["enemy"]] if p.get("enemy") else [])
                + (["--seconds", str(p["seconds"])] if p.get("seconds") else [])
                + (["--second"] if p.get("second") else []),
                "deck trainer", "hours, stoppable"),
    "starters": (lambda d, p: ["starters", d, str(p.get("size", 1)), str(p.get("hours", 8)),
                                "--seconds", str(p.get("seconds", 60)),
                                "--skip", ",".join(map(str, p.get("skip", []))) or "0"],
                 "starter test", "hours, stoppable"),
    "games":   (lambda d, p: ["games", d, p.get("enemy", ""),
                              str(p.get("runs", 10)), str(p.get("enemy_hands", 3))]
                + (["--second"] if p.get("second") else [])
                + (["--device", p["device"]] if p.get("device") else [])
                + (["--hands", ",".join(map(str, p["hands"]))] if p.get("hands") else []),
                "finish the game vs a meta deck", "hours, resumable"),
}

# Jobs that are about the installation rather than about one deck.
DECKLESS = ("meta", "images")

# Jobs started from their own page, because they need input the generic
# "run" button cannot give: a pilot run without chosen cards is a job
# that fails on its first line.
OWN_PAGE = ("pilot", "train", "games", "starters")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(conn, kind, deck=None, **params):
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, deck, params, queued_at) VALUES (?,?,?,?)",
            (kind, deck, json.dumps(params), _now()))
    return cur.lastrowid


def active(conn):
    """The running job, if any. Views must check this before trusting data."""
    return conn.execute(
        "SELECT * FROM jobs WHERE state = 'running' ORDER BY id LIMIT 1"
    ).fetchone()


def recent(conn, limit=25):
    return conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def cancel(conn, job_id):
    """Queued jobs are dropped; a running one is left alone.

    Killing a running solver mid-sweep leaves partial records that read
    as complete, which is the failure this module exists to prevent.
    The worker is the only thing allowed to end a running job.
    """
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET state='cancelled', finished_at=? "
            "WHERE id=? AND state='queued'", (_now(), job_id))
    return cur.rowcount > 0


# --- worker side ---------------------------------------------------------

# Jobs whose searches can run on another machine (skills/remote.py); the rest
# are the server's whatever their params say.
DEVICE_KINDS = ("games", "train", "starters", "pilot")


def device_of(job):
    """Where a job's searches run (skills/remote.py)."""
    if job["kind"] not in DEVICE_KINDS:
        return "server"
    return json.loads(job["params"] or "{}").get("device") or "server"


def claim(conn, device="server"):
    """Take the oldest queued job for `device`. One worker thread per
    device (web/worker.py), so no contention."""
    with conn:
        row = next((r for r in conn.execute(
            "SELECT * FROM jobs WHERE state='queued' ORDER BY id")
            if device_of(r) == device), None)
        if not row:
            return None
        conn.execute("UPDATE jobs SET state='running', started_at=? WHERE id=?",
                     (_now(), row["id"]))
    return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def run(conn, job):
    """Execute one claimed job to completion."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"job{job['id']}_{job['kind']}.log"
    build, _, _ = KINDS[job["kind"]]
    params = json.loads(job["params"] or "{}")
    argv = [a for a in build(job["deck"] or "", params) if a != ""]

    cmd = ["nice", "-n", "19", str(ROOT / "venv" / "bin" / "python"),
           str(ROOT / "daemon.py"), *argv]
    with conn:
        conn.execute("UPDATE jobs SET log_path=? WHERE id=?",
                     (str(log), job["id"]))

    try:
        with log.open("w") as fh:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                    cwd=str(ROOT), env={**engine.env(), remote.ENV_VAR: device_of(job)})
            with conn:
                conn.execute("UPDATE jobs SET pid=? WHERE id=?",
                             (proc.pid, job["id"]))
            code = proc.wait()
    except Exception as e:
        _finish(conn, job["id"], "failed", str(e))
        return "failed"

    if code == remote.EXIT_DOWN:
        # Its device is off or in Windows. Back in the queue: the lane waits
        # and retries, and the games already played are not played again.
        tail = "\n".join(log.read_text().splitlines()[-1:]) if log.exists() else ""
        with conn:
            conn.execute("UPDATE jobs SET state='queued', started_at=NULL, pid=NULL, "
                         "error=? WHERE id=?",
                         (f"waiting for {device_of(job)}: {tail[-300:]}", job["id"]))
        return "waiting"
    if code != 0:
        tail = "\n".join(log.read_text().splitlines()[-3:]) if log.exists() else ""
        _finish(conn, job["id"], "failed", f"exit {code}: {tail[-400:]}")
        return "failed"
    _finish(conn, job["id"], "done", None)
    return "done"


def _finish(conn, job_id, state, error):
    with conn:
        conn.execute(
            "UPDATE jobs SET state=?, error=?, finished_at=?, pid=NULL WHERE id=?",
            (state, error, _now(), job_id))


# --- progress ------------------------------------------------------------

def progress(job):
    """Best-effort progress from the job's own log.

    Deliberately reports what it can SEE rather than estimating: a
    fabricated percentage is the same failure as a fabricated metric.
    """
    path = job["log_path"]
    if not path or not Path(path).exists():
        return {"hands": 0, "line": "", "note": "starting"}
    lines = Path(path).read_text().splitlines()
    last = next((l for l in reversed(lines) if l.strip()), "")

    # Jobs that know their own denominator say so explicitly, rather than
    # having a percentage inferred for them.
    counted = [l for l in lines if l.startswith("progress ")]
    if counted:
        try:
            done, total = counted[-1].split()[1].split("/")
            return {"hands": int(done), "total": int(total), "line": last[:160],
                    "note": f"{done} of {total}"}
        except (IndexError, ValueError):
            pass

    hands = sum(1 for l in lines if ": solved" in l or ": brick" in l
                or ": unsolved" in l or ": error" in l)
    return {"hands": hands, "line": last[:160],
            "note": f"{hands} hand(s) solved so far" if hands else "working"}


def pilot_report(job):
    """The structured report a finished pilot job wrote, if any."""
    if job["kind"] != "pilot":
        return None
    from skills import handlab
    params = json.loads(job["params"] or "{}")
    path = params.get("report")
    return handlab.load(path) if path else None


def partial(job):
    """Results available before the job finishes, where a ledger exists."""
    if job["kind"] != "optimize" or not job["deck"]:
        return []
    ledger = ROOT / "results" / f"optimize_{job['deck']}.jsonl"
    if not ledger.exists():
        return []
    out = []
    for line in ledger.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return sorted(out, key=lambda r: -r.get("score", 0))


# --- the queue, per device ------------------------------------------------

def queue(conn):
    """{device: {"running": job or None, "queued": [jobs in claim order]}}
    for every known device."""
    out = {d: {"running": None, "queued": []} for d in remote.DEVICES}
    for j in conn.execute("SELECT * FROM jobs WHERE state IN ('queued','running') "
                          "ORDER BY id"):
        q = out.setdefault(device_of(j), {"running": None, "queued": []})
        if j["state"] == "running":
            q["running"] = j
        else:
            q["queued"].append(j)
    return out


def games_estimate(conn, job, dh, hands):
    """What a games job has left, and roughly how long that is on its device.

    Only from measurements: the mean seconds of the games that device has
    already played on that side (turns 2-3), and turn 1 at its full budget
    (it rarely stops early). None for the time when there is nothing
    measured yet -- a guessed number would read as a measured one."""
    p = json.loads(job["params"] or "{}")
    dev = remote.get(device_of(job))
    first = 0 if p.get("second") else 1
    runs, ehands = int(p.get("runs", 10)), int(p.get("enemy_hands", 3))
    seeds = (1000, 1000 + ehands)
    key = (dh, p.get("enemy", ""), first, dev.name, *seeds)
    only = ""           # a job over some hands (`--hands`) counts those alone
    if p.get("hands"):
        hands = len(p["hands"])
        only = " AND hand_index IN (%s)" % ",".join(str(int(h)) for h in p["hands"])
    played = conn.execute(
        "SELECT COUNT(*) FROM game_results WHERE deck_hash=? AND enemy=? AND first=? "
        "AND device=? AND enemy_seed >= ? AND enemy_seed < ? AND run < ?" + only,
        (*key, runs)).fetchone()[0]
    t1_done = conn.execute(
        "SELECT COUNT(*) FROM game_turn1 WHERE deck_hash=? AND enemy=? AND first=? "
        "AND device=? AND enemy_seed >= ? AND enemy_seed < ?" + only, key).fetchone()[0]
    left = max(0, hands * ehands * runs - played)
    t1_left = max(0, hands * ehands - t1_done)
    m = conn.execute(
        "SELECT AVG(seconds), COUNT(*) FROM game_results WHERE device=? AND first=? "
        "AND outcome != 'no_t1' AND seconds IS NOT NULL", (dev.name, first)).fetchone()
    secs = None
    if m[1]:
        secs = (left * m[0] + t1_left * engine.SOLVE_MS / 1000) / dev.workers
    return {"games": hands * ehands * runs, "left": left, "t1_left": t1_left,
            "seconds": secs, "basis": m[1]}
