"""Games kept out of training (user, 2026-11-27: "dont include any games with bad data. if you
find a problem with games then i dont want any of it in the data set").

tools/experiments/play_audit.py records every game with a misplay in `bad_games`; the
trainers drop every line and position of that PAIRING (our deck + hand, enemy hand, device),
not only the flagged run: a kill line in game_kills carries no run number, and the user asked
for none of it."""

from datetime import datetime, timezone


def audit_key(enemy, first, device):
    return f"audit:{enemy}:{int(bool(first))}:{device}"


def record(conn, enemy, first, device, flagged, games=None):
    """Store the audit's flagged games [(game_results row, [problem, ...])]."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # the audit covers the whole section: its last verdict replaces the one before
    conn.execute("DELETE FROM bad_games WHERE enemy=? AND first=? AND device=?",
                 (enemy, int(bool(first)), device))
    conn.executemany(
        "INSERT OR REPLACE INTO bad_games VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(g["deck_hash"], enemy, g["hand_index"], g["board"], g["enemy_seed"], int(bool(first)),
          device, g["run"], ",".join(p), now) for g, p in flagged])
    # when this section was last audited, and over how many games: "0 quarantined" and
    # "never audited" must not read the same (tools/night_report.py)
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                 (audit_key(enemy, first, device), f"{now} {games}"))
    conn.commit()


def pairings(conn):
    """{(deck_hash, hand_index, enemy_seed, device)} with any bad game."""
    return {tuple(r) for r in conn.execute(
        "SELECT DISTINCT deck_hash, hand_index, enemy_seed, device FROM bad_games")}


def is_bad(bad, deck_hash, hand, eseed, device):
    return (deck_hash, hand, eseed, device) in bad


FILE = "results/ml/bad_pairings.json"     # what tools/duelpc/train.sh ships to the trainers


def dump(conn, path):
    import json
    from pathlib import Path
    Path(path).write_text(json.dumps(sorted(pairings(conn))))


def load(path=FILE):
    """The shipped pairings as a set; a MISSING file is an error (training on everything
    because the list did not arrive would be exactly the bad data the user ruled out)."""
    import json
    from pathlib import Path
    return {tuple(p) for p in json.loads(Path(path).read_text())}


def demo_bad(bad, raw):
    s = raw.get("solver") or {}
    return (s.get("deck_hash"), s.get("hand"), s.get("eseed"), s.get("device")) in bad


def position_bad(bad, row):
    dh, _, hand = (row.get("group") or "").rpartition(":")
    return row.get("source") == "games" and (dh, int(hand) if hand.isdigit() else hand,
                                             row.get("eseed"), row.get("device")) in bad
