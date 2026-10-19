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
- Login **rate limited from the first commit** (8 per 5 min per address),
  counted **in SQLite, not in process memory**. The qBittorrent WebUI here was
  actively brute-forced while exposed. An in-process counter gives every
  gunicorn worker its own allowance — the real limit becomes 8 x workers, and it
  resets whenever a worker recycles. A correct password is refused while limited.
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
- [x] **3 — the four views.** Report, handtrap matrix, threats, line drilling.
      Built. The uncertainty design is real rather than decorative: solve rates
      render as confidence bands that visibly widen on small samples, a deck
      whose target is flagged greys its own panel with the reason attached,
      every measured panel states its source file and budget, Maxx "C" survival
      is never shown without its cost beside it, and the matrix says which
      direction is good — including when the sweep does not contain your deck
      at all, which otherwise reads as a matchup picture that happens to omit
      you.
- [x] **4 — deckbuilder.** Built. Searches all 14,618 cards; the 1,088 that
      appear in a scraped tournament list rank first, and the deck's own
      archetype ranks ahead of that when one is identified. Two decisions
      carry the weight:

      *Ranking is by a Wilson lower bound, not by the raw inclusion rate.*
      Sorting on `pct_lists` puts a card in 100% of two lists above one in
      85% of forty, at the exact spot in the UI where a reader stops
      scrolling. Each row also draws the gap: a solid bar to the lower
      bound, a ghost bar out to the point estimate, with "87% of 94"
      beside it. A one-list card is visibly almost all ghost.

      *Legality is computed only by `Deck.validate`, over HTTP, on every
      change.* A copy of the banlist in JavaScript would drift from the
      CLI's answer the first time a list updated, and the builder would
      show a legal deck the engine refuses. Saving an illegal list is
      allowed — a list in progress usually is — but never silently, and
      the save path reuses `importer.save`'s filename sanitising and runs
      the same solver preflight as import.
- [x] **5 — images at full scale, hardening pass, deploy.** Built.

      *Images.* `skills/images.py` + an `images` CLI command fetch the pool
      into `data/cardimg` at 10 req/s, resumable, meta-played first. Bodies
      land under `<id>.jpg.part` and are renamed only when complete: a
      truncated JPEG under the final name is skipped by every later run,
      so that card is broken forever. A 404 goes in a `missing` ledger; a
      timeout does not. An uncached card gets a placeholder SVG marked
      `no-store`, never a broken image and never a request to their CDN.

      *Hardening.* One real bug: the import upload interpolated the
      browser-supplied filename into a path, and
      `duelroom-upload-../../../../tmp/x` normalises to `/tmp/x` because
      the prefix becomes a directory component the next `..` pops off.
      Also: one `deck_path()` instead of four hand-built paths, an
      allowlist on import URLs with redirects off, security headers, and a
      login timing fix. Details in the git log.

      *Deploy.* `tmux-start.sh` / `tmux-stop.sh` + `duelroom.service` and a
      watchdog timer, matching the stockroom pattern — including its
      lesson that killing the tmux session only SIGHUPs gunicorn, which
      treats it as a reload and keeps holding the port. Leaf cert issued
      from the existing local CA, so the browser that already trusts
      stockroom trusts this. Verified under the real stack: TLS validates,
      the cookie is Secure + HttpOnly + SameSite=Lax, all eight pages
      serve, and **the login limiter held at 8 across two separate
      gunicorn workers** — the exact failure the SQLite move was for,
      now proven in the configuration that has it rather than in a test
      client.

## Turning it on

Nothing is enabled: the one thing this cannot do for you is choose a
password.

```bash
venv/bin/python web/manage.py set-password     # writes data/web.env, mode 600
venv/bin/python web/manage.py check            # every row should say ok
sudo systemctl enable --now duelroom.service duelroom-watchdog.timer
```

Then `https://10.0.0.96:8446/`. Port-forwarding 8446 at the router is the
separate, deliberate step that makes it reachable from outside.

```bash
venv/bin/python web/manage.py unlock           # if you lock yourself out
tmux attach -t duelroom                        # gunicorn and worker logs
```

## Running it

```bash
venv/bin/python -c "from werkzeug.security import generate_password_hash as g; \
    print(g(input('password: ')))"
export DUELROOM_PASSWORD_HASH='pbkdf2:sha256:...'
venv/bin/python web/run.py          # 127.0.0.1:8446
```

Binding beyond localhost is a deployment decision (stage 5), not a development
default.
