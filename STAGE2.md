# Stage 2 — Engine Harness

**This stage is the entire risk of the project.** Everything downstream is ordinary
Python; this is the part that can eat weeks. Budget accordingly and time-box it.

## Goal

Drive a Yu-Gi-Oh! duel from Python: load a deck, step the engine, read the game state,
answer its prompts, and — critically — **set up arbitrary board states**.

## Approach, in order of preference

1. **[ygorl](https://github.com/n0tevi1/ygorl)** — PyTorch/pybind11, Linux, documents a
   no-GPU path, already builds the C++ extension. Most likely to compile cleanly here.
2. **[ygo-env](https://github.com/izzak98/ygo-env)** — self-contained Gymnasium wrapper.
3. **[ygoenv](https://github.com/sbl1996/ygo-agent)** — fast and well-proven, but drags
   in JAX and envpool, which is more than this project needs.
4. **Raw `ygopro-core` via ctypes** — the fallback. More work but zero dependency risk.

Do not spend more than a few evenings on any one option before trying the next.

## What the engine gives you

`ygopro-core` is a message-passing state machine and is headless by design — EDOPro is
just a GUI over it. The shape of the C API:

- `create_duel(seed)` → handle; `start_duel(handle, options)`; `end_duel(handle)`
- `set_player_info(handle, player, lp, startcount, drawcount)`
- `new_card(handle, code, owner, player, location, sequence, position)` — **this is how
  you construct arbitrary board states.** Stage 4's board-breaking puzzles depend on it.
- `process(handle)` then `get_message(handle, buf)` — pump and read
- `set_responsei` / `set_responseb` — answer the engine's prompts
- `query_card`, `query_field_card`, `query_field_info` — read state

The single most valuable thing here is not the duel loop. It is that the engine
**enumerates legal actions for any state**. That is what makes Stage 3 possible.

## Build

### `skills/engine.py`
- Load core + `CardScripts` + `cards.cdb`, with paths and **versions pinned** in config.
- `Duel` class: create, seed, load decks, step, query, respond, teardown.
- Decode the message enum into typed Python events (`SelectIdleCmd`, `SelectChain`,
  `SelectCard`, …). This decoding layer is most of the work.
- `snapshot()` / `restore()` or re-simulation from seed — Stage 3's search needs to
  explore branches and come back. **Decide this early**; retrofitting it is painful.
- `set_board(spec)` — build a named board state from a JSON description.

## Deliverables

1. Load a `.ydk`, run a full duel to completion headless, deterministically by seed.
2. Same seed → byte-identical game log. Non-negotiable; everything later assumes it.
3. Enumerate legal actions at an arbitrary decision point.
4. Construct a board from JSON and query it back correctly.
5. Branch and return: explore an action, roll back, explore another.

## Gotchas

- **Pin `ygopro-core`, `CardScripts`, and `cards.cdb` together and record the versions
  in every `runs` row.** They move independently and a mismatch produces silent
  misbehaviour rather than an error.
- Determinism is easy to lose to dict ordering or unseeded tiebreaks in your own code,
  not just the engine. Test it explicitly.
- Branch-and-return via re-simulation from seed is simpler and more reliable than
  snapshotting engine memory. Slower, but Stage 3's hand-level cache absorbs most of it.

---
Delete this file when the stage is complete and verified.
