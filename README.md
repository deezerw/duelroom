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
  confidence intervals overlap almost everything.
- **A solver budget that is too low invents bricks.** 10s reports a hand dead that
  60s solves in 9 actions. Run `ladder <deck>` per deck rather than assuming.
- **Some decks have no measurable target.** If a deck does not build a board, its
  solve rate is about a goal it never meant to reach. `report` flags this.
- **Maxx "C" and Droll score 0% stop** because the metric asks only whether a line
  reaches its target, and those cards punish a combo rather than stopping it.
- **`locals.toml` is a stand-in** until you edit it with your actual room.

`tools/audit_fields.py` checks for the specific bug class that produced most of the
above: code reading a record field that is never present, silently defaulting, and
reporting a plausible number.

## Requirements

Python 3.12, `libsqlite3-dev`, and a C++17 toolchain for the vendored engine. The
vendored submodules and the merged card database are not in this repo; see the setup
notes in `skills/engine.py` and run `daemon.py setup` after cloning.
