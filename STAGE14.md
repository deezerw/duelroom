# Stage 14 — A searched defender (user chose this 2026-11-18)

Why: the scripted defender is readable by the attacker's search. A family of 8 scripts
(tools/experiments/defender_family.py) stopped NO more converged kills than today's two
(their kill 92% -> 90%, our break 81% -> 81%). So either the boards really are breakable,
or only a defender that picks its own replies would hold. This stage finds out which.

## Design: the defender best-responds on the attacker's line (double oracle)

The attacker's search stays single-agent (NRPA). The defender's search is an OUTER loop
in Python around it, at the defender's chain windows on the attacker's winning line:

    policy = the script (game.DEFENCES[0]) + no rules
    repeat up to K times:
        L = the attacker's best line against `policy`, searched from the root
        no win                -> SURVIVED (the policy holds)
        branches(L): every defender chain window on L where it could have answered
            differently (fire another eligible piece, or pass instead of firing)
        for each branch: the attacker searches again ROOTED just after it
        every branch beaten  -> LOST (no single reply on that line stops it)
        some branch holds    -> confirm it on a 2nd seed (a 20 s search misses kills);
                                add it to `policy` as a RULE, go round again
    K rounds, the defender always had a reply -> SURVIVED

The rule is what makes the next round fair: the attacker re-plans FROM THE ROOT knowing
the defender now answers that way (a rooted re-search alone would freeze the attacker's
moves before the window, which flatters the defender). A rule is keyed by the window's
trigger: "fire P when the window answers card T" / "do not fire P on T", T = the last
chain link's card, else the card just summoned.

Not covered (limits to write down, not to fix now): the defender's follow-up choices
(which card S:P banishes) stay patch 0004's by-value pick; information is perfect on
both sides, as everywhere in the solver.

## Steps

1. **Solver patch 0006** (rooted binary, `build_rooted_solver.sh`):
   `--opp-branches <dir>`: replay the line, and at every defender chain window write one
   prefix replay per alternative it did not take, plus `branches.jsonl` (window index,
   trigger, options, chosen). `--opp-rule P:T` / `--opp-skip P:T` (repeatable) in the
   smart defender's window choice. Test: a stored lost kill position, a hand-picked
   alternative, the prefix replays to the window and the rule fires.
2. **`game.searched_kill(...)`**: the loop above, budgets as parameters, every search
   logged; unit tests with a fake solver.
3. **Measure** on the plateau's 72 kill positions (duelpc) at the games' budget and at
   4x: scripted (1,2) vs searched. Report how many of the converged-lost positions the
   searched defender saves, and the cost per position. The same for our break (going
   second), where WE are the attacker and their board is the defender.
4. If it saves a real share: make it the games' turn-2 check (ask the user first: it is
   the measurement every number rests on), re-check the plateau, set budgets, re-play.
   If not: write down that the boards really are breakable, and go on with budgets.

## Result, kill check (2026-11-18): the searched defender stops nothing real

72 plateau runs at 4x (OTK_MS x 4), duelpc, 0 failures, 2.5 min and 5 searches per position.
Attacker wins: scripted family 1+2 65%, searched 74% (it starts from ONE script), beats
both 57%. It takes away 6 of the scripted wins and gives 12 -- seed noise from one more
randomized search, not defence: 10 runs learned a rule, and in all 10 the attacker
re-planned around it and won; 19 runs were held by the plain script; the rest the attacker
beat every reply on its line (8 of its lines passed no defender window with any choice).
Seeds agree on 81% of positions.

**Read:** our turn-1 boards really are breakable; no single reply at any window saves them.
The lever is the board, not the defender (see the game review of h16/e1009 run 8: the
games' turn 1 falls back by goal overlap and leaves free interruptions in hand). DEFENCES
stays (1, 2); `game.searched_defence` stays as a tool, not the games' check.

Not run: the brk stage (their board, we break). Their turn-1 board comes from a ONE-card
target (Angelechy Endgame Problem@szone), so the enemy activates it and passes; measuring
a defender on that board says nothing until the enemy has real goals (asked the user).

**2026-11-19, gbrk (the enemy defends with a searched defence, the games' going-second
positions: their TRAINED turn 1, our break at 8x), 76 positions:** attacker (us) wins 67%
against the scripted family, 68% against the searched defender; it took away 3 scripted wins
and gave 4 (seed noise). No reply on our line saves them in 52 positions. Going second, the
scripted defender does not flatter our breaks: the 66% stands. Both sides of the defender
question now read "the script is as good as a searched reply"; the searched defender stays a tool.
