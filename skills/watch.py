"""What is running right now, queued or not (the Jobs page's live panel).

Experiments (tools/experiments/*.py, tools/ml/*.py) run outside the job
queue, so the queue alone shows an idle machine while it works for hours.
This reads the process table instead -- nothing has to report in:

- scripts: the python processes running a tools/ script or a daemon.py
  command; where their stdout goes (their log), and their progress when
  the script defines `progress()` -> {done, total, parts: [(label, done,
  total)]}. Imported only by file path, and only when it is running.
- solvers: every combosolver, local or started over ssh on a remote
  device, with its budget (--solve-ms), how long it has run and the
  script it belongs to.

Time left comes only from what this panel has watched: the rate between
the first time it saw a script (by pid and start) and now. Until a few
tasks finish under watch it says so instead of guessing.
"""

import importlib.util
import json
import os
import re
import time
from pathlib import Path

from . import engine, remote

ROOT = engine.ROOT
RATES = ROOT / "data" / "watch_rates.json"
MIN_DONE = 3            # tasks finished under watch before a time is shown
_CLK = os.sysconf("SC_CLK_TCK")


def _cmdline(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]
    except OSError:
        return None


def _stat(pid):
    """(ppid, seconds running)."""
    try:
        s = Path(f"/proc/{pid}/stat").read_text()
        f = s[s.rindex(")") + 2:].split()
        up = float(Path("/proc/uptime").read_text().split()[0])
        return int(f[1]), up - int(f[19]) / _CLK
    except (OSError, ValueError, IndexError):
        return None, 0.0


def _env(pid, key):
    try:
        for kv in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
            if kv.startswith(key.encode() + b"="):
                return kv.split(b"=", 1)[1].decode()
    except OSError:
        pass
    return None


def _procs():
    out = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            argv = _cmdline(int(d))
            if argv:
                out[int(d)] = [a.decode(errors="replace") for a in argv]
    return out


def _script_of(argv):
    """The tools/ script or daemon command a python argv runs, else None."""
    if not argv or not Path(argv[0]).name.startswith("python"):
        return None
    for i, a in enumerate(argv[1:], 1):
        if a.endswith(".py") and ("tools/" in a or a.endswith("daemon.py")):
            path = (ROOT / a) if not a.startswith("/") else Path(a)
            args = argv[i + 1:]
            name = path.stem if path.stem != "daemon" else "daemon " + " ".join(args[:1])
            return {"name": name, "path": path, "args": args}
    return None


def _flag(argv, name):
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _device_of_ssh(argv):
    for a in argv:
        m = re.match(r"\w+@([\w.-]+)$", a)
        if m:
            for d in remote.DEVICES.values():
                if d.host == m.group(1):
                    return d.name
    return None


def _solver(argv):
    """(device, solver argv) when argv runs a combosolver, here or over ssh."""
    if Path(argv[0]).name == "combosolver":
        return "server", argv
    if Path(argv[0]).name == "ssh":
        cmd = " ".join(argv)
        if "combosolver" in cmd:
            return _device_of_ssh(argv) or "remote", cmd.split()
    return None


def _tag(argv):
    """Which task a solve is: the part of --outdir under scratch/ or games/."""
    out = _flag(argv, "--outdir") or ""
    m = (re.search(r"/scratch/(.+?)/(?:out-?\S*)$", out)
         or re.search(r"/games/[^/]+/[^/]+/(.+)/[^/]+$", out))
    return m.group(1) if m else Path(out).parent.name


def _log_of(pid):
    try:
        p = os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return None
    return Path(p) if p.startswith("/") and Path(p).is_file() else None


def _tail(path, n=8, scan=65536):
    with path.open("rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - scan))
        lines = fh.read().decode(errors="replace").splitlines()
    return lines[-n:], next((l for l in reversed(lines) if l.startswith("temps ")), None)


def server_temp():
    """This machine's CPU package temperature, as the remote heat watch logs it."""
    for h in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (h / "name").read_text().strip() not in ("coretemp", "k10temp"):
                continue
            for lab in h.glob("temp*_label"):
                if lab.read_text().strip() in ("Package id 0", "Tctl"):
                    return int((h / lab.name.replace("label", "input")).read_text()) // 1000
        except (OSError, ValueError):
            continue
    return None


_mods = {}


def _progress(path):
    """The script's own progress(), or None."""
    try:
        key = (path, path.stat().st_mtime)
        if key not in _mods:
            spec = importlib.util.spec_from_file_location(f"_watch_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _mods[key] = mod
        fn = getattr(_mods[key], "progress", None)
        return fn() if fn else None
    except Exception as exc:  # noqa: BLE001 -- a broken progress() must not break the page
        return {"error": f"{type(exc).__name__}: {exc}"}


def _left(key, done, total, now=None):
    """Seconds left from the rate seen since this panel first saw `key`."""
    now = now or time.time()
    try:
        seen = json.loads(RATES.read_text())
    except (OSError, ValueError):
        seen = {}
    if key not in seen or seen[key][1] > done:
        seen[key] = [now, done]
        seen = dict(list(seen.items())[-50:])
        try:
            RATES.write_text(json.dumps(seen))
        except OSError:
            pass
    t0, d0 = seen[key]
    if done - d0 < MIN_DONE or now <= t0:
        return None, done - d0
    return (total - done) * (now - t0) / (done - d0), done - d0


def snapshot():
    """{device: {"scripts": [...], "solvers": [...]}} for every device."""
    procs = _procs()
    parent = {pid: _stat(pid) for pid in procs}
    scripts = {}
    for pid, argv in procs.items():
        s = _script_of(argv)
        if not s:
            continue
        # a script started by another script (or by a shell wrapping it) is part of it
        pp = parent[pid][0]
        while pp in procs and not _script_of(procs[pp]):
            pp = parent.get(pp, (None,))[0]
        if pp in procs:
            continue
        s.update(pid=pid, running_s=parent[pid][1],
                 device=_env(pid, "DUELROOM_DEVICE") or "server",
                 log=_log_of(pid), solvers=[])
        scripts[pid] = s

    def owner(pid):
        while pid in procs:
            if pid in scripts:
                return pid
            pid = parent[pid][0]
        return None

    out = {name: {"scripts": [], "solvers": []} for name in remote.DEVICES}
    for pid, argv in procs.items():
        hit = _solver(argv)
        if not hit:
            continue
        dev, sargv = hit
        ms = _flag(sargv, "--solve-ms")
        sv = {"device": dev, "tag": _tag(sargv), "running_s": parent[pid][1],
              "budget_s": int(ms) / 1000 if ms and ms.isdigit() else None}
        sv["frac"] = min(1.0, sv["running_s"] / sv["budget_s"]) if sv["budget_s"] else None
        own = owner(pid)
        sv["script"] = scripts[own]["name"] if own else None
        out.setdefault(dev, {"scripts": [], "solvers": []})["solvers"].append(sv)
        if own:
            scripts[own]["solvers"].append(sv)

    for pid, s in scripts.items():
        s["tail"], s["temps"] = _tail(s["log"]) if s["log"] else ([], None)
        s["progress"] = _progress(s["path"])
        p = s["progress"]
        s["left_s"] = s["watched"] = None
        if p and not p.get("error") and p.get("total"):
            start = int(time.time() - s["running_s"])
            s["left_s"], s["watched"] = _left(f"{pid}:{start}", p["done"], p["total"])
        s["log"] = str(s["log"]) if s["log"] else None
        s["path"] = str(s["path"].relative_to(ROOT)) if s["path"].is_relative_to(ROOT) else str(s["path"])
        # where its solvers actually run (a games job on duelpc runs from the server)
        devs = {sv["device"] for sv in s["solvers"]} or {s["device"]}
        for d in devs:
            out.setdefault(d, {"scripts": [], "solvers": []})["scripts"].append(s)
    t = server_temp()
    if t is not None:
        out["server"]["temps"] = f"temps server: cpu {t} C"
    for d in out.values():
        d["solvers"].sort(key=lambda sv: -sv["running_s"])
        d["scripts"].sort(key=lambda s: -s["running_s"])
    return out
