# Stage 10 — Run tests on another machine (duelpc)

Goal (user, 2026-11-17): the app stays on the server only, but a test can be
sent to another device — the gaming PC's Ubuntu SSD, `duelpc` (10.0.0.153,
7800X3D 16 threads, 30 GB, Wake-on-LAN tested) — and started while nobody
is home. Installer: `tools/duelpc/`. Delete this file when every step below
is done and verified.

Design: **path mirroring.** duelpc has the same user and the same absolute
paths (`/home/deezer/duelroom/...`) for everything the solver reads, so a
solver command line runs there unchanged. The server keeps all logic, all
state and the database; duelpc only ever runs `combosolver`.

1. **Provision** (`skills/remote.py sync`): rsync the rooted solver build, the
   merged card DB and the CardScripts trees to duelpc at the same paths;
   write a version stamp (hashes of binary + cards.cdb + scripts rev) on both
   sides. A remote solve refuses to start if the stamps differ.
2. **Remote solve** (`game._solve`): when the job's device is remote, rsync the
   command's input files (every arg that is a file) and the workdir to the
   same paths, run the identical command over one multiplexed SSH
   connection under `timeout -s KILL`, rsync the outdir back. Exit 255
   (SSH lost) -> wake + wait + one retry; still down -> the job stops
   (games are resumable, nothing half-recorded).
3. **Devices in the job queue**: `params.device`; the worker runs ONE job per
   device at once (one lane each), so the server and duelpc work in
   parallel. Per-device workers count: server 4, duelpc 8 (2 threads each).
4. **Results keyed by device**: `device` column in the four game tables, in
   the primary key (rebuild migration like `_migrate_game_first`; old rows
   = 'server'). Every read filters by device; the games/impact/stops pages
   get a device switch. Machines with different search speed are never
   pooled into one number.
5. **Wake on demand**: before a duelpc job, SSH check; else Wake-on-LAN
   (MAC 10:ff:e0:86:34:42) and wait up to 4 min; still down -> job fails
   with a clear message.
6. **Benchmark**: the same stored turn-3 positions on both devices, paired
   (states searched + wins), so a later choice to scale duelpc's budget to
   match the server is measured, not guessed.
