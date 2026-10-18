# duelroom web UI

Lives in `web/` inside the duelroom repo, not beside it, so it imports `skills.*`
and reads the same SQLite. Two copies of the analysis code, or two databases,
would drift — and the thing this project has proved worst at is noticing when a
number stopped meaning what it says.

## Settled

- **Full replacement.** Everything the CLI does plus deckbuilding; the CLI becomes
  the debugging tool.
- **Internet-facing, single password.** gunicorn + TLS on **:8446**
  (8443 flipper, 8444 stockroom, 8445 nextcloud, 8447 taken).
- **Card images cached locally.** YGOPRODeck's ToS forbids hotlinking. ~14.6k
  cards: small crops for lists (~220MB), full art on hover (~1.5GB), fetched as a
  resumable background job at their 20 req/s limit.
- **Deckbuilder searches the full pool, meta-played cards ranked first**, with
  inclusion rates shown inline.
- **Long jobs queue** with live progress and partial results read from the
  checkpoint ledger. Close the tab, come back. No notifications.

## The design rule

Uncertainty is drawn, not footnoted. A web UI is structurally biased toward
confident numbers — a stat tile looks authoritative in a way a terminal warning
next to its own number does not. Last night's work was almost entirely about
making uncertainty visible, and the easy version of this UI would undo it.

- Solve rates render as **interval bars**, so a 2-hand run looks visibly useless
  beside a 40-hand one
- A deck with a flagged target gets its whole panel **greyed, with the reason on
  it** (`.panel.untrusted`), never a green `0%`
- Every measured panel states **source file, hand count, solver budget**
- Maxx "C" shows **two numbers side by side** — survival and cards surrendered —
  because either alone misleads
- If a number cannot carry its caveat, it does not get a tile

## Security, from this machine's own history

- `duelroom_session` cookie, **not** the Flask default `session`: stockroom and
  flipper both used the default and logged people between apps.
- Login **rate limited from the first commit** (8 per 5 min per address). The
  qBittorrent WebUI here was actively brute-forced while exposed. A correct
  password is refused while limited.
- `safe_next()` rejects anything absolute, scheme-bearing or `//`-prefixed —
  open redirect on the post-login `next` was a real finding in stockroom *and*
  flipper.
- Secret key generated once into a mode-600 file, so a restart does not log you
  out and nothing is hardcoded.

## Stages

- [x] **1 — skeleton, auth, decks.** App factory, hardened login, deck list, deck
      detail with archetype inference, import (ydke / URL / .ydk upload) with the
      engine preflight, ydk + ydke export.
- [x] **2 — job queue.** `jobs` table, one worker, strictly one solver job at a
      time (the engine takes 6 of 8 cores). Live progress. Every view that reads
      derived numbers must know a job is mid-flight — the CLI shipped a bug where
      a half-written sweep was read as complete and reported 1 of 7 decks as the
      whole meta. Built: `jobs` table, `web/worker.py` (run exactly one),
      self-refreshing job page, partial optimize results from the checkpoint
      ledger shown greyed while the run continues. A crashed worker resets
      orphaned `running` rows on restart, or they would block the queue forever.
      Running jobs cannot be cancelled from the UI — killing a solver mid-sweep
      is what leaves partial records that read as complete.
- [ ] **3 — the four views.** Report, handtrap matrix, threats, line drilling.
      This is where the uncertainty design actually lives.
- [ ] **4 — deckbuilder.** Search 14.6k cards with meta ranking, live banlist
      validation, save to .ydk. Biggest single chunk; effectively a mini
      YGOPRODeck.
- [ ] **5 — images at full scale, hardening pass, deploy.** systemd unit,
      gunicorn + TLS, matching the stockroom pattern.

## Running it

```bash
venv/bin/python -c "from werkzeug.security import generate_password_hash as g; \
    print(g(input('password: ')))"
export DUELROOM_PASSWORD_HASH='pbkdf2:sha256:...'
venv/bin/python web/run.py          # 127.0.0.1:8446
```

Binding beyond localhost is a deployment decision (stage 5), not a development
default.
