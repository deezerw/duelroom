"""Move a section of stored games to a label of their own ("device"), files too.

When the way games are played changes (the enemy's turn-1 target, a budget),
old and new games must never be pooled into one number. The device column
already keeps sections apart (/games shows one per device), so the old games
are relabelled -- readable, never mixed. Done by hand three times on
2026-11-19 (endgame-t1, brk1x); this is that, once:

  * every game row of the section (game_results, game_used, game_stops)
    gets device=LABEL and its line paths point into the label's folder;
  * each game's run folder moves to `games_dir(deck, enemy, first, LABEL)`;
  * turn 1s stay where they are (new games reuse them when the change does
    not touch turn 1): their rows are COPIED under LABEL and their folders
    copied, so the old games keep the files /watch and impact read;
    `--move-turn1` moves them instead (the change touched turn 1);
  * results/ml/game_kills.jsonl lines of those games follow (device, paths).

    venv/bin/python tools/relabel_games.py <deck> <enemy> first|second <label>
        [--from server] [--move-turn1] [--dry-run]

Back up data/duelroom.db first; the tool refuses if LABEL already holds games.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skills import db, game  # noqa: E402

TABLES = ("game_results", "game_used", "game_stops")
KILLS = ROOT / "results" / "ml" / "game_kills.jsonl"


def relabel(conn, deck, enemy, first, label, src="server", move_turn1=False, dry=False,
            say=print):
    dhs = [r[0] for r in conn.execute(
        "SELECT DISTINCT deck_hash FROM trainer_hands WHERE deck=?", (deck,))]
    if not dhs:
        raise SystemExit(f"no trained deck named {deck}")
    first = int(bool(first))
    if conn.execute(f"SELECT COUNT(*) FROM game_results WHERE enemy=? AND first=? AND device=? "
                    f"AND deck_hash IN ({','.join('?' * len(dhs))})",
                    (enemy, first, label, *dhs)).fetchone()[0]:
        raise SystemExit(f"{label} already holds {deck} vs {enemy} games: pick another label")
    old_dir = game.games_dir(deck, enemy, first, src)
    new_dir = game.games_dir(deck, enemy, first, label)
    old, new = f"{old_dir}/", f"{new_dir}/"
    where = (f"enemy=? AND first=? AND device=? AND deck_hash IN ({','.join('?' * len(dhs))})")
    key = (enemy, first, src, *dhs)
    games = conn.execute(f"SELECT hand_index, enemy_seed, run FROM game_results WHERE {where}",
                         key).fetchall()
    t1 = conn.execute(f"SELECT DISTINCT hand_index, enemy_seed FROM game_turn1 WHERE {where}",
                      key).fetchall()
    say(f"{deck} vs {enemy} going {'first' if first else 'second'}: {len(games)} game(s), "
        f"{len(t1)} turn 1(s), {old_dir} -> {new_dir}")
    if dry:
        return len(games)
    moved = 0
    for h, e, k in games:
        a = old_dir / f"h{h}" / f"e{e}" / f"run{k}"
        if a.exists():
            b = new_dir / f"h{h}" / f"e{e}" / f"run{k}"
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(a), str(b))
            moved += 1
    for h, e in t1:
        a = old_dir / f"h{h}" / f"e{e}" / "t1"
        if a.exists():
            b = new_dir / f"h{h}" / f"e{e}" / "t1"
            b.parent.mkdir(parents=True, exist_ok=True)
            (shutil.move if move_turn1 else
             lambda x, y: shutil.copytree(x, y, dirs_exist_ok=True))(str(a), str(b))
    with conn:
        for t in TABLES:
            conn.execute(f"UPDATE {t} SET device=?, line=replace(line, ?, ?) WHERE {where}",
                         (label, old, new, *key))
        cols = [c[1] for c in conn.execute("PRAGMA table_info(game_turn1)")]
        sel = ", ".join("?" if c == "device" else c for c in cols)
        if move_turn1:
            conn.execute(f"UPDATE game_turn1 SET device=?, start=replace(start, ?, ?), "
                         f"prefix=replace(prefix, ?, ?) WHERE {where}",
                         (label, old, new, old, new, *key))
        else:
            conn.execute(f"INSERT OR IGNORE INTO game_turn1 ({', '.join(cols)}) "
                         f"SELECT {sel} FROM game_turn1 WHERE {where}", (label, *key))
            conn.execute("UPDATE game_turn1 SET start=replace(start, ?, ?), "
                         "prefix=replace(prefix, ?, ?) WHERE device=? AND enemy=? AND first=? "
                         f"AND deck_hash IN ({','.join('?' * len(dhs))})",
                         (old, new, old, new, label, enemy, first, *dhs))
    if KILLS.exists():
        lines, n = [], 0
        for raw in KILLS.read_text().splitlines():
            d = json.loads(raw)
            if (d["deck_hash"] in dhs and d["enemy"] == enemy and d["device"] == src
                    and first):
                d["device"] = label
                for k in d.get("kill", []):
                    if k.get("line"):
                        k["line"] = k["line"].replace(old, new)
                n += 1
            lines.append(json.dumps(d))
        if n:
            KILLS.write_text("\n".join(lines) + "\n")
        say(f"  game_kills.jsonl: {n} line(s) follow")
    missing = sum(1 for (ln,) in conn.execute(
        f"SELECT line FROM game_results WHERE device=? AND enemy=? AND first=? AND line IS NOT NULL "
        f"AND deck_hash IN ({','.join('?' * len(dhs))})", (label, enemy, first, *dhs))
        if not Path(ln).exists())
    say(f"  {moved} run folder(s) moved; {missing} line path(s) missing afterwards")
    return len(games)


if __name__ == "__main__":
    a = sys.argv[1:]
    flags = {x for x in a if x.startswith("--")}
    src = a[a.index("--from") + 1] if "--from" in a else "server"
    pos = [x for i, x in enumerate(a) if not x.startswith("--")
           and not (i and a[i - 1] == "--from")]
    if len(pos) != 4 or pos[2] not in ("first", "second"):
        raise SystemExit(__doc__)
    deck, enemy, side, label = pos
    relabel(db.connect(), deck, enemy, side == "first", label, src=src,
            move_turn1="--move-turn1" in flags, dry="--dry-run" in flags)
