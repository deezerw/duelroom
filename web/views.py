"""Pages. Stage 1: deck list, deck detail, import."""

from pathlib import Path

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for)

import json

from skills import (card_db, config, db, drill, gate, importer,
                    meta_scrape as ms, report as report_mod, scenarios, stats)
from skills.deck import Deck

from . import jobs
from .auth import login_required

bp = Blueprint("views", __name__)
ROOT = Path(__file__).resolve().parent.parent
DECKS = ROOT / "assets" / "decks"
ROOT_RESULTS = ROOT / "results"


def conn():
    return db.connect()


@bp.route("/")
@login_required
def index():
    c = conn()
    active = db.get_meta(c, "active_deck")
    rows = []
    for path in sorted(DECKS.glob("*.ydk")):
        deck = Deck.from_ydk(path)
        deck.canonicalise(c)
        guesses = ms.identify_archetype(c, deck)
        rows.append({
            "name": path.stem,
            "main": len(deck.main),
            "extra": len(deck.extra),
            "side": len(deck.side),
            "archetype": guesses[0][0] if guesses and guesses[0][1] >= 0.5 else None,
            "legal": not deck.validate(c),
            "active": path.stem == active,
        })
    return render_template("index.html", decks=rows, active=active)


@bp.route("/deck/<name>")
@login_required
def deck(name):
    path = DECKS / f"{name}.ydk"
    if not path.exists():
        flash(f"No deck named {name}")
        return redirect(url_for("views.index"))
    c = conn()
    d = Deck.from_ydk(path)
    unresolved = d.canonicalise(c)
    guesses = ms.identify_archetype(c, d)

    sections = {}
    for sec in ("main", "extra", "side"):
        sections[sec] = [
            {"name": n, "copies": k,
             "id": next((cid for cid in set(getattr(d, sec))
                         if (card_db.resolve(c, cid) or {})["name"] == n), None)}
            for n, k in d.named(c, sec)
        ]
    return render_template(
        "deck.html", deck=d, name=name, sections=sections,
        problems=d.validate(c), unresolved=unresolved,
        archetype=guesses[0] if guesses and guesses[0][1] >= 0.5 else None,
        alts=guesses[1:3] if guesses else [],
        ydke=importer.to_ydke(d),
        running=jobs.active(c),
        runnable=[(k, v[1], v[2]) for k, v in jobs.KINDS.items() if k != "meta"],
    )


@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_deck():
    if request.method != "POST":
        return render_template("import.html")

    c = conn()
    source = (request.form.get("source") or "").strip()
    upload = request.files.get("file")
    name = (request.form.get("name") or "").strip() or None

    try:
        if upload and upload.filename:
            tmp = Path("/tmp") / f"duelroom-upload-{upload.filename[:60]}"
            upload.save(tmp)
            d = importer.load(tmp, name)
            tmp.unlink(missing_ok=True)
        elif source:
            d = importer.load(source, name)
        else:
            flash("Paste a ydke:// URI or a ygoprodeck URL, or choose a .ydk file.")
            return render_template("import.html"), 400
    except Exception as e:
        flash(f"Import failed: {e}")
        return render_template("import.html"), 400

    d.canonicalise(c)
    path = importer.save(d, name)

    # Same preflight the CLI runs: a card the solver cannot load
    # segfaults it, and finding that out at import time beats finding
    # out hours into a batch.
    from skills import carddb
    blockers = carddb.check_deck(d)
    if blockers:
        flash(f"Imported, but {len(blockers)} card(s) cannot be loaded by the "
              f"solver — run `daemon.py setup` to rebuild the merged card database.")

    db.set_meta(c, "active_deck", d.name)
    c.commit()
    flash(f"Imported {d.name} ({len(d.main)}/{len(d.extra)}/{len(d.side)})")
    return redirect(url_for("views.deck", name=path.stem))


def _latest_matrix():
    files = sorted((ROOT_RESULTS).glob("handtrap_matrix*.jsonl"),
                   key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _matrix_records():
    path = _latest_matrix()
    if not path:
        return [], None, 0
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return recs, path, len(recs)


@bp.route("/report")
@bp.route("/report/<name>")
@login_required
def report(name=None):
    c = conn()
    name = name or db.get_meta(c, "active_deck")
    if not name or not (DECKS / f"{name}.ydk").exists():
        flash("No deck selected — import one or pick one from the deck list.")
        return redirect(url_for("views.index"))
    r = report_mod.deck_report(c, DECKS / f"{name}.ydk")

    surv = []
    for code, s_ in (r["funnel"] or {}).get("survival_given_line", {}).items():
        row = card_db.resolve(c, int(code))
        surv.append({
            "code": int(code),
            "name": row["name"] if row else code,
            "value": s_,
            "blind": int(code) in scenarios.BLIND_SPOTS,
            "reason": scenarios.BLIND_SPOTS.get(int(code)),
        })
    surv.sort(key=lambda x: (x["value"] is None, x["value"] or 0))

    cost = None
    cache = ROOT_RESULTS / "special_summons.json"
    if cache.exists():
        cost = json.loads(cache.read_text()).get(name)

    f = r["funnel"] or {}
    ci = stats.wilson(f.get("solved") or 0, f.get("hands") or 0) if f else None
    return render_template("report.html", r=r, name=name, surv=surv, cost=cost,
                           ci=ci, running=jobs.active(c))


@bp.route("/matrix")
@login_required
def matrix():
    c = conn()
    recs, path, n = _matrix_records()
    if not recs:
        flash("No handtrap matrix yet — run one from a deck page.")
        return redirect(url_for("views.index"))
    m = scenarios.handtrap_matrix(recs)
    w = config.weights()
    ranking = scenarios.weighted_handtrap_ranking(m, w)
    covered, missing = config.coverage(m.keys())
    codes = sorted(scenarios.HANDTRAPS)
    mine = db.get_meta(c, "active_deck")
    hands = min((r.get("summary", {}).get("hands") or 0) for r in recs)
    # Say so when the sweep does not include your deck. Otherwise the
    # table reads as "here is your matchup picture" while containing
    # nothing about your deck at all.
    mine_present = bool(mine) and any(
        config.slug(d) == config.slug(mine) for d in m)
    return render_template(
        "matrix.html", matrix=m, codes=codes, names=scenarios.HANDTRAPS,
        mine_present=mine_present,
        ranking=ranking, covered=covered, missing=missing, mine=mine,
        slug=config.slug, source=path.name, n_decks=n, hands=hands,
        blind=scenarios.BLIND_SPOTS, running=jobs.active(c))


@bp.route("/threats")
@login_required
def threats():
    c = conn()
    prof = gate.threat_profile(c)
    if not prof:
        flash("No board library yet.")
        return redirect(url_for("views.index"))
    cache = ROOT_RESULTS / "special_summons.json"
    ss = json.loads(cache.read_text()) if cache.exists() else {}
    counts = sorted({n for d in prof.values() for n in d})
    # Shaped here rather than in the template: a weighted mean is not
    # something Jinja should be asked to express.
    rows = sorted(
        ({"archetype": a,
          "dist": d,
          "mean": sum(k * v for k, v in d.items())} for a, d in prof.items()),
        key=lambda r: -r["mean"])
    bvh = sorted(
        ({"archetype": a, **v} for a, v in gate.board_vs_hand(c).items()),
        key=lambda r: -r["field"])
    summons = sorted(
        ({"archetype": a, **v} for a, v in ss.items()),
        key=lambda r: -r["mean"])
    return render_template(
        "threats.html", rows=rows, counts=counts, board_vs_hand=bvh,
        pieces=sorted(gate.common_pieces(c).items()), summons=summons,
        mine=db.get_meta(c, "active_deck"), slug=config.slug,
        running=jobs.active(c))


@bp.route("/drill")
@bp.route("/drill/<name>")
@login_required
def drill_view(name=None):
    c = conn()
    name = name or db.get_meta(c, "active_deck")
    if not name:
        flash("No deck selected.")
        return redirect(url_for("views.index"))
    lines, src = [], None
    for cand in sorted(ROOT_RESULTS.glob("*.jsonl"),
                       key=lambda p: -p.stat().st_mtime):
        found = drill.lines_from(cand, name)
        if found:
            src = cand.name
            for rec, line, n in found[:12]:
                lines.append({
                    "interrupts": n,
                    "actions": len(line.get("actions") or []),
                    "hand": [card_db.resolve(c, x)["name"] if card_db.resolve(c, x)
                             else str(x) for x in (rec.get("hand") or [])],
                    "board": drill.describe(c, line),
                })
            break
    return render_template("drill.html", name=name, lines=lines, source=src,
                           running=jobs.active(c))


@bp.route("/jobs")
@login_required
def job_list():
    c = conn()
    return render_template("jobs.html", running=jobs.active(c),
                           rows=jobs.recent(c), kinds=jobs.KINDS)


@bp.route("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    c = conn()
    job = jobs.get(c, job_id)
    if not job:
        flash("No such job")
        return redirect(url_for("views.job_list"))
    return render_template("job.html", job=job, progress=jobs.progress(job),
                           partial=jobs.partial(job), kinds=jobs.KINDS)


@bp.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@login_required
def job_cancel(job_id):
    c = conn()
    if jobs.cancel(c, job_id):
        flash(f"Job {job_id} cancelled")
    else:
        flash("Only queued jobs can be cancelled — killing a running solver "
              "leaves partial records that read as complete.")
    return redirect(url_for("views.job_list"))


@bp.route("/deck/<name>/run/<kind>", methods=["POST"])
@login_required
def run_job(name, kind):
    c = conn()
    if kind not in jobs.KINDS:
        flash("Unknown job type")
        return redirect(url_for("views.deck", name=name))
    hands = request.form.get("hands", type=int) or 12
    job_id = jobs.enqueue(c, kind, deck=name, hands=hands)
    running = jobs.active(c)
    if running and running["id"] != job_id:
        flash(f"Queued as job {job_id} — job {running['id']} "
              f"({running['kind']}) is running first.")
    else:
        flash(f"Queued as job {job_id}.")
    return redirect(url_for("views.job_detail", job_id=job_id))


@bp.route("/deck/<name>/activate", methods=["POST"])
@login_required
def activate(name):
    c = conn()
    if not (DECKS / f"{name}.ydk").exists():
        flash("No such deck")
        return redirect(url_for("views.index"))
    db.set_meta(c, "active_deck", name)
    c.commit()
    return redirect(url_for("views.deck", name=name))


@bp.route("/deck/<name>/ydk")
@login_required
def download_ydk(name):
    path = DECKS / f"{name}.ydk"
    if not path.exists():
        return "not found", 404
    return Response(
        path.read_text(), mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{name}.ydk"'})
