# duelroom

A Yu-Gi-Oh! deck laboratory. Scrapes the current meta from YGOPRODeck, runs a
solitaire search over the real rules engine to find the best line from any hand, and
uses that to answer the questions that actually win locals: is my deck consistent, can
it play through handtraps, can it break what it will face, and which flex slots should
change.

It is deliberately **not** a bot-vs-bot arena. See
[Why No Duel AI](CLAUDE.md#why-no-duel-ai-yet).

## Quick start

```bash
source venv/bin/activate
python daemon.py                       # interactive
python daemon.py report my-deck        # or one-shot
```

```
duelroom> import ydke://...            # or a .ydk path, or a ygoprodeck deck URL
duelroom> mydeck                       # commands now default to this deck
duelroom> meta                         # scrape recent tournament lists
duelroom> locals auto                  # weight the meta by what you'll face
duelroom> report                       # everything known about your deck
duelroom> gate                         # measure it (slow: minutes per run)
duelroom> drill                        # export best lines as EDOPro replays
```

`help` lists every command.

## How it works

The engine is [ygorl](https://github.com/n0tevi1/ygorl) (wrapping edo9300's
`ygopro-core` with Project Ignis card scripts) plus
[ygo-combo-solver](https://github.com/96jonesa/ygo-combo-solver) for turn-1 line
search. duelroom implements no rules engine of its own.

Two observations make the whole thing work without an opponent AI:

1. **Searching your own turn to its best board is optimal piloting.** A solver beats
   any learned policy at the solitaire problem, and it is the same machinery that
   discovers combo lines.
2. **Board breaking is a static benchmark, not a duel.** Point the solver at a meta
   deck to generate its real turn-1 boards, then deal yourself a going-second hand.

Setup, architecture, and the full list of known limitations are in
[CLAUDE.md](CLAUDE.md). Current work queue is in [BACKLOG.md](BACKLOG.md).

## Read the numbers carefully

This tool is built to avoid confident wrong answers, because that has been its most
common failure. Several things it reports are weaker than they look, and it says so
in its own output:

- **Sample sizes are small** unless you have run a long batch. Under 20 hands the
  confidence intervals overlap almost everything. Measured: going from 12 to 40 hands
  left the board library's conclusions unchanged but REORDERED the handtrap ranking's
  top two, because a matrix cell only counts hands that solved and then had a legal
  window. Trust the matrix least.
- **A solver budget that is too low invents bricks.** 10s reports a hand dead that
  60s solves in 9 actions. Run `ladder <deck>` per deck rather than assuming.
- **Some decks have no measurable target.** If a deck does not build a board, its
  solve rate is about a goal it never meant to reach. `report` flags this.
- **Maxx "C" and Droll score 0% stop** because the metric asks only whether a line
  reaches its target, and those cards punish a combo rather than stopping it.
- **`locals.toml` is a stand-in** until you edit it with your actual room.

```bash
venv/bin/pytest tests/ -q                 # regression tests, under a second
venv/bin/python tools/check_invariants.py # facts that cannot all be true
venv/bin/python tools/audit_fields.py     # fields read but never present
venv/bin/python tools/mutation_test.py    # break the code, check something notices
```

The suite covers bugs that were actually found here, not hypothetical ones, and each
test was verified by reintroducing its bug and watching it fail. `tools/audit_fields.py` checks for the specific bug class that produced most of the
above: code reading a record field that is never present, silently defaulting, and
reporting a plausible number.

## Setup from a fresh clone

The repo carries source and decklists only. The rules engine, card scripts and card
database are large third-party trees under `vendor/` and are gitignored, so a clone
cannot run a solver command until they are fetched and built.

```bash
./bootstrap.sh            # full setup, safe to re-run
./bootstrap.sh --check    # report what is missing, change nothing
```

Needs Python 3.12, a C++17 toolchain, cmake 3.20+, and `libsqlite3-dev`. The script
checks for those first and prints the apt line if any are absent.

It encodes several things that are not obvious and fail unhelpfully when skipped:
submodules must be fetched `--recursive` (ygopro-core nests Lua as its own submodule,
and without it the build reports "ygopro-core sources not found" while the directory
looks populated); upstream's `>=3.11,<3.12` Python pin is conservative and is relaxed
rather than installing a second interpreter; and the card database must be merged,
because recent sets ship as separate `release-*.cdb` files that the engine never loads
and a deck holding one of those cards segfaults the solver.
