# Stage 6 — Watch a combo line on a real playmat

Delete this file once the stage is complete and verified.

## Goal

A **Watch** button next to every solved line, on the pilot report and the drill page.
It opens a playmat that plays the line out step by step the way a duel client does:
both fields with real zones (5 main monster, 2 extra monster, 5 spell/trap, field
spell), hand, GY, banished, deck and extra counts, card art from the local cache, and
cards moving between zones. Play/pause, step forward/back, speed, a step list you can
click to jump, and a caption for each step ("activate Aluber — searched Branded
Fusion").

## What already exists (do not rebuild)

- `handlab.steps()` replays a line from its raw responses with an observer over the
  message stream. Same mechanism here.
- `ygorl.solver.targets.board_summary(core, turn, lp)` reads both players' zones from
  the live core, with **true zone indices** (`sequence`), positions and overlays.
  Prototype on `results/sweep_active.jsonl`: a 115-response Blue-Eyes line gives 270
  observer buffers and reads back the full end board.
- `/img/<size>/<id>.jpg` serves the local card-art cache.

## Design

**Frames are authoritative snapshots, not a reimplemented board model.** One
`board_summary` per observer buffer, after that buffer's messages. A hand-tracked zone
model, built by applying MSG_MOVE, drifts on hand reordering, overlays and pendulum
zones. A snapshot cannot drift. Frames with no visible change merge into the
neighbouring frame. The buffer's `Move` messages go into the frame as
`moves: [{code, from, to}]`, and the frontend uses them only to animate: the card flies
from its old slot to its new one.

**The record has to be stored to be watched.** Pilot reports kept only the readable
steps, not the responses. `analyse` now stores `replay = {start, lines:[{responses}]}`
for the main line and for every hand-trap recovery line. Older reports cannot be
watched, and the page says so instead of hiding the button. Drill lines come from
sweep/board records, which already hold `start` + `lines`.

**Frames are computed on request and cached** next to the report
(`<rid>.frames.<line>.json`). A replay costs a fraction of a second, so the cache is
for the cost of the big JSON response, not for correctness.

**No card-back asset.** Face-down cards are drawn in CSS.

## Pieces

1. `skills/playback.py`: `frames(conn, start, responses) -> {"frames", "names"}`.
2. Pilot reports store `replay`; `analyse` and the trap recoveries both.
3. Routes: `/watch/pilot/<deck>/<rid>?line=main|<trap code>` and
   `/watch/drill/<deck>?src=<file>&hand=<i>&line=<j>`, plus a `.json` variant that
   returns the frames.
4. `web/templates/watch.html` + `web/static/playmat.js`: playmat, controls, step
   list, FLIP-style move animation.
5. Watch buttons on the pilot report (main line + each recovery) and on the drill page.

## Verified when

- A long line (the 115-response Blue-Eyes line) plays start to finish, and its last
  frame equals the record's stored end board, card for card and zone for zone.
- A line with an Xyz shows its materials, and one with a face-down set shows a back.
- Old pilot reports say "re-run to watch" instead of breaking.
- Works on a phone.
