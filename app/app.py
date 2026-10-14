#!/usr/bin/env python3
"""Vault — Flask app (catalog + admin).

Serves derivatives + masters from /Volumes/Vault/archive.
"""
from __future__ import annotations

import math
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import common  # noqa: E402

from flask import (  # noqa: E402
    Flask, abort, flash, g, redirect, render_template,
    request, send_file, send_from_directory, url_for, jsonify,
)

APP_ROOT = Path(__file__).parent
ARCHIVE = common.ARCHIVE_ROOT

PER_PAGE = 48

app = Flask(__name__, template_folder=str(APP_ROOT / "templates"),
            static_folder=str(APP_ROOT / "static"))
app.secret_key = os.environ.get("VAULT_SECRET_KEY", "change-me-in-prod")

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # racine repo
import vault_config  # noqa: E402
SITE_NAME = vault_config.load_config().site_name

@app.context_processor
def _inject_site():
    return {"site_name": SITE_NAME}


# ---------- DB ----------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        con = sqlite3.connect(common.DB_PATH)
        con.row_factory = sqlite3.Row
        g.db = con
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------- context ----------
@app.context_processor
def inject_globals():
    return {
        "archive_rel": lambda p: url_for("media", path=p),
    }


@app.template_filter("human_size")
def human_size(n):
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@app.template_filter("dur")
def dur(seconds):
    if not seconds:
        return "—"
    s = int(seconds)
    return f"{s//60}:{s%60:02d}"


# ---------- media (derivatives) ----------
@app.route("/media/<path:path>")
def media(path: str):
    """Serves derivatives (thumbs, previews) from /Volumes/Vault/archive."""
    full = (ARCHIVE / path).resolve()
    if common.ARCHIVE_ROOT not in full.parents and full != common.ARCHIVE_ROOT:
        abort(403)
    if not full.exists() or full.is_dir():
        abort(404)
    # never serve masters directly via this endpoint
    if "masters/" in path:
        abort(403)
    return send_from_directory(ARCHIVE, path, max_age=3600)


# ---------- public routes ----------
@app.route("/")
def home():
    db = get_db()
    _bl = "id NOT IN (SELECT item_id FROM blocklist)"
    stats = {
        "total":    db.execute(f"SELECT COUNT(*) FROM items WHERE {_bl}").fetchone()[0],
        "photos":   db.execute(f"SELECT COUNT(*) FROM items WHERE item_type IN ('scan','photo') AND {_bl}").fetchone()[0],
        "hardware": db.execute(f"SELECT COUNT(*) FROM items WHERE item_type='visual' AND file_path_master LIKE 'masters/hardware/%' AND {_bl}").fetchone()[0],
        "videos":   db.execute(f"SELECT COUNT(*) FROM items WHERE item_type='video' AND {_bl}").fetchone()[0],
    }
    years = db.execute(
        f"SELECT COALESCE(year, circa_start) AS y, COUNT(*) AS n FROM items "
        f"WHERE y IS NOT NULL AND {_bl} GROUP BY y ORDER BY y"
    ).fetchall()
    recent = db.execute(
        f"SELECT id, slug, title, file_path_thumb, year FROM items "
        f"WHERE {_bl} ORDER BY created_at DESC LIMIT 24"
    ).fetchall()
    return render_template("home.html", stats=stats, years=years, recent=recent)


@app.route("/items")
def items():
    db = get_db()
    page = max(1, int(request.args.get("page", 1)))
    sort = request.args.get("sort", "year_desc")
    item_type = request.args.get("type", "")
    product = request.args.get("product", "")
    year = request.args.get("year", "")

    where, params = ["1=1", "id NOT IN (SELECT item_id FROM blocklist)"], []
    if item_type:
        where.append("item_type = ?"); params.append(item_type)
    if product:
        where.append("product = ?"); params.append(product)
    if year:
        where.append("(year = ? OR (circa_start <= ? AND circa_end >= ?))")
        params += [int(year), int(year), int(year)]

    order = {
        "year_desc": "COALESCE(year, circa_start) DESC, title",
        "year_asc":  "COALESCE(year, circa_start) ASC, title",
        "title":     "title",
        "recent":    "created_at DESC",
    }.get(sort, "COALESCE(year, circa_start) DESC, title")

    total = db.execute(
        f"SELECT COUNT(*) FROM items WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    rows = db.execute(
        f"SELECT id, slug, title, item_type, product, year, circa_start, circa_end, "
        f"file_path_thumb FROM items WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, PER_PAGE, (page - 1) * PER_PAGE],
    ).fetchall()

    pages = max(1, math.ceil(total / PER_PAGE))
    types = [r[0] for r in db.execute(
        "SELECT DISTINCT item_type FROM items WHERE id NOT IN (SELECT item_id FROM blocklist) ORDER BY item_type")]
    products = [r[0] for r in db.execute(
        "SELECT DISTINCT product FROM items WHERE product IS NOT NULL "
        "AND id NOT IN (SELECT item_id FROM blocklist) ORDER BY product LIMIT 100")]

    return render_template("items.html",
        rows=rows, total=total, page=page, pages=pages, sort=sort,
        item_type=item_type, product=product, year=year,
        types=types, products=products,
        qs=lambda **kw: "?" + urlencode({**request.args, **kw}))


@app.route("/item/<slug>")
def item(slug: str):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE slug = ?", (slug,)).fetchone()
    if not row:
        abort(404)
    tags = db.execute(
        "SELECT t.slug, t.label FROM tags t JOIN item_tags it ON it.tag_id=t.id "
        "WHERE it.item_id=? ORDER BY t.label",
        (row["id"],),
    ).fetchall()
    sources = db.execute(
        "SELECT kind, reference, acquired_at, notes FROM sources WHERE item_id=?",
        (row["id"],),
    ).fetchall()
    return render_template("item.html", it=row, tags=tags, sources=sources)


@app.route("/tags")
def tags_index():
    db = get_db()
    cloud = [dict(r) for r in db.execute(
        "SELECT t.slug, t.label, COUNT(it.item_id) AS n "
        "FROM tags t LEFT JOIN item_tags it ON it.tag_id=t.id "
        "  AND it.item_id NOT IN (SELECT item_id FROM blocklist) "
        "GROUP BY t.id ORDER BY n DESC, t.label LIMIT 400").fetchall()]
    rows = [dict(r) for r in db.execute(
        "SELECT t.slug, t.label, COALESCE(t.category,'Autre') AS category, "
        "COUNT(it.item_id) AS n FROM tags t LEFT JOIN item_tags it ON it.tag_id=t.id "
        "  AND it.item_id NOT IN (SELECT item_id FROM blocklist) "
        "GROUP BY t.id ORDER BY n DESC, t.label").fetchall()]
    groups = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(r)
    by_category = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    return render_template("tags_index.html", cloud=cloud, by_category=by_category)


@app.route("/tags/<slug>")
def tag(slug: str):
    db = get_db()
    t = db.execute("SELECT * FROM tags WHERE slug=?", (slug,)).fetchone()
    if not t:
        abort(404)
    rows = db.execute(
        "SELECT i.id, i.slug, i.title, i.year, i.circa_start, i.circa_end, "
        "i.file_path_thumb FROM items i JOIN item_tags it ON it.item_id=i.id "
        "WHERE it.tag_id=? AND i.id NOT IN (SELECT item_id FROM blocklist) "
        "ORDER BY COALESCE(i.year, i.circa_start) DESC", (t["id"],),
    ).fetchall()
    return render_template("tag.html", tag=t, rows=rows)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    rows = []
    if q:
        db = get_db()
        # FTS5 query; escape quotes
        safe = q.replace('"', '""')
        rows = db.execute(
            "SELECT i.id, i.slug, i.title, i.year, i.circa_start, i.circa_end, "
            "i.file_path_thumb, snippet(items_fts, 1, '<mark>', '</mark>', '…', 10) AS snip "
            "FROM items_fts JOIN items i ON i.rowid = items_fts.rowid "
            "WHERE items_fts MATCH ? AND i.id NOT IN (SELECT item_id FROM blocklist) "
            "ORDER BY rank LIMIT 200",
            (f'"{safe}"*' if " " in safe else f"{safe}*",),
        ).fetchall()
    return render_template("search.html", q=q, rows=rows)


# ---------- failures ----------
@app.route("/failures")
def failures():
    db = get_db()
    def q(sql):
        return [dict(r) for r in db.execute(sql).fetchall()]
    sel = "SELECT id, slug, item_type, year, title FROM items i "
    no_preview = q(sel + "WHERE file_path_preview IS NULL ORDER BY item_type, id")
    vision_failed = q(sel + "WHERE file_path_preview IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM enrichments e WHERE e.item_id=i.id AND e.model LIKE 'hub:%lite') "
        "ORDER BY item_type, id")
    untranscribed = q(sel + "WHERE item_type='video' AND NOT EXISTS "
        "(SELECT 1 FROM transcripts t WHERE t.item_id=i.id) ORDER BY id")
    return render_template("failures.html", no_preview=no_preview,
                           vision_failed=vision_failed, untranscribed=untranscribed)


# ---------- master access ----------
@app.route("/open-master/<item_id>", methods=["POST"])
def open_master(item_id: str):
    db = get_db()
    row = db.execute("SELECT file_path_master FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        abort(404)
    full = ARCHIVE / row["file_path_master"]
    if not full.exists():
        return jsonify({"ok": False, "error": "master_missing"}), 404
    subprocess.Popen(["open", "-R", str(full)])  # reveal in Finder
    return jsonify({"ok": True})


# ---------- video streaming ----------
# /media ne sert JAMAIS les masters (voir plus haut). Les vidéos n'ont pas de
# dérivé lisible : cette route dédiée sert le master d'un item VIDÉO seulement,
# avec Range (send_file conditional) pour pouvoir avancer dans la lecture.
@app.route("/stream/<item_id>")
def stream(item_id: str):
    db = get_db()
    row = db.execute(
        "SELECT file_path_master FROM items WHERE id=? AND item_type='video'",
        (item_id,),
    ).fetchone()
    if not row or not row["file_path_master"]:
        abort(404)
    full = (ARCHIVE / row["file_path_master"]).resolve()
    if common.MASTERS_ROOT.resolve() not in full.parents:
        abort(403)
    if not full.exists():
        abort(404)
    mimetype = "video/mp4" if full.suffix.lower() in (".mp4", ".m4v") else None
    return send_file(full, mimetype=mimetype, conditional=True, max_age=3600)


# ---------- admin ----------
@app.route("/admin")
def admin():
    db = get_db()
    by_type = db.execute(
        "SELECT item_type, COUNT(*) n FROM items GROUP BY item_type ORDER BY n DESC"
    ).fetchall()
    by_pub = db.execute(
        "SELECT publication_level, COUNT(*) n FROM items GROUP BY publication_level"
    ).fetchall()
    by_rights = db.execute(
        "SELECT rights_status, COUNT(*) n FROM items GROUP BY rights_status"
    ).fetchall()
    last_audit = db.execute(
        "SELECT item_id, action, actor, created_at FROM audit_log "
        "ORDER BY id DESC LIMIT 30"
    ).fetchall()
    return render_template("admin.html", by_type=by_type, by_pub=by_pub,
                           by_rights=by_rights, last_audit=last_audit)


@app.route("/admin/items/<item_id>/edit", methods=["GET", "POST"])
def admin_edit(item_id: str):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        fields = {}
        for k in ("title", "description", "item_type", "product", "campaign",
                  "year", "language", "country", "rights_status", "publication_level",
                  "physical_format", "notes_internal"):
            v = request.form.get(k, "").strip() or None
            if k == "year" and v:
                v = int(v)
            fields[k] = v
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE items SET {sets} WHERE id = ?", (*fields.values(), item_id))
        db.execute(
            "INSERT INTO audit_log (item_id, action, actor) VALUES (?, 'edit', 'admin')",
            (item_id,),
        )
        db.commit()
        flash("saved")
        return redirect(url_for("admin_edit", item_id=item_id))
    return render_template("admin_edit.html", it=row)


@app.route("/admin/sources")
def admin_sources():
    db = get_db()
    rows = db.execute(
        "SELECT s.*, i.title, i.slug FROM sources s JOIN items i ON i.id=s.item_id "
        "ORDER BY s.id DESC LIMIT 200"
    ).fetchall()
    return render_template("admin_sources.html", rows=rows)


@app.route("/admin/exports", methods=["GET", "POST"])
def admin_exports():
    if request.method == "POST":
        subprocess.Popen([
            str(APP_ROOT / "venv" / "bin" / "python"),
            str(ARCHIVE / "scripts" / "export_public_catalog.py"),
        ])
        flash("export triggered")
        return redirect(url_for("admin_exports"))
    export_dir = ARCHIVE / "exports" / "public"
    files = sorted(export_dir.glob("*")) if export_dir.exists() else []
    return render_template("admin_exports.html", files=files)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=True)
