"""Run the solver on another machine (STAGE10.md).

The app, its database and every decision stay on the server; another
device only ever runs `combosolver`. It works by PATH MIRRORING: the device
has the same user and the same absolute paths for everything the solver
reads, so a solver command line built here runs there unchanged.

    sync(dev)          copy the solver build, card DB and scripts over
    ensure_up(dev)     SSH check, else Wake-on-LAN and wait
    run(dev, cmd, ...) push the command's input files, run it, pull outdir

A job picks its device through the environment (`DUELROOM_DEVICE`, set by
the worker from the job's params), so one job process talks to one device
and nothing about the game logic changes with where the search runs.
"""

import hashlib
import os
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import engine

ENV_VAR = "DUELROOM_DEVICE"


@dataclass(frozen=True)
class Device:
    name: str
    host: str = ""          # empty: this machine
    mac: str = ""           # Wake-on-LAN
    workers: int = 4        # games at once (2 solver threads each)
    label: str = ""

    @property
    def remote(self):
        return bool(self.host)


SERVER = Device("server", workers=4, label="server (i7-9700K, 8 threads)")
DEVICES = {
    "server": SERVER,
    # duelpc.local resolves over mDNS (avahi on both ends), so a new DHCP
    # address does not break it. 16 threads: 8 games x 2 solver threads.
    "duelpc": Device("duelpc", host="duelpc.local", mac="10:ff:e0:86:34:42",
                     workers=8, label="gaming PC (Ryzen 7800X3D, 16 threads)"),
}

# What the solver reads besides its per-solve inputs. The merged tree is
# mostly symlinks into the upstream one, so both go, links kept as links.
_V = engine.VENDOR
STATIC = (
    _V / "build" / "combo-solver-rooted" / "bin",
    engine.ROOT / "vendor" / "third_party_merged",
    engine.UPSTREAM_THIRD_PARTY / "CardScripts",
    engine.UPSTREAM_THIRD_PARTY / "BabelCDB",
    engine.UPSTREAM_THIRD_PARTY / "LFLists",
)
STAMP = Path.home() / ".duelroom-solver-stamp"
WAKE_WAIT_S = 240
SSH_LOST = 255
# A job process exits with this when its device went away (EX_TEMPFAIL):
# the worker queues the job again instead of failing it, and games resume.
EXIT_DOWN = 75


class DeviceDown(RuntimeError):
    pass


def current():
    """The device this process runs its solves on."""
    name = os.environ.get(ENV_VAR) or "server"
    if name not in DEVICES:
        raise ValueError(f"unknown device {name!r} (known: {', '.join(DEVICES)})")
    return DEVICES[name]


def get(name):
    return DEVICES[name or "server"]


# --- SSH ------------------------------------------------------------------

def _ssh_opts():
    # One multiplexed connection per device: eight workers each running
    # rsync + ssh + rsync per solve would otherwise pay a handshake apiece.
    cm = Path.home() / ".ssh" / "cm-duelroom-%h"
    # accept-new: a LAN box's key is learnt on first use; a CHANGED key
    # (reinstall) still fails, loudly, in up().
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={cm}",
            "-o", "ControlPersist=10m", "-o", "LogLevel=ERROR"]


def _target(dev):
    return f"deezer@{dev.host}"


def ssh(dev, command, timeout=60, check=False):
    r = subprocess.run(["ssh", *_ssh_opts(), _target(dev), command],
                       capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{dev.name}: `{command}` exit {r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[-300:]}")
    return r


def _rsync(args, timeout=600):
    rsh = "ssh " + " ".join(shlex.quote(o) for o in _ssh_opts())
    # -O: -R recreates every parent (/home, ...) and may not set their times.
    r = subprocess.run(["rsync", "-a", "-O", "-e", rsh, *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise DeviceDown(f"rsync exit {r.returncode}: {r.stderr.strip()[-300:]}")
    return r


def up(dev):
    try:
        r = ssh(dev, "true", timeout=20)
    except subprocess.TimeoutExpired:
        return False
    if "Host key verification failed" in r.stderr or "IDENTIFICATION HAS CHANGED" in r.stderr:
        # Not asleep: reinstalled. Waking it again will not help.
        raise DeviceDown(f"{dev.name}: SSH host key changed (reinstalled?); "
                         f"run: ssh-keygen -R {dev.host}")
    return r.returncode == 0


def wake(dev):
    """Send the Wake-on-LAN magic packet (broadcast, ports 9 and 7)."""
    pkt = b"\xff" * 6 + bytes.fromhex(dev.mac.replace(":", "")) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            s.sendto(pkt, ("255.255.255.255", port))


def ensure_up(dev, log=print):
    """Reachable, waking it if needed. Raises DeviceDown."""
    if not dev.remote or up(dev):
        return
    if not dev.mac:
        raise DeviceDown(f"{dev.name} is not reachable and has no Wake-on-LAN MAC")
    log(f"{dev.name}: not answering; sending Wake-on-LAN")
    deadline = time.monotonic() + WAKE_WAIT_S
    while time.monotonic() < deadline:
        wake(dev)
        time.sleep(15)
        if up(dev):
            log(f"{dev.name}: awake")
            return
    raise DeviceDown(f"{dev.name} did not wake within {WAKE_WAIT_S} s "
                     "(off at the wall, booted into Windows, or no network)")


# --- the mirrored solver install ------------------------------------------

def stamp():
    """Fingerprint of everything in STATIC: path, size, mtime, link target."""
    h = hashlib.sha256()
    for top in STATIC:
        for root, dirs, files in os.walk(top):
            dirs.sort()
            for name in sorted(files + [d for d in dirs if os.path.islink(os.path.join(root, d))]):
                p = os.path.join(root, name)
                if os.path.islink(p):
                    h.update(f"{p}>{os.readlink(p)}\n".encode())
                else:
                    st = os.stat(p)
                    h.update(f"{p}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


def remote_stamp(dev):
    r = ssh(dev, f"cat {shlex.quote(str(STAMP))} 2>/dev/null")
    return r.stdout.strip()


def sync(dev, log=print):
    """Mirror STATIC onto the device and record the stamp there."""
    want = stamp()
    if remote_stamp(dev) == want:
        log(f"{dev.name}: solver install up to date ({want})")
        return want
    t = time.monotonic()
    _rsync(["-R", "--delete", *[str(p) for p in STATIC], f"{_target(dev)}:/"],
           timeout=1800)
    ssh(dev, f"echo {want} > {shlex.quote(str(STAMP))}", check=True)
    log(f"{dev.name}: solver install synced ({want}, {time.monotonic() - t:.0f} s)")
    return want


def prepare(dev, log=print):
    """Before a job: awake, same solver install, binary runs."""
    if not dev.remote:
        return
    ensure_up(dev, log)
    ssh(dev, f"mkdir -p {shlex.quote(str(engine.ROOT))}", check=True)
    sync(dev, log)
    binary = STATIC[0] / "combosolver"
    ssh(dev, f"{shlex.quote(str(binary))} --help >/dev/null", check=True)
    heat_of(dev, log)


# --- heat ---------------------------------------------------------------------
#
# Days of solving with nobody home: every minute the device's CPU (k10temp
# Tctl) and GPU temperatures go into the job log, and new solves wait while
# it runs hot -- running solves finish, so the load falls on its own. The
# 7800X3D holds itself at 89 C by design, so one reading there is normal;
# HOT_MINUTES of it in a row is a cooling problem (fan, dust, a hot room).

CPU_HOT, CPU_OK = 89, 80
GPU_HOT, GPU_OK = 85, 75
HOT_MINUTES = 3
HEAT_EVERY_S = 60
_TEMPS = ("for h in /sys/class/hwmon/hwmon*; do [ \"$(cat $h/name)\" = k10temp ] && "
          "cat $h/temp1_input; done; nvidia-smi --query-gpu=temperature.gpu,power.draw "
          "--format=csv,noheader,nounits 2>/dev/null || true")


def temps(dev):
    """{cpu: C, gpu: C or None, gpu_w: W or None}, or None when unreadable."""
    try:
        r = ssh(dev, _TEMPS, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if r.returncode != 0 or not lines:
        return None
    try:
        out = {"cpu": int(lines[0]) // 1000, "gpu": None, "gpu_w": None}
        if len(lines) > 1 and "," in lines[1]:
            g, w = (x.strip() for x in lines[1].split(",")[:2])
            out["gpu"], out["gpu_w"] = int(float(g)), float(w)
    except ValueError:
        return None
    return out


class Heat:
    """Hysteresis on readings: hot after HOT_MINUTES readings at or over the
    limit, cool again once every reading is back under the OK mark."""

    def __init__(self):
        self.hot = False
        self.streak = 0

    def update(self, t):
        if t is None:
            return self.hot
        over = t["cpu"] >= CPU_HOT or (t["gpu"] or 0) >= GPU_HOT
        self.streak = self.streak + 1 if over else 0
        if self.streak >= HOT_MINUTES:
            self.hot = True
        elif self.hot and t["cpu"] < CPU_OK and (t["gpu"] or 0) < GPU_OK:
            self.hot = False
        return self.hot


_heat = {}
_heat_lock = threading.Lock()


def _watch(dev, heat, log):
    while True:
        t = temps(dev)
        was = heat.hot
        heat.update(t)
        if t:
            log(f"temps {dev.name}: cpu {t['cpu']} C"
                + (f", gpu {t['gpu']} C {t['gpu_w']:.0f} W" if t["gpu"] is not None else "")
                + (" -- HOT, new solves wait" if heat.hot else ""))
        if heat.hot and not was:
            log(f"{dev.name}: over {CPU_HOT} C CPU / {GPU_HOT} C GPU for {HOT_MINUTES} min; "
                f"pausing new solves until under {CPU_OK} / {GPU_OK} C")
        if was and not heat.hot:
            log(f"{dev.name}: cooled down, solving again")
        time.sleep(HEAT_EVERY_S)


def heat_of(dev, log=print):
    """This process's heat watch for `dev`, started on first use."""
    with _heat_lock:
        if dev.name not in _heat:
            _heat[dev.name] = Heat()
            threading.Thread(target=_watch, args=(dev, _heat[dev.name], log),
                             name=f"heat-{dev.name}", daemon=True).start()
        return _heat[dev.name]


# --- one solve ----------------------------------------------------------------

@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


def run(dev, cmd, env, timeout_s, push_dirs=(), outdir=None):
    """Run `cmd` on `dev` as if here. Every argument that is an existing
    file is pushed first (same absolute path), plus `push_dirs`; `outdir`
    is pulled back afterwards. Raises DeviceDown when SSH itself fails
    (exit 255), subprocess.TimeoutExpired past `timeout_s`."""
    heat = _heat.get(dev.name)      # started by prepare()
    while heat and heat.hot:
        time.sleep(10)
    cwd = os.getcwd()
    files = sorted({os.path.abspath(a) for a in cmd if os.path.isfile(a)})
    dirs = [os.path.abspath(d) for d in push_dirs]
    if outdir:
        dirs.append(os.path.abspath(outdir))
    _rsync(["-R", "--delete", *files, *dirs, f"{_target(dev)}:/"])

    envs = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
    # The kill happens THERE: an ssh client killed here would leave the
    # solver running on the device.
    line = (f"cd {shlex.quote(cwd)} && env {envs} timeout -s KILL {int(timeout_s)} "
            + " ".join(shlex.quote(str(a)) for a in cmd))
    r = subprocess.run(["ssh", *_ssh_opts(), _target(dev), line],
                       capture_output=True, text=True, timeout=timeout_s + 120)
    if r.returncode == SSH_LOST:
        raise DeviceDown(f"{dev.name}: connection lost: {r.stderr.strip()[-200:]}")
    if r.returncode == 137:      # timeout -s KILL
        raise subprocess.TimeoutExpired(cmd, timeout_s)
    if outdir:
        _rsync([f"{_target(dev)}:{os.path.abspath(outdir)}/", f"{os.path.abspath(outdir)}/"])
    return Result(r.returncode, r.stdout, r.stderr)
