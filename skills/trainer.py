"""The deck trainer: play every hand over and over, keep the best line ever found.

The "AI that learns the deck" this project can honestly build. Two things
it is NOT, both measured before this was written (STAGE7.md has the
numbers):

- **Not a neural network.** ygorl's PPO and behaviour-cloning runs on a
  CPU learned nothing usable for a combo deck: the reward has to travel
  back through ~1,000 decisions, and an hour buys ~1,300 games.
- **Not a smarter solver.** The combo solver can warm-start its move
  policy from a corpus of past lines (`--adapt`). It works -- the policy
  reproduces 90% of the corpus -- but loading the corpus costs about what
  it saves: at equal wall time, plain extra budget did as well.

What DOES improve with repetition is the board. The solver is a
randomized search that stops at the first line meeting its targets, so
any one attempt is one sample, and the best board a hand can reach is
usually not the one a single attempt returns (CLAUDE.md: elfnote went
from 3.73 to 4.80 interruptions a hand with more draws). The trainer
turns that into memory: a fixed pool of hands, a few attempts per hand
per GENERATION, each aimed where the evidence says a better board might
be, and the best line ever found kept per hand -- watchable on the
playmat. The learning curve is the pool's mean best board per generation.

Where each generation aims, in order:

1. A hand never attempted gets the breadth goals -- its base plus each
   interrupter piece -- exactly as `sweep` asks them.
2. Bigger stacks built from the pieces that solved (`combinations_to_try`):
   the boards nobody has asked this hand for yet.
3. Near misses: goals that failed but placed some of their cards, biggest
   first. A miss at one seed is a sample, not a proof.
4. The current best goal again, at a fresh seed: the same goal can end on
   a better board, because everything beyond the targets is incidental.

Every attempt's solver seed comes from (hand seed, goal, draw number) and
the draw number counts that goal's previous attempts, so a rerun of the
same history does the same work, and every new attempt is a new sample.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import engine, scenarios, sweep

STATE = engine.ROOT / "results" / "trainer"
HANDS = 24          # the pool; the learning curve is measured over it
PER_HAND = 3        # solves per hand per generation, after the first
BIG_STACK = 3       # a goal of this many pieces or more gets...
BIG_STACK_X = 2     # ...this many times the budget: 3-piece stacks never
                    # solved inside 60 s on the first deck trained
# The trainer's per-attempt budget: three times sweep's. sweep's 60 s is
# where the BRICK COUNT settles (engine.SOLVE_MS), which is a cheaper
# question than "the best line this hand has": at 10 s -> 20 s the hard
# goals went 10/24 -> 11/24 and never flattened (STAGE7), and the lines
# the trainer misses are the long ones, where one early choice (which
# materials) only pays off ten decisions later. Nobody waits on it.
TRAIN_MS = 180_000
PLATEAU = 6         # generations in a row with no hand improved = "as good
                    # as it'll get" (train ... <enemy>); 3 stopped the first
                    # deck after 9 attempts a hand
EXT = "ext:"        # a goal's "this line must use <code>" requirement


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deck_hash(deck_path):
    """What makes two decks the same deck: the cards, not the file name."""
    from ygorl.cards.ydk import load_ydk
    d = load_ydk(Path(deck_path))
    blob = ",".join(map(str, sorted(d.main))) + "|" + ",".join(map(str, sorted(d.extra)))
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


# --- stopping ----------------------------------------------------------------
#
# A running job cannot be cancelled from the web UI -- killing a solver
# mid-run leaves partial records that read as complete (web/jobs.py). So
# the trainer is told to stop and does so between generations, where
# everything it has is already committed.

def stop_path(name):
    return STATE / f"{name}.stop"


def request_stop(name):
    STATE.mkdir(parents=True, exist_ok=True)
    stop_path(name).write_text(_now())


def stop_requested(name):
    return stop_path(name).exists()


def clear_stop(name):
    stop_path(name).unlink(missing_ok=True)


# --- the player's preferences -----------------------------------------------
#
# Two things the player can tell the trainer, kept by deck NAME:
#
# - a move to AVOID, clicked on the playmat. Activations are forbidden in
#   the search (`--no-activate` for the zone, `--no-chain` for the card's
#   chain-window effects, which is broader: the solver's chain filter has
#   no zone). Summons have no solver flag, so they are only discouraged in
#   the ranking -- and a summon to avoid is never asked for as a goal.
#   Either way the ranking puts every line that avoids them first, so an
#   old best that uses one stays only until something without it is found.
# - a card that COUNTS as an interruption on the field. Upstream reads
#   interruptions from the card scripts and misses many (on the first deck
#   trained: Kaleido-Heart, Dragostapelia, Mudragon), and a card it does
#   not count is neither scored nor ever asked for.

AVOID = ("avoid_activate", "avoid_summon")
KINDS = (*AVOID, "interrupter", "extender")
# playback's zones -> the solver's. A spell shows as activated in the
# spell & trap zone even when it was played from the hand, so "sz" bans both.
_SOLVER_ZONES = {"hand": ("hand",), "mz": ("mzone",), "sz": ("szone", "hand"),
                 "gy": ("grave",), "ban": ("banished",), "extra": ("extra",)}


def prefs(conn, name):
    """{"avoid": {("activate", code, zone) | ("summon", code)},
    "interrupters": {code}, "extenders": {code}, "values": {(code, zone): worth}}
    -- the shape `playback.line_moves` speaks."""
    out = {"avoid": set(), "interrupters": set(), "extenders": set(),
           "values": {(r["code"], r["zone"]): r["value"] for r in conn.execute(
               "SELECT code, zone, value FROM card_values WHERE deck=?", (name,))}}
    for r in conn.execute("SELECT kind, code, zone FROM trainer_prefs WHERE deck=?",
                          (name,)):
        if r["kind"] == "avoid_activate":
            out["avoid"].add(("activate", r["code"], r["zone"]))
        elif r["kind"] == "avoid_summon":
            out["avoid"].add(("summon", r["code"]))
        elif r["kind"] == "interrupter":
            out["interrupters"].add(r["code"])
        elif r["kind"] == "extender":
            out["extenders"].add(r["code"])
    return out


def set_pref(conn, name, kind, code, zone="", on=True):
    if kind not in KINDS:
        raise ValueError(kind)
    with conn:
        if on:
            conn.execute("INSERT OR IGNORE INTO trainer_prefs (deck, kind, code, "
                         "zone, added) VALUES (?,?,?,?,?)",
                         (name, kind, int(code), zone or "", _now()))
        else:
            conn.execute("DELETE FROM trainer_prefs WHERE deck=? AND kind=? AND "
                         "code=? AND zone=?", (name, kind, int(code), zone or ""))


VALUE_MAX = 5
VALUE_ZONES = {"field": "on the field", "grave": "in the GY", "hand": "in the hand"}


def _value_zone(where):
    """A `board_interruptions` place -> a card_values zone (set cards are on the field)."""
    return "field" if where in ("field", "set") else where


def set_value(conn, name, code, zone, value):
    """What one interruption of `code` in `zone` is worth, 0 .. VALUE_MAX in
    halves (0: it does not count there). None clears it back to the
    scripts' reading (1 where they see an interruption, else nothing)."""
    if zone not in VALUE_ZONES:
        raise ValueError(zone)
    with conn:
        conn.execute("DELETE FROM card_values WHERE deck=? AND code=? AND zone=?",
                     (name, int(code), zone))
        if value is not None:
            v = round(float(value) * 2) / 2
            if not 0 <= v <= VALUE_MAX:
                raise ValueError(value)
            conn.execute("INSERT INTO card_values (deck, code, zone, value, added) "
                         "VALUES (?,?,?,?,?)", (name, int(code), zone, v, _now()))


def value_options(conn, deck_path, p):
    """One row per (card, zone) that counts: every place the scripts read
    the card as an interruption (worth 1 unless set), every place the
    player gave it a value, and the field for a marked interrupter.
    [{code, name, zone, where, value, set, known}], highest value first."""
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import card_interruptions

    from . import card_db

    cards, deck, out = default_cards(), load_ydk(Path(deck_path)), []
    for code in dict.fromkeys(cards.canonical(c) for c in (*deck.extra, *deck.main)):
        known = {_value_zone(i.where) for i in card_interruptions(code, cards)}
        zones = known | {z for (c, z) in p["values"] if c == code}
        if code in p["interrupters"]:
            zones.add("field")
        name = (card_db.resolve(conn, code) or {"name": f"#{code}"})["name"]
        for z in sorted(zones, key=list(VALUE_ZONES).index):
            v = p["values"].get((code, z))
            out.append({"code": code, "name": name, "zone": z, "where": VALUE_ZONES[z],
                        "value": _num(1 if v is None else v), "set": v is not None,
                        "known": z in known})
    return sorted(out, key=lambda o: (-o["value"], o["name"], list(VALUE_ZONES).index(o["zone"])))


def _num(x):
    """3.0 -> 3: keys and labels stay whole numbers until a value is not."""
    x = round(x, 2)
    return int(x) if float(x).is_integer() else x


def prefs_table(conn, name):
    from . import card_db
    rows = []
    for r in conn.execute("SELECT * FROM trainer_prefs WHERE deck=? ORDER BY added",
                          (name,)):
        card = card_db.resolve(conn, r["code"]) or {"name": f"#{r['code']}"}
        rows.append({**dict(r), "name": card["name"]})
    return rows


def interrupter_options(conn, deck_path, p):
    """The deck's monsters for the "counts as an interruption" picker:
    [{code, name, known, on}] -- `known` when upstream already counts it
    on the field, `on` when the player added it."""
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import card_interruptions

    from . import card_db

    cards, deck, out = default_cards(), load_ydk(Path(deck_path)), []
    for code in dict.fromkeys(cards.canonical(c) for c in (*deck.extra, *deck.main)):
        card = cards.get(code)
        if card is None or not card.is_monster:
            continue
        known = any(i.where == "field" for i in card_interruptions(code, cards))
        out.append({"code": code, "known": known, "on": code in p["interrupters"],
                    "name": (card_db.resolve(conn, code) or {"name": f"#{code}"})["name"]})
    return out


def extender_options(conn, deck_path, p):
    """Every card of the deck for the "extender" picker: [{code, name, on}].
    Spells and traps too -- an extender is any card a line should go
    through, and a spell is "used" by resolving it."""
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards

    from . import card_db

    cards, deck, out = default_cards(), load_ydk(Path(deck_path)), []
    for code in dict.fromkeys(cards.canonical(c) for c in (*deck.main, *deck.extra)):
        if cards.get(code) is None:
            continue
        out.append({"code": code, "on": code in p["extenders"],
                    "name": (card_db.resolve(conn, code) or {"name": f"#{code}"})["name"]})
    return sorted(out, key=lambda o: o["name"])


def split_goal(targets):
    """(board targets, extender codes): an extender rides in a goal as
    `ext:<code>` beside the board cards, so the goal's history, retries and
    draw counting treat "this board, through Requiem" as its own goal."""
    board = [t for t in targets if not str(t).startswith(EXT)]
    ext = [int(str(t)[len(EXT):]) for t in targets if str(t).startswith(EXT)]
    return with_equips(board), ext


def with_equips(board):
    """A goal's board as the solver must see it: a field target that only
    works equipped (`drill.NEEDS_LINK_EQUIP`) brings its equip along, so
    "82135803" means a Desirae that can negate. Applied here, where every
    goal becomes `--target`s (trainer and games), rather than to stored
    goals: their history, retries and stacking keep their keys."""
    from .drill import NEEDS_LINK_EQUIP
    out = list(board)
    for t in board:
        code, _, zone = str(t).partition("@")
        equip = NEEDS_LINK_EQUIP.get(int(code)) if code.isdigit() else None
        if (equip and zone in ("", "mzone", "atk", "def")
                and not any(str(x).startswith(f"{equip}@") for x in out)):
            out.append(f"{equip}@szone")
    return out


def extender_args(p, required=(), cards=None):
    """Solver flags for the extenders. Every one is a `--hint` on every
    attempt (an NRPA sampling bonus for moves that engage it: a nudge, not
    a rule). A REQUIRED one is a rule, checked at the goal: a monster must
    be summoned (`--summon-min`), a spell or trap resolved (`--resolve`).
    The solver watches at most four such cards."""
    from ygorl.engine.duel import default_cards

    args = []
    for c in sorted(p["extenders"]):
        args += ["--hint", str(c)]
    if required:
        cards = cards or default_cards()
    for c in list(dict.fromkeys(required))[:4]:
        card = cards.get(c)
        args += (["--summon-min", str(c)] if card is not None and card.is_monster
                 else ["--resolve", str(c)])
    return args


def solver_args(p):
    """The avoided activations as solver flags."""
    args, chained = [], set()
    for m in sorted(p["avoid"], key=str):
        if m[0] != "activate":
            continue
        for z in _SOLVER_ZONES.get(m[2], ()):
            args += ["--no-activate", f"{m[1]}@{z}"]
        if m[1] not in chained:
            chained.add(m[1])
            args += ["--no-chain", str(m[1])]
    return args


def _banned_targets(p):
    return {str(m[1]) for m in p["avoid"] if m[0] == "summon"}


def _wants_banned(targets, banned):
    return any(t.split("@")[0] in banned for t in targets)


# --- scoring -----------------------------------------------------------------

def parts(key):
    """A score key by name. Keys stored before 2026-11-01 had no leading
    `avoided` term, and before 2026-11-02 no `extenders` term;
    `rescore_bests` rewrites them, this reads any of them."""
    if key is None:
        return None
    if len(key) == 4:
        key = [0, *key]
    if len(key) == 5:
        key = [*key[:4], 0, key[4]]
    return {"avoided": -key[0], "on_field": key[1], "interruptions": key[2],
            "negates": key[3], "extenders": key[4], "actions": -key[5]}


def line_key(line, opening, cards=None, start=None, p=None, conn=None):
    """[-avoided, on_field, interruptions, negates, extenders, -actions].

    `avoided` counts the moves the player asked to avoid that the line
    makes (needs `start` and `conn` to replay it); it comes first, so any
    line that avoids them outranks any that does not. `p` is `prefs()`,
    whose interrupters also count -- on the field -- as interruptions.

    `on_field` and `interruptions` add up what each piece is WORTH where
    it ended -- field, GY or hand: 1, unless the player set a value for
    the card there (`card_values`) -- so a Desirae can outrank an
    Evolzar. A value also counts a card the scripts do not read there (a
    Tearlament in the GY). `negates` stays a plain count.

    `extenders` counts the player's extenders the line summoned or
    activated. A tiebreak, below the board: the same board THROUGH the
    bridge cards is the line with the follow-up, but no line is ever
    preferred to a bigger board for summoning something on the way.

    On-field first, as `drill.lines_from` ranks: interruptions held in
    hand are real but they are not a line you can practise -- you hold
    them whatever you drew. Fewer actions breaks ties: the same board in
    fewer steps is the better thing to learn.

    `interruptions` and `negates` count a card in hand ONLY if the combo
    put it there. A hand trap you opened with and never touched is the
    same card whatever line you play, so counting it rated "hold Ash and
    do nothing" as a board -- and ranked the hands with no engine, which
    are the hands full of hand traps, above the ones that combo. Every
    copy still held from `opening` is left out; a copy beyond what you
    opened with was searched or added, so it counts. A copy played and
    then returned to hand is indistinguishable from one never played,
    and is left out too: the cautious reading. Graveyard pieces count --
    the combo is what sent them there.
    """
    from collections import Counter

    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import board_interruptions

    from .drill import _on_field, dead_on_field

    cards = cards or default_cards()
    p = {"avoid": set(), "interrupters": set(), "extenders": set(), "values": {},
         **(p or {})}
    vals = p["values"]

    def worth(code, zone="field"):
        v = vals.get((code, zone))
        return vals.get((cards.canonical(code), zone), 1) if v is None else v

    board = line.get("board") or {}
    score = board_interruptions(board, cards)
    end_hand = Counter(((board.get("players") or [{}])[0]).get("hand") or [])
    held = Counter(opening) & end_hand          # copies never part of the line
    dead = dead_on_field(board, cards)          # a bare Desirae negates nothing
    n = neg = 0
    for code, where, negate in score.pieces:
        if where == "field" and cards.canonical(code) in dead:
            continue
        if where == "hand" and held[code] > 0:
            held[code] -= 1
            continue
        n += worth(code, _value_zone(where))
        neg += int(bool(negate))
    field_marked = set(p["interrupters"]) | {c for (c, z), v in vals.items() if z == "field"}
    extra = _extra_on_field(board, field_marked, cards, worth)
    other = _extra_off_field(board, vals, cards, held, worth)
    avoided = used = 0
    if ((p["avoid"] or p["extenders"]) and start is not None
            and line.get("responses")):
        from .playback import line_moves
        moves = line_moves(conn, start, line["responses"])
        avoided = len(moves & p["avoid"])
        used = len({m[1] for m in moves} & p["extenders"])
    return [-avoided, _num(_on_field(line, worth if vals else None) + extra),
            _num(n + extra + other), neg, used, -len(line.get("actions") or [])]


def _extra_on_field(board, codes, cards, worth=None):
    """The player's own interrupters face-up on the field that upstream's
    script reading did not already count (`worth(code)` each, default 1)."""
    from ygorl.solver.blocking import card_interruptions

    if not codes:
        return 0
    me = (board.get("players") or [{}])[0]
    n = 0
    for c in (*(me.get("mzone") or []), *(me.get("szone") or [])):
        if (c["code"] in codes and c.get("position", 0) & 0x5
                and not any(i.where == "field"
                            for i in card_interruptions(c["code"], cards))):
            n += worth(c["code"]) if worth else 1
    return n


def _extra_off_field(board, vals, cards, held, worth):
    """Cards the player valued in the GY or hand where the scripts read no
    interruption: each copy there counts its value. A hand copy held since
    the opening is left out, as for every hand piece (`held` is what is
    left of that count after the scripts' own hand pieces took theirs)."""
    from ygorl.solver.blocking import card_interruptions

    me = (board.get("players") or [{}])[0]
    n = 0
    for zone, key in (("grave", "grave"), ("hand", "hand")):
        for code in me.get(key) or []:
            if (code, zone) not in vals and (cards.canonical(code), zone) not in vals:
                continue
            if any(_value_zone(i.where) == zone for i in card_interruptions(code, cards)):
                continue        # already a scripts' piece
            if zone == "hand" and held[code] > 0:
                held[code] -= 1
                continue
            n += worth(code, zone)
    return n


def _compact(record, line):
    """The smallest record `playback` can replay: start + the one line."""
    keep = {k: line[k] for k in ("responses", "actions", "board") if k in line}
    return {"start": record.get("start") or {}, "lines": [keep]}


# --- the pool ----------------------------------------------------------------

def ensure_pool(conn, deck_path, name, hands=HANDS):
    """The pool's hands, created on first use. Same seeds as `sweep`,
    so a deck's stored sweep results are the trainer's starting point."""
    from ygorl.cards.ydk import load_ydk
    from ygorl.solver.batch import hand_seed, sample_hand

    dh = deck_hash(deck_path)
    deck = load_ydk(Path(deck_path))
    with conn:
        for i in range(hands):
            hs = hand_seed(0, name, i)
            hand, _ = sample_hand(deck, hs)
            conn.execute(
                "INSERT OR IGNORE INTO trainer_hands "
                "(deck_hash, deck, hand_index, hand_seed, hand) "
                "VALUES (?,?,?,?,?)", (dh, name, i, str(hs), json.dumps(list(hand))))
    return dh


# --- starter test: what k cards make on their own ------------------------------
#
# The same trainer over a different pool: every k-card hand the deck can
# hold (k = 1..3), with the rest of the deck behind it as usual. A pool of
# its own (`starter_key`), so it never mixes with the training hands. A hand
# that makes nothing in its first generation gets no further tries (`plan`
# has nothing to retry), so the dead hands cost one generation and the rest
# of the time goes to the hands that do something.

STARTER_SIZES = (1, 2, 3)
STARTER_MS = 60_000


def starter_key(dh, k):
    return f"{dh}:starters{k}"


def starter_hands(deck_path, k, skip=()):
    """[(index, hand)] of every k-card hand the main deck can hold (copies
    respected), indices stable whatever `skip` leaves out, in sorted order."""
    from collections import Counter
    from itertools import combinations_with_replacement

    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards

    cards = default_cards()
    count = Counter(cards.canonical(c) for c in load_ydk(Path(deck_path)).main)
    skip, out = {cards.canonical(c) for c in skip}, []
    for i, hand in enumerate(h for h in combinations_with_replacement(sorted(count), k)
                             if all(n <= count[c] for c, n in Counter(h).items())):
        if not skip & set(hand):
            out.append((i, list(hand)))
    return out


def starter_skip(deck_path, with_handtraps=False):
    """The cards a starter test leaves out by default: the deck's hand traps,
    which do nothing on your own turn."""
    if with_handtraps:
        return set()
    from ygorl.cards.ydk import load_ydk
    return set(scenarios.HANDTRAPS) & set(load_ydk(Path(deck_path)).main)


def ensure_starter_pool(conn, deck_path, name, k, skip=()):
    from ygorl.solver.batch import hand_seed

    dh = starter_key(deck_hash(deck_path), k)
    with conn:
        for i, hand in starter_hands(deck_path, k, skip):
            conn.execute(
                "INSERT OR IGNORE INTO trainer_hands "
                "(deck_hash, deck, hand_index, hand_seed, hand) VALUES (?,?,?,?,?)",
                (dh, name, i, str(hand_seed(0, f"{name}:starters{k}", i)),
                 json.dumps(hand)))
    return dh


def starter_estimate(deck_path, k, skip=(), solve_ms=STARTER_MS, workers=None):
    """(hands, hours) of the first generation: every hand asks for every
    piece once (about `breadth` goals each)."""
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import blocking_plan

    deck = load_ydk(Path(deck_path))
    goals = len(blocking_plan(deck.main, deck.extra, [], default_cards()).pieces) + 1
    n = len(starter_hands(deck_path, k, skip))
    return n, n * goals * solve_ms / 1000 / 3600 / (workers or engine.DEFAULT_WORKERS)


def _set_best(conn, dh, hand_index, key, goal, compact, gen, source):
    conn.execute(
        "UPDATE trainer_hands SET best_key=?, best_goal=?, best_record=?, "
        "best_gen=?, best_source=?, best_at=? "
        "WHERE deck_hash=? AND hand_index=?",
        (json.dumps(key), json.dumps(list(goal)), json.dumps(compact), gen,
         source, _now(), dh, hand_index))


def bootstrap(conn, dh, name, deck_path, say=print):
    """Seed personal bests from lines already solved for THIS deck.

    Only records whose deck holds exactly the same cards and whose hand
    seed is in the pool -- a record from an older version of the list is
    another deck's answer.
    """
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards

    deck = load_ydk(Path(deck_path))
    want = (sorted(deck.main), sorted(deck.extra))
    pool = {int(r["hand_seed"]): r for r in conn.execute(
        "SELECT * FROM trainer_hands WHERE deck_hash=?", (dh,))}
    cards, p = default_cards(), prefs(conn, name)
    taken = 0
    for path in sorted((engine.ROOT / "results").glob("*.jsonl")):
        if path.name.startswith("optimize_"):
            continue        # candidate decks, never this one
        for raw in path.read_text().splitlines():
            if not raw.strip() or f'"{name}"' not in raw:
                continue
            rec = json.loads(raw)
            d = rec.get("deck")
            if (not isinstance(d, dict) or d.get("name") != name
                    or rec.get("status") != "solved" or not rec.get("lines")
                    or (sorted(d.get("main") or []), sorted(d.get("extra") or [])) != want):
                continue
            row = pool.get(rec.get("hand_seed"))
            if row is None:
                continue
            best = max(((line_key(ln, json.loads(row["hand"]), cards,
                                  rec.get("start"), p, conn), ln)
                        for ln in rec["lines"]),
                       key=lambda t: t[0])
            have = json.loads(row["best_key"]) if row["best_key"] else None
            if have is None or best[0] > have:
                with conn:
                    _set_best(conn, dh, row["hand_index"], best[0],
                              rec.get("targets") or [], _compact(rec, best[1]),
                              0, path.name)
                pool[rec["hand_seed"]] = conn.execute(
                    "SELECT * FROM trainer_hands WHERE deck_hash=? AND hand_index=?",
                    (dh, row["hand_index"])).fetchone()
                taken += 1
    say(f"bootstrap: {taken} personal best(s) taken from stored results")
    return taken


def rescore_bests(conn, dh, name, say=print):
    """Re-score every stored best with the current `line_key`.

    Idempotent, and run at the start of every training session, so a
    change to the scoring can never leave old keys being compared with
    new ones. Only the best line per hand is stored, so this re-ranks
    the bests -- it cannot discover that an earlier, discarded line now
    outranks one.
    """
    from ygorl.engine.duel import default_cards

    cards, changed, p = default_cards(), 0, prefs(conn, name)
    rows = conn.execute("SELECT * FROM trainer_hands WHERE deck_hash=? "
                        "AND best_record IS NOT NULL", (dh,)).fetchall()
    with conn:
        for r in rows:
            rec = json.loads(r["best_record"])
            key = line_key(rec["lines"][0], json.loads(r["hand"]), cards,
                           rec.get("start"), p, conn)
            if key != json.loads(r["best_key"]):
                conn.execute("UPDATE trainer_hands SET best_key=? WHERE "
                             "deck_hash=? AND hand_index=?",
                             (json.dumps(key), dh, r["hand_index"]))
                changed += 1
    if changed:
        say(f"rescored {changed} stored best(s) with the current scoring")
    return changed


# --- choosing where to aim -----------------------------------------------------

def _attempts(conn, dh, hand_index):
    return [{"label": r["label"], "targets": json.loads(r["targets"]),
             "status": r["status"], "best_placed": r["best_placed"],
             "key": [r["on_field"], r["interruptions"], r["negates"]]}
            for r in conn.execute(
                "SELECT * FROM trainer_attempts WHERE deck_hash=? AND hand_index=? "
                "ORDER BY rowid", (dh, hand_index))]


def plan(attempts, breadth, best_goal, per_hand=PER_HAND, banned=(),
         extenders=()):
    """[(targets, label, draw)] for one hand's next generation.

    Pure: history in, goals out, so the scheduling is testable without
    an hour of solver. `breadth` is the hand's piece goals (computed by
    the caller from the deck): all of them while the hand has no history,
    and afterwards any never asked -- an interrupter the player adds later
    is asked for first. `banned` are target cards never to ask for (a
    summon the player said to avoid). `extenders` are the player's bridge
    cards: once the hand has a best line, its best board THROUGH each one
    is asked for, again whenever the best board grows.
    """
    def draws(targets):
        k = frozenset(targets)
        return sum(1 for a in attempts if frozenset(a["targets"]) == k)

    banned = set(banned)
    breadth = [(t, lbl) for t, lbl in breadth if not _wants_banned(t, banned)]
    if not attempts:
        return [(list(t), lbl, 0) for t, lbl in breadth]

    out, seen = [], set()

    first_label = {}
    for a in attempts:
        first_label.setdefault(frozenset(a["targets"]), a["label"])

    def add(targets, label):
        k = frozenset(targets)
        if k in seen or len(out) >= per_hand or _wants_banned(targets, banned):
            return
        seen.add(k)
        # A goal tried before keeps its FIRST label. `combinations_to_try`
        # finds the solved pieces by their "piece:" label, so a piece that
        # only solves on a retry would otherwise never be stacked.
        out.append((list(targets), first_label.get(k, label), draws(targets)))

    # 1. pieces never asked (an interrupter added since)
    tried = {frozenset(a["targets"]) for a in attempts}
    for targets, label in breadth:
        if frozenset(targets) not in tried:
            add(targets, label)

    # 1b. the best board through each extender, never asked on this board
    if best_goal:
        board, _ = split_goal(best_goal)
        for c in extenders:
            targets = [*board, f"{EXT}{c}"]
            if frozenset(targets) not in tried:
                add(targets, f"{EXT}{c}")

    # 2. stacks nobody has asked this hand for
    for targets, label in sweep.combinations_to_try(attempts, max_new=per_hand):
        add(targets, label)

    # 3. near misses, biggest goal first, least-retried first
    misses = {}
    for a in attempts:
        if a["status"] == "solved":
            continue
        placed = a.get("best_placed")
        if isinstance(placed, int) and placed > 0:
            k = frozenset(a["targets"])
            if k not in misses or placed > misses[k][1]:
                misses[k] = (a, placed)
    solved = {frozenset(a["targets"]) for a in attempts if a["status"] == "solved"}
    for a, placed in sorted(misses.values(),
                            key=lambda t: (-len(t[0]["targets"]), -t[1],
                                           draws(t[0]["targets"]))):
        if frozenset(a["targets"]) not in solved:
            add(a["targets"], a["label"])

    # 4. the best goal again, then the biggest solved goals
    if best_goal:
        add(best_goal, "best")   # labelled "best" only if never tried by name
    for a in sorted((a for a in attempts if a["status"] == "solved"),
                    key=lambda a: (-len(a["targets"]), draws(a["targets"]))):
        add(a["targets"], "redraw")
    return out


# --- one generation ------------------------------------------------------------

def breadth_goals(deck_path, hand, p):
    """(base, [(targets, label)]): base + EVERY interrupter piece, plus the
    player's own interrupters. Not capped as `sweep` caps them: the first
    deck trained had six recognised pieces and the trainer only ever
    asked for four."""
    from ygorl.cards.ydk import load_ydk
    from ygorl.engine.duel import default_cards
    from ygorl.solver.blocking import blocking_plan

    deck = load_ydk(Path(deck_path))
    bp = blocking_plan(deck.main, deck.extra, list(hand), default_cards())
    codes = [int(x.password) for x in bp.pieces]
    codes += sorted(c for c in p["interrupters"] if c not in codes)
    goals = [([*bp.base, str(c)], f"piece:{c}") for c in codes]
    if bp.base:
        goals.append((list(bp.base), "base"))
    return list(bp.base), goals


def _budget(targets, base, solve_ms):
    # An `ext:` requirement counts as a piece: it asks for more line.
    from collections import Counter
    pieces = sum((Counter(targets) - Counter(base)).values())
    return solve_ms * BIG_STACK_X if pieces >= BIG_STACK else solve_ms


def _record_gen(conn, dh, name, gen, started, solves, improved, solve_ms):
    rows = conn.execute("SELECT best_key FROM trainer_hands WHERE deck_hash=?",
                        (dh,)).fetchall()
    keys = [parts(json.loads(r["best_key"])) if r["best_key"] else None
            for r in rows]
    solved = [k for k in keys if k]
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO trainer_gens (deck_hash, deck, gen, started, "
            "finished, solves, improved, pool, solved_hands, mean_on_field, "
            "mean_interruptions, solve_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            # Unsolved hands count as ZERO, not as missing: a pool mean
            # over solved hands only rises when a hand stops solving.
            (dh, name, gen, started, _now(), solves, improved, len(rows),
             len(solved), sum(k["on_field"] for k in solved) / max(1, len(rows)),
             sum(k["interruptions"] for k in solved) / max(1, len(rows)), solve_ms))


def run_generation(conn, deck_path, name, dh, gen, per_hand=PER_HAND,
                   solve_ms=None, workers=None, say=print):
    from ygorl.engine.duel import default_cards

    solve_ms = solve_ms or TRAIN_MS
    workers = workers or engine.DEFAULT_WORKERS
    started = _now()
    hands = conn.execute(
        "SELECT * FROM trainer_hands WHERE deck_hash=? ORDER BY hand_index",
        (dh,)).fetchall()
    p = prefs(conn, name)
    extra = tuple(solver_args(p))
    banned = _banned_targets(p)
    cards = default_cards()
    batch, goals = [], []
    for h in hands:
        hand = json.loads(h["hand"])
        hist = _attempts(conn, dh, h["hand_index"])
        base, breadth = breadth_goals(deck_path, hand, p)
        best_goal = json.loads(h["best_goal"]) if h["best_goal"] else None
        for targets, label, draw in plan(hist, breadth, best_goal, per_hand,
                                         banned, sorted(p["extenders"])):
            board, need = split_goal(targets)
            batch.append((str(deck_path), name, h["hand_index"], int(h["hand_seed"]),
                          hand, board, label, _budget(targets, base, solve_ms),
                          draw, extra + tuple(extender_args(p, need, cards))))
            goals.append(targets)
    big = sum(1 for b in batch if b[7] > solve_ms)
    say(f"generation {gen}: {len(batch)} solve(s) over {len(hands)} hand(s), "
        f"{solve_ms // 1000}s each ({big} big stack(s) at "
        f"{solve_ms * BIG_STACK_X // 1000}s) on {workers} worker(s)"
        + (f"; avoiding {len(p['avoid'])} move(s)" if p["avoid"] else "")
        + (f"; {len(p['extenders'])} extender(s)" if p["extenders"] else ""))

    best = {h["hand_index"]: (json.loads(h["best_key"]) if h["best_key"] else None)
            for h in hands}
    opening = {h["hand_index"]: json.loads(h["hand"]) for h in hands}
    improved = done = 0
    for res in sweep._map_solves(batch, workers):
        a, i = res["attempt"], res["hand_index"]
        # The solver saw the board targets only; the goal is what was asked,
        # `ext:` requirements included (results arrive in batch order).
        a["targets"] = goals[done]
        done += 1
        key, line = None, None
        if res.get("record"):
            # Every line the solver wrote, not just its own pick: it ranks
            # by upstream's count, which knows neither the player's
            # interrupters nor the moves to avoid.
            rec = res["record"]
            key, line = max(((line_key(ln, opening[i], cards, rec.get("start"),
                                       p, conn), ln) for ln in rec["lines"]),
                            key=lambda t: t[0])
        better = key is not None and (best[i] is None or key > best[i])
        with conn:
            conn.execute(
                "INSERT INTO trainer_attempts (deck_hash, hand_index, gen, targets, "
                "label, draw, solver_seed, status, wall_s, best_placed, on_field, "
                "interruptions, negates, improved, at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dh, i, gen, json.dumps(a["targets"]), a["label"], a["draw"],
                 a.get("solver_seed"), a["status"], a.get("wall_s"),
                 a.get("best_placed"), key[1] if key else None,
                 key[2] if key else None, key[3] if key else None,
                 int(better), _now()))
            if better:
                _set_best(conn, dh, i, key, a["targets"],
                          _compact(res["record"], line), gen, "trainer")
        if better:
            improved += 1
            was = f"{parts(best[i])['on_field']} on field" if best[i] else "no line"
            k = parts(key)
            say(f"  hand {i}: NEW BEST {k['on_field']} on field, {k['interruptions']} "
                f"interruption(s){', uses an avoided move' if k['avoided'] else ''} "
                f"(was {was}) via {a['label']}")
            best[i] = key
        say(f"progress {done}/{len(batch)}  hand {i} {a['label']}: {a['status']}")
    _record_gen(conn, dh, name, gen, started, len(batch), improved, solve_ms)
    g = curve(conn, dh)[-1]
    say(f"generation {gen} done: {improved} improvement(s); pool mean "
        f"{g['mean_on_field']:.2f} on field, {g['solved_hands']}/{g['pool']} solved")
    return improved


def plateaued(conn, dh, n):
    """True when the last `n` finished generations improved no hand."""
    gens = [g for g in curve(conn, dh) if g["gen"] > 0]
    return len(gens) >= n and all(not g["improved"] for g in gens[-n:])


def train(conn, deck_path, name, generations=None, hours=None, hands=HANDS,
          per_hand=PER_HAND, solve_ms=None, workers=None, say=print,
          plateau=None, starters=None, skip=()):
    """Run generations until `generations` are done, `hours` pass, a stop
    is requested, or -- with `plateau` -- that many generations in a row
    improved no hand ("as good as it'll get", STAGE8). Resumable: every
    attempt is committed as it lands, and a killed generation is simply
    started again under a new number. `starters=k`: the starter test's
    pool of k-card hands (less `skip`) instead of the training hands."""
    import time

    engine.require()
    engine.preflight([Path(deck_path)])
    dh = (ensure_starter_pool(conn, deck_path, name, starters, skip) if starters
          else ensure_pool(conn, deck_path, name, hands))
    rescore_bests(conn, dh, name, say)
    clear_stop(name)
    with conn:   # an interrupted generation never finished: drop its row
        conn.execute("DELETE FROM trainer_gens WHERE deck_hash=? AND finished IS NULL",
                     (dh,))
    if not curve(conn, dh):
        if not starters:    # stored results are 5-card hands
            bootstrap(conn, dh, name, deck_path, say)
        _record_gen(conn, dh, name, 0, _now(), 0, 0, solve_ms or TRAIN_MS)
        say("generation 0 = the starting point (stored results only)")
    deadline = time.monotonic() + hours * 3600 if hours else None
    ran = 0
    while True:
        if generations is not None and ran >= generations:
            break
        if deadline is not None and time.monotonic() >= deadline:
            say("time limit reached")
            break
        if stop_requested(name):
            say("stop requested -- ending after the last full generation")
            break
        if plateau and plateaued(conn, dh, plateau):
            say(f"plateau: {plateau} generation(s) in a row improved no hand")
            break
        gen = curve(conn, dh)[-1]["gen"] + 1
        run_generation(conn, deck_path, name, dh, gen, per_hand, solve_ms,
                       workers, say)
        ran += 1
    clear_stop(name)
    return ran


# --- reading it back -----------------------------------------------------------

def curve(conn, dh):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM trainer_gens WHERE deck_hash=? AND finished IS NOT NULL "
        "ORDER BY gen", (dh,))]


def rank(h):
    """Sort key for a hands_table row, best line first: the trainer's own
    line_key order, unsolved hands last."""
    k = h["key"]
    if not k:
        return (1,)
    return (0, k["avoided"], -k["on_field"], -k["interruptions"],
            -k["negates"], -k["extenders"], k["actions"])


def best_hand(conn, dh):
    """The hands_table row holding the best watchable line, or None."""
    rows = [h for h in hands_table(conn, dh) if h["watchable"]]
    return min(rows, key=rank) if rows else None


def hands_table(conn, dh):
    from . import card_db
    out = []
    for h in conn.execute(
            "SELECT h.*, (SELECT COUNT(*) FROM trainer_attempts a WHERE "
            "a.deck_hash=h.deck_hash AND a.hand_index=h.hand_index) AS tries "
            "FROM trainer_hands h WHERE deck_hash=? ORDER BY hand_index", (dh,)):
        key = parts(json.loads(h["best_key"])) if h["best_key"] else None
        names = [(card_db.resolve(conn, c) or {"name": str(c)})["name"]
                 for c in json.loads(h["hand"])]
        out.append({"i": h["hand_index"], "hand": names, "key": key,
                    "codes": [int(c) for c in json.loads(h["hand"])],
                    "on_field": key["on_field"] if key else None,
                    "interruptions": key["interruptions"] if key else None,
                    "actions": key["actions"] if key else None,
                    "avoided": key["avoided"] if key else 0,
                    "extenders": key["extenders"] if key else 0,
                    "gen": h["best_gen"], "source": h["best_source"],
                    "tries": h["tries"], "watchable": bool(h["best_record"])})
    return out


def best_record(conn, dh, hand_index):
    row = conn.execute(
        "SELECT best_record FROM trainer_hands WHERE deck_hash=? AND hand_index=?",
        (dh, hand_index)).fetchone()
    return json.loads(row["best_record"]) if row and row["best_record"] else None
