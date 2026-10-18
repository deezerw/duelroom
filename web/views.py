"""Pages. Stage 1: deck list, deck detail, import."""

from pathlib import Path

from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for)

from skills import card_db, db, importer, meta_scrape as ms
from skills.deck import Deck

from . import jobs
from .auth import login_required

bp = Blueprint("views", __name__)
DECKS = Path(__file__).resolve().parent.parent / "assets" / "decks"


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
