#!/usr/bin/env python3
"""
photo_web.py — Local web UI for the photo intelligence database

Runs locally and is accessible from any device on your network.

Usage:
    python3 photo_web.py
    python3 photo_web.py --port 5050 --db ~/photos_meta.db
    python3 photo_web.py --host 127.0.0.1   # localhost only (default binds all interfaces)
"""

import sqlite3
import argparse
import json
import base64
import subprocess
import tempfile
import os
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_file, abort
from flask import render_template_string

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_DB   = str(Path.home() / "photos_meta.db")
DEFAULT_PORT = 5050
THUMB_SIZE   = 400   # px, longest edge for thumbnails
PAGE_SIZE    = 48    # photos per page

# Curated Gemma tag vocabulary used by the search dropdown and Top Scenes chart.
# Derived from the top-50 Gemma tags in the library; trimmed for duplicates
# (outdoor/outdoors, indoor/interior/home_interior overlap), generic noise
# (people, group, event, display), and redundant time-of-day variants.
# Alphabetized — used directly to render the <option> list.
CURATED_TAGS = [
    "afternoon", "architecture", "baseball", "calm", "candid",
    "cityscape", "city_street", "cloudy", "crowd", "document",
    "evening", "family", "formal", "friends", "home_interior",
    "indoor", "joyful", "landscape", "midday", "morning",
    "museum", "nature", "night", "ocean", "outdoor",
    "park", "portrait", "restaurant", "sports", "stadium",
]

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
DB_PATH = DEFAULT_DB

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    return conn

# ─────────────────────────────────────────────────────────────────────────────
# THUMBNAIL GENERATION
# ─────────────────────────────────────────────────────────────────────────────

_thumb_cache = {}

def make_thumbnail(file_path: str, size: int = THUMB_SIZE) -> bytes | None:
    """Generate JPEG thumbnail using sips with PIL fallback. Cached in memory."""
    if file_path in _thumb_cache:
        return _thumb_cache[file_path]

    p = Path(file_path)
    if not p.exists():
        return None

    # Try sips first (fastest on macOS, handles HEIC natively)
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg",
             "-s", "formatOptions", "75",
             "-Z", str(size),
             str(p), "--out", tmp.name],
            capture_output=True, timeout=30
        )
        if result.returncode == 0:
            thumb_path = Path(tmp.name)
            if thumb_path.exists() and thumb_path.stat().st_size > 0:
                data = thumb_path.read_bytes()
                _thumb_cache[file_path] = data
                return data
    except Exception:
        pass
    finally:
        if tmp:
            try: os.unlink(tmp.name)
            except Exception: pass

    # PIL fallback (handles JPEG, PNG, etc. but not HEIC)
    try:
        from PIL import Image, ImageOps
        import io
        img = Image.open(p)
        try: img = ImageOps.exif_transpose(img)
        except Exception: pass
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        scale = min(size/w, size/h)
        if scale < 1:
            img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        data = buf.getvalue()
        _thumb_cache[file_path] = data
        return data
    except Exception:
        pass

    return None

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    tag_options = "\n".join(
        f'    <option value="{t}">{t.replace("_", " ").title()}</option>'
        for t in CURATED_TAGS
    )
    return render_template_string(HTML_TEMPLATE, tag_options=tag_options)

@app.route("/api/search")
def search():
    q           = request.args.get("q", "").strip()
    year        = request.args.get("year", "").strip()
    person      = request.args.get("person", "").strip()
    scene       = request.args.get("scene", "").strip()
    favorites   = request.args.get("favorites", "").strip()
    has_gemma      = request.args.get("has_gemma", "").strip()
    hide_no_file   = request.args.get("hide_no_file", "").strip()
    hide_screenshot= request.args.get("hide_screenshot", "").strip()
    page           = max(1, int(request.args.get("page", 1)))
    offset      = (page - 1) * PAGE_SIZE

    conn   = get_db()
    params = []
    where  = ["kind = 0"]  # photos only

    if favorites == "1":
        where.append("is_favorite = 1")
    if has_gemma == "1":
        where.append("gemma_processed = 1")
    if hide_no_file == "1":
        where.append("file_path IS NOT NULL")
    if hide_screenshot == "1":
        where.append("is_screenshot = 0")
    if year:
        where.append("year = ?")
        params.append(int(year))
    if person:
        where.append("named_people LIKE ?")
        params.append(f"%{person}%")
    if scene:
        # Filter by Gemma tag (replaces unreliable apple_scene_top).
        # Uses json_each to match an exact tag value inside the JSON array.
        where.append("EXISTS (SELECT 1 FROM json_each(photos.gemma_tags) WHERE value = ?)")
        params.append(scene)

    # Full-text search
    if q:
        try:
            fts_sql = f"""
                SELECT p.* FROM photos p
                JOIN photos_fts f ON p.id = f.rowid
                WHERE f.photos_fts MATCH ?
                  AND {' AND '.join(where)}
                ORDER BY rank
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(fts_sql, [q] + params + [PAGE_SIZE, offset]).fetchall()
            count_row = conn.execute(f"""
                SELECT COUNT(*) FROM photos p
                JOIN photos_fts f ON p.id = f.rowid
                WHERE f.photos_fts MATCH ?
                  AND {' AND '.join(where)}
            """, [q] + params).fetchone()
            total = count_row[0] if count_row else 0
        except Exception:
            # FTS fallback
            pattern = f"%{q}%"
            where.append("""(gemma_description LIKE ? OR gemma_tags LIKE ?
                OR named_people LIKE ?
                OR gemma_location_guess LIKE ? OR vision_text_content LIKE ?)""")
            params += [pattern]*5
            rows  = conn.execute(f"SELECT * FROM photos WHERE {' AND '.join(where)} ORDER BY date_created DESC LIMIT ? OFFSET ?",
                                  params + [PAGE_SIZE, offset]).fetchall()
            count = conn.execute(f"SELECT COUNT(*) FROM photos WHERE {' AND '.join(where)}", params).fetchone()[0]
            total = count
    else:
        sql = f"SELECT * FROM photos WHERE {' AND '.join(where)} ORDER BY date_created DESC LIMIT ? OFFSET ?"
        rows  = conn.execute(sql, params + [PAGE_SIZE, offset]).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM photos WHERE {' AND '.join(where)}", params).fetchone()[0]

    results = []
    for row in rows:
        r = dict(row)
        tags = []
        if r.get("gemma_tags"):
            try: tags = json.loads(r["gemma_tags"])[:8]
            except Exception: pass
        people = []
        if r.get("named_people"):
            try: people = json.loads(r["named_people"])
            except Exception: pass
        results.append({
            "id"          : r["id"],
            "filename"    : r.get("original_filename") or r.get("filename"),
            "date"        : (r.get("date_created") or "")[:10],
            "year"        : r.get("year"),
            "scene"       : tags[0] if tags else None,
            "description" : (r.get("gemma_description") if r.get("gemma_description") not in (None, "parse error", "file not found", "image encode failed") else None) or r.get("apple_description"),
            "location"    : r.get("gemma_location_guess"),
            "tags"        : tags,
            "people"      : people,
            "has_file"    : bool(r.get("file_path")),
            "is_favorite" : bool(r.get("is_favorite")),
            "has_gps"     : bool(r.get("latitude")),
            "lat"         : r.get("latitude"),
            "lon"         : r.get("longitude"),
            "camera"      : r.get("camera_model"),
            "gemma_done"  : r.get("gemma_processed") == 1,
        })

    conn.close()
    return jsonify({
        "results" : results,
        "total"   : total,
        "page"    : page,
        "pages"   : max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
    })

@app.route("/api/photo/<int:photo_id>")
def photo_detail(photo_id):
    conn = get_db()
    row  = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    r = dict(row)
    for json_field in ["gemma_tags", "apple_scene_labels", "named_people",
                        "albums", "vision_classifications"]:
        if r.get(json_field):
            try: r[json_field] = json.loads(r[json_field])
            except Exception: pass
    return jsonify(r)

@app.route("/thumb/<int:photo_id>")
def thumbnail(photo_id):
    conn = get_db()
    row  = conn.execute("SELECT file_path FROM photos WHERE id=?", (photo_id,)).fetchone()
    conn.close()
    if not row or not row[0]:
        abort(404)
    data = make_thumbnail(row[0])
    if not data:
        abort(404)
    from flask import Response
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})

@app.route("/original/<int:photo_id>")
def original(photo_id):
    # FIXME(security): this calls send_file() on whatever absolute path is in
    # photos.file_path. Phase 1 only writes paths inside the Apple Photos
    # library, so in normal operation this is safe — but if you ever extend
    # the schema or import paths from another source, add a sandbox check
    # like `Path(p).resolve().is_relative_to(library_root)` before serving.
    conn = get_db()
    row  = conn.execute("SELECT file_path FROM photos WHERE id=?", (photo_id,)).fetchone()
    conn.close()
    if not row or not row[0]:
        abort(404)
    p = Path(row[0])
    if not p.exists():
        abort(404)
    return send_file(str(p))

@app.route("/api/stats")
def stats():
    conn = get_db()
    s = {}
    for label, sql in [
        ("total",       "SELECT COUNT(*) FROM photos WHERE kind=0"),
        ("with_gemma",  "SELECT COUNT(*) FROM photos WHERE gemma_processed=1"),
        ("with_gps",    "SELECT COUNT(*) FROM photos WHERE latitude IS NOT NULL AND kind=0"),
        ("with_people", "SELECT COUNT(*) FROM photos WHERE named_people IS NOT NULL"),
        ("favorites",   "SELECT COUNT(*) FROM photos WHERE is_favorite=1"),
        ("year_min",    "SELECT MIN(year) FROM photos WHERE year IS NOT NULL"),
        ("year_max",    "SELECT MAX(year) FROM photos WHERE year IS NOT NULL"),
    ]:
        s[label] = conn.execute(sql).fetchone()[0]

    # Top scenes (from Gemma tags, restricted to the curated dropdown vocabulary)
    scenes = conn.execute(f"""
        SELECT value AS tag, COUNT(*) c
        FROM photos, json_each(photos.gemma_tags)
        WHERE kind=0
          AND gemma_processed=1
          AND value IN ({','.join('?' * len(CURATED_TAGS))})
        GROUP BY value
        ORDER BY c DESC
        LIMIT 12
    """, CURATED_TAGS).fetchall()
    s["top_scenes"] = [{"label": r[0], "count": r[1]} for r in scenes]

    # Top people
    people_rows = conn.execute("""
        SELECT named_people FROM photos
        WHERE named_people IS NOT NULL AND kind=0
        LIMIT 5000
    """).fetchall()
    people_count = {}
    for row in people_rows:
        try:
            for name in json.loads(row[0]):
                people_count[name] = people_count.get(name, 0) + 1
        except Exception:
            pass
    s["top_people"] = sorted(
        [{"name": k, "count": v} for k, v in people_count.items()],
        key=lambda x: -x["count"]
    )[:12]

    # Photos per year
    years = conn.execute("""
        SELECT year, COUNT(*) c FROM photos
        WHERE year IS NOT NULL AND kind=0
        GROUP BY year ORDER BY year
    """).fetchall()
    s["by_year"] = [{"year": r[0], "count": r[1]} for r in years]

    conn.close()
    return jsonify(s)

@app.route("/api/years")
def years():
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT year FROM photos
        WHERE year IS NOT NULL AND kind=0
        ORDER BY year DESC
    """).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])

@app.route("/api/map_points")
def map_points():
    """Return all photos with GPS for the map view."""
    conn = get_db()
    rows = conn.execute("""
        SELECT id, latitude, longitude, date_created,
               gemma_description, apple_description
        FROM photos
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
          AND kind = 0 AND file_path IS NOT NULL
        ORDER BY date_created DESC
    """).fetchall()
    conn.close()
    points = []
    for r in rows:
        desc = r[4] if (r[4] and r[4] not in ('parse error', 'file not found', 'image encode failed')) else r[5]
        points.append({
            "id"  : r[0],
            "lat" : r[1],
            "lon" : r[2],
            "date": (r[3] or "")[:10],
            "desc": desc,
        })
    return jsonify({"points": points})

@app.route("/api/people")
def people():
    conn = get_db()
    rows = conn.execute("""
        SELECT named_people FROM photos
        WHERE named_people IS NOT NULL AND kind=0
        LIMIT 10000
    """).fetchall()
    conn.close()
    count = {}
    for row in rows:
        try:
            for name in json.loads(row[0]):
                if name.strip():
                    count[name] = count.get(name, 0) + 1
        except Exception:
            pass
    result = sorted(count.keys(), key=lambda n: -count[n])
    return jsonify(result)

# ─────────────────────────────────────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Photo Intelligence</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Libre+Baskerville:ital,wght@0,400;0,700;1,400&family=DM+Mono:wght@300;400;500&display=swap');

  :root {
    --bg: #0f0f0f;
    --bg2: #161616;
    --bg3: #1e1e1e;
    --border: #2a2a2a;
    --text: #e8e4dc;
    --text2: #8a8580;
    --text3: #5a5550;
    --accent: #c8a96e;
    --accent2: #8fb3a0;
    --red: #c47a6a;
    --card-radius: 4px;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Libre Baskerville', Georgia, serif;
    min-height: 100vh;
    line-height: 1.6;
  }

  /* ── HEADER ── */
  header {
    padding: 24px 32px 0;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: flex-end;
    gap: 32px;
    flex-wrap: wrap;
  }
  .logo {
    font-size: 11px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--text3);
    font-family: 'DM Mono', monospace;
    padding-bottom: 24px;
    white-space: nowrap;
  }
  .logo span { color: var(--accent); }

  nav {
    display: flex;
    gap: 0;
    flex: 1;
  }
  nav button {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text2);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    padding: 12px 20px 22px;
    cursor: pointer;
    transition: all 0.2s;
  }
  nav button:hover { color: var(--text); }
  nav button.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }

  /* ── SEARCH BAR ── */
  .search-bar {
    padding: 24px 32px;
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: center;
  }
  .search-input-wrap {
    flex: 1;
    min-width: 240px;
    position: relative;
  }
  .search-input-wrap input {
    width: 100%;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    color: var(--text);
    font-family: 'Libre Baskerville', serif;
    font-size: 15px;
    padding: 10px 16px 10px 40px;
    outline: none;
    transition: border-color 0.2s;
  }
  .search-input-wrap input:focus { border-color: var(--accent); }
  .search-icon {
    position: absolute;
    left: 13px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--text3);
    font-size: 14px;
  }

  select, .filter-btn {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    color: var(--text2);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.1em;
    padding: 10px 14px;
    cursor: pointer;
    outline: none;
    transition: all 0.2s;
  }
  select:focus, .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .filter-btn.active { border-color: var(--accent); color: var(--accent); background: #1a1510; }

  .search-btn {
    background: var(--accent);
    border: none;
    border-radius: var(--card-radius);
    color: #0f0f0f;
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    padding: 10px 20px;
    cursor: pointer;
    transition: opacity 0.2s;
  }
  .search-btn:hover { opacity: 0.85; }

  /* ── MAIN CONTENT ── */
  main { padding: 24px 32px; }

  /* ── RESULTS INFO ── */
  .results-info {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    color: var(--text3);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.1em;
  }
  .results-info span { color: var(--accent); }

  /* ── PHOTO GRID ── */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
  }

  .card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    overflow: hidden;
    cursor: pointer;
    transition: transform 0.15s, border-color 0.15s;
  }
  .card:hover {
    transform: translateY(-2px);
    border-color: var(--accent);
  }

  .card-thumb {
    width: 100%;
    aspect-ratio: 4/3;
    object-fit: cover;
    display: block;
    background: var(--bg3);
  }
  .card-thumb-placeholder {
    width: 100%;
    aspect-ratio: 4/3;
    background: var(--bg3);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text3);
    font-size: 24px;
  }

  .card-body {
    padding: 10px 12px 12px;
  }
  .card-date {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--text3);
    letter-spacing: 0.1em;
    margin-bottom: 4px;
  }
  .card-desc {
    font-size: 12px;
    color: var(--text2);
    line-height: 1.5;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .card-badges {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 8px;
  }
  .badge {
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    letter-spacing: 0.08em;
    padding: 2px 6px;
    border-radius: 2px;
    background: var(--bg3);
    color: var(--text3);
    border: 1px solid var(--border);
  }
  .badge.scene { color: var(--accent2); border-color: var(--accent2); }
  .badge.person { color: var(--accent); border-color: #3a2e1a; background: #1a1510; }
  .badge.fav { color: #c4a35a; }
  .badge.loc { color: var(--accent2); }

  /* ── PAGINATION ── */
  .pagination {
    display: flex;
    gap: 8px;
    justify-content: center;
    margin-top: 32px;
    flex-wrap: wrap;
  }
  .pagination button {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    color: var(--text2);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    padding: 8px 14px;
    cursor: pointer;
    transition: all 0.2s;
  }
  .pagination button:hover { border-color: var(--accent); color: var(--text); }
  .pagination button.active { border-color: var(--accent); color: var(--accent); background: #1a1510; }
  .pagination button:disabled { opacity: 0.3; cursor: default; }

  /* ── MODAL ── */
  .modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.92);
    z-index: 100;
    overflow-y: auto;
    padding: 24px;
  }
  .modal-overlay.open { display: flex; align-items: flex-start; justify-content: center; }

  .modal {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 6px;
    width: 100%;
    max-width: 960px;
    display: grid;
    grid-template-columns: 1fr 360px;
    overflow: hidden;
    margin: auto;
  }
  @media (max-width: 700px) {
    .modal { grid-template-columns: 1fr; }
    .modal-info { border-left: none; border-top: 1px solid var(--border); }
  }

  .modal-image-wrap {
    background: #000;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 400px;
    max-height: 80vh;
    overflow: hidden;
  }
  .modal-image-wrap img {
    max-width: 100%;
    max-height: 80vh;
    object-fit: contain;
    display: block;
  }

  .modal-info {
    border-left: 1px solid var(--border);
    padding: 24px;
    overflow-y: auto;
    max-height: 80vh;
  }
  .modal-close {
    position: absolute;
    top: 16px;
    right: 16px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 50%;
    color: var(--text2);
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    font-size: 16px;
    z-index: 101;
    transition: all 0.2s;
  }
  .modal-close:hover { color: var(--text); border-color: var(--accent); }

  .modal-filename {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text3);
    letter-spacing: 0.1em;
    margin-bottom: 4px;
  }
  .modal-date {
    font-size: 18px;
    color: var(--accent);
    margin-bottom: 16px;
  }
  .modal-desc {
    font-size: 14px;
    color: var(--text);
    line-height: 1.7;
    margin-bottom: 16px;
    font-style: italic;
  }

  .meta-section {
    margin-bottom: 16px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }
  .meta-section:last-child { border-bottom: none; }
  .meta-label {
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--text3);
    margin-bottom: 6px;
  }
  .meta-value {
    font-size: 13px;
    color: var(--text2);
  }
  .tag-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .tag {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    padding: 3px 8px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 2px;
    color: var(--text2);
  }
  .person-tag {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    padding: 3px 8px;
    background: #1a1510;
    border: 1px solid #3a2e1a;
    border-radius: 2px;
    color: var(--accent);
  }

  /* ── STATS VIEW ── */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 32px;
  }
  .stat-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: 20px;
  }
  .stat-number {
    font-family: 'DM Mono', monospace;
    font-size: 28px;
    color: var(--accent);
    line-height: 1;
    margin-bottom: 6px;
  }
  .stat-label {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--text3);
  }

  .section-title {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--text3);
    margin-bottom: 16px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }

  .bar-chart { margin-bottom: 32px; }
  .bar-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 8px;
  }
  .bar-label {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text2);
    width: 140px;
    flex-shrink: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .bar-track {
    flex: 1;
    height: 6px;
    background: var(--bg3);
    border-radius: 3px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 3px;
    transition: width 0.6s ease;
  }
  .bar-fill.green { background: var(--accent2); }
  .bar-count {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--text3);
    width: 60px;
    text-align: right;
    flex-shrink: 0;
  }

  /* year timeline */
  .year-timeline {
    display: flex;
    align-items: flex-end;
    gap: 3px;
    height: 80px;
    margin-bottom: 8px;
  }
  .year-bar {
    flex: 1;
    background: var(--accent);
    border-radius: 2px 2px 0 0;
    min-height: 2px;
    opacity: 0.7;
    transition: opacity 0.2s;
    cursor: default;
    position: relative;
  }
  .year-bar:hover { opacity: 1; }
  .year-labels {
    display: flex;
    justify-content: space-between;
    font-family: 'DM Mono', monospace;
    font-size: 9px;
    color: var(--text3);
  }

  /* loading */
  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 80px;
    color: var(--text3);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.2em;
  }
  .loading::after {
    content: '';
    width: 16px;
    height: 16px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-left: 12px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── MAP ── */
  #map { background: var(--bg2); }
  .leaflet-popup-content-wrapper {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text);
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  }
  .leaflet-popup-tip { background: var(--bg2); }
  .map-popup img {
    width: 160px; height: 120px;
    object-fit: cover;
    border-radius: 2px;
    display: block;
    margin-bottom: 8px;
    cursor: pointer;
  }
  .map-popup-date {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--text3);
    margin-bottom: 4px;
  }
  .map-popup-desc {
    font-size: 12px;
    color: var(--text2);
    line-height: 1.5;
    max-width: 160px;
  }
  .map-popup-open {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--accent);
    cursor: pointer;
    margin-top: 6px;
    display: inline-block;
  }
  .map-controls {
    position: absolute;
    top: 12px;
    right: 12px;
    z-index: 1000;
    display: flex;
    gap: 8px;
    flex-direction: column;
  }
  .map-stat {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px 12px;
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--text3);
    pointer-events: none;
  }
  .map-stat span { color: var(--accent); }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.css"/>

<style>
  .empty {
    text-align: center;
    padding: 80px;
    color: var(--text3);
    font-family: 'DM Mono', monospace;
    font-size: 12px;
    letter-spacing: 0.15em;
  }
</style>
</head>
<body>

<header>
  <div class="logo">Photo <span>Intelligence</span></div>
  <nav>
    <button class="active" onclick="showView('search')">Search</button>
    <button onclick="showView('stats')">Library Stats</button>
    <button onclick="showView('map')">Map</button>
  </nav>
</header>

<div class="search-bar">
  <div class="search-input-wrap">
    <span class="search-icon">⌕</span>
    <input type="text" id="searchInput" placeholder="Search descriptions, locations, people, tags…"
           onkeydown="if(event.key==='Enter') doSearch()">
  </div>
  <select id="yearFilter" onchange="doSearch()">
    <option value="">All years</option>
  </select>
  <select id="personFilter" onchange="doSearch()">
    <option value="">All people</option>
  </select>
  <select id="sceneFilter" onchange="doSearch()">
    <option value="">All scenes</option>
{{ tag_options|safe }}
  </select>
  <button class="filter-btn" id="favBtn" onclick="toggleFav()">★ Favorites</button>
  <button class="filter-btn" id="gemmaBtn" onclick="toggleGemma()">AI Described</button>
  <button class="filter-btn active" id="hideNoFileBtn" onclick="toggleHideNoFile()">☁ Hide iCloud-only</button>
  <button class="filter-btn active" id="hideScreenshotBtn" onclick="toggleHideScreenshot()">⊘ Hide Screenshots</button>
  <button class="search-btn" onclick="doSearch()">Search</button>
</div>

<!-- SEARCH VIEW -->
<main id="searchView">
  <div class="results-info" id="resultsInfo" style="display:none">
    <div>Showing <span id="resultCount">0</span> photos</div>
    <div id="pageInfo"></div>
  </div>
  <div id="photoGrid" class="grid"></div>
  <div class="pagination" id="pagination"></div>
</main>

<!-- STATS VIEW -->
<main id="statsView" style="display:none">
  <div id="statsContent"><div class="loading">Loading stats</div></div>
</main>

<!-- MAP VIEW -->
<main id="mapView" style="display:none;padding:0">
  <div id="map" style="width:100%;height:calc(100vh - 140px)"></div>
</main>

<!-- MODAL -->
<div class="modal-overlay" id="modal" onclick="closeModal(event)">
  <button class="modal-close" onclick="closeModalBtn()">✕</button>
  <div class="modal" id="modalContent">
    <div class="modal-image-wrap">
      <img id="modalImg" src="" alt="">
    </div>
    <div class="modal-info" id="modalInfo"></div>
  </div>
</div>

<script>
let currentPage = 1;
let currentQuery = {};
let favActive = false;
let gemmaActive = false;
let hideNoFileActive = true;
let hideScreenshotActive = true;

// ── INIT ──────────────────────────────────────────────────────────────────
async function init() {
  // Load years
  const years = await fetch('/api/years').then(r => r.json());
  const yearSel = document.getElementById('yearFilter');
  years.forEach(y => {
    const opt = document.createElement('option');
    opt.value = y; opt.textContent = y;
    yearSel.appendChild(opt);
  });

  // Load people
  const people = await fetch('/api/people').then(r => r.json());
  const personSel = document.getElementById('personFilter');
  people.slice(0, 50).forEach(p => {
    const opt = document.createElement('option');
    opt.value = p; opt.textContent = p;
    personSel.appendChild(opt);
  });

  doSearch();
}

// ── SEARCH ────────────────────────────────────────────────────────────────
function toggleFav() {
  favActive = !favActive;
  document.getElementById('favBtn').classList.toggle('active', favActive);
  doSearch();
}
function toggleGemma() {
  gemmaActive = !gemmaActive;
  document.getElementById('gemmaBtn').classList.toggle('active', gemmaActive);
  doSearch();
}
function toggleHideNoFile() {
  hideNoFileActive = !hideNoFileActive;
  document.getElementById('hideNoFileBtn').classList.toggle('active', hideNoFileActive);
  doSearch();
}
function toggleHideScreenshot() {
  hideScreenshotActive = !hideScreenshotActive;
  document.getElementById('hideScreenshotBtn').classList.toggle('active', hideScreenshotActive);
  doSearch();
}

function doSearch(page = 1) {
  currentPage = page;
  const q = {
    q:         document.getElementById('searchInput').value.trim(),
    year:      document.getElementById('yearFilter').value,
    person:    document.getElementById('personFilter').value,
    scene:     document.getElementById('sceneFilter').value,
    favorites: favActive ? '1' : '',
    has_gemma: gemmaActive ? '1' : '',
    hide_no_file: hideNoFileActive ? '1' : '',
    hide_screenshot: hideScreenshotActive ? '1' : '',
    page:      page,
  };
  currentQuery = q;
  loadResults(q);
}

async function loadResults(q) {
  const grid = document.getElementById('photoGrid');
  grid.innerHTML = '<div class="loading">Searching</div>';

  const params = new URLSearchParams(Object.fromEntries(
    Object.entries(q).filter(([,v]) => v !== '')
  ));
  const data = await fetch('/api/search?' + params).then(r => r.json());

  document.getElementById('resultsInfo').style.display = 'flex';
  document.getElementById('resultCount').textContent = data.total.toLocaleString();
  document.getElementById('pageInfo').textContent =
    data.pages > 1 ? `Page ${data.page} of ${data.pages}` : '';

  if (data.results.length === 0) {
    grid.innerHTML = '<div class="empty">No photos found</div>';
    document.getElementById('pagination').innerHTML = '';
    return;
  }

  grid.innerHTML = data.results.map(p => `
    <div class="card" onclick="openModal(${p.id})">
      ${p.has_file
        ? `<img class="card-thumb" src="/thumb/${p.id}" loading="lazy" alt="">`
        : `<div class="card-thumb-placeholder">☁</div>`}
      <div class="card-body">
        <div class="card-date">${p.date || ''}${p.is_favorite ? ' ★' : ''}</div>
        <div class="card-desc">${p.description || p.scene || ''}</div>
        <div class="card-badges">
          ${p.scene ? `<span class="badge scene">${p.scene}</span>` : ''}
          ${p.location ? `<span class="badge loc">📍 ${p.location.split('(')[0].trim()}</span>` : ''}
          ${(p.people||[]).slice(0,2).map(n => `<span class="badge person">${n.split(' ')[0]}</span>`).join('')}
        </div>
      </div>
    </div>
  `).join('');

  // Pagination
  const pag = document.getElementById('pagination');
  if (data.pages <= 1) { pag.innerHTML = ''; return; }

  let pagHtml = '';
  if (data.page > 1)
    pagHtml += `<button onclick="doSearch(${data.page-1})">← Prev</button>`;

  const start = Math.max(1, data.page - 3);
  const end   = Math.min(data.pages, data.page + 3);
  if (start > 1) pagHtml += `<button onclick="doSearch(1)">1</button><span style="color:var(--text3);padding:8px">…</span>`;
  for (let i = start; i <= end; i++)
    pagHtml += `<button class="${i===data.page?'active':''}" onclick="doSearch(${i})">${i}</button>`;
  if (end < data.pages) pagHtml += `<span style="color:var(--text3);padding:8px">…</span><button onclick="doSearch(${data.pages})">${data.pages}</button>`;
  if (data.page < data.pages)
    pagHtml += `<button onclick="doSearch(${data.page+1})">Next →</button>`;

  pag.innerHTML = pagHtml;
}

// ── MODAL ─────────────────────────────────────────────────────────────────
async function openModal(id) {
  const modal = document.getElementById('modal');
  const img   = document.getElementById('modalImg');
  const info  = document.getElementById('modalInfo');

  img.src = `/thumb/${id}?size=800`;
  info.innerHTML = '<div class="loading">Loading</div>';
  modal.classList.add('open');

  const p = await fetch(`/api/photo/${id}`).then(r => r.json());

  // Full-size image
  img.src = `/thumb/${id}?size=800`;

  const people = Array.isArray(p.named_people) ? p.named_people : [];
  const tags   = Array.isArray(p.gemma_tags) ? p.gemma_tags : [];

  info.innerHTML = `
    <div class="modal-filename">${p.original_filename || p.filename || ''}</div>
    <div class="modal-date">${(p.date_created||'').slice(0,10)}</div>

    ${(p.gemma_description && !['parse error','file not found','image encode failed'].includes(p.gemma_description)) ? `
    <div class="meta-section">
      <div class="meta-label">AI Description</div>
      <div class="modal-desc">${p.gemma_description}</div>
    </div>` : p.apple_description ? `
    <div class="meta-section">
      <div class="meta-label">Apple Description</div>
      <div class="modal-desc">${p.apple_description}</div>
    </div>` : ''}

    ${p.gemma_location_guess ? `
    <div class="meta-section">
      <div class="meta-label">Location Guess</div>
      <div class="meta-value">📍 ${p.gemma_location_guess}</div>
    </div>` : ''}

    ${people.length ? `
    <div class="meta-section">
      <div class="meta-label">People</div>
      <div class="tag-list">${people.map(n=>`<span class="person-tag">${n}</span>`).join('')}</div>
    </div>` : ''}

    ${tags.length ? `
    <div class="meta-section">
      <div class="meta-label">Tags</div>
      <div class="tag-list">${tags.map(t=>`<span class="tag">${t}</span>`).join('')}</div>
    </div>` : ''}

    ${p.latitude ? `
    <div class="meta-section">
      <div class="meta-label">GPS</div>
      <div class="meta-value">${Number(p.latitude).toFixed(4)}, ${Number(p.longitude).toFixed(4)}</div>
    </div>` : ''}

    ${p.camera_model ? `
    <div class="meta-section">
      <div class="meta-label">Camera</div>
      <div class="meta-value">${p.camera_make||''} ${p.camera_model}${p.lens_model ? ' · '+p.lens_model : ''}</div>
      ${p.aperture ? `<div class="meta-value" style="margin-top:4px;font-size:12px;color:var(--text3)">
        f/${p.aperture} · ${p.shutter_speed}s · ISO ${p.iso||'—'}</div>` : ''}
    </div>` : ''}

    ${p.is_favorite ? `<div style="color:var(--accent);font-size:12px;font-family:'DM Mono',monospace">★ Favorite</div>` : ''}
  `;
}

function closeModal(e) {
  if (e.target === document.getElementById('modal'))
    document.getElementById('modal').classList.remove('open');
}
function closeModalBtn() {
  document.getElementById('modal').classList.remove('open');
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('modal').classList.remove('open');
});

// ── STATS ─────────────────────────────────────────────────────────────────
let _map = null;

function showView(view) {
  document.getElementById('searchView').style.display = view==='search' ? 'block' : 'none';
  document.getElementById('statsView').style.display  = view==='stats'  ? 'block' : 'none';
  document.getElementById('mapView').style.display    = view==='map'    ? 'block' : 'none';
  document.querySelectorAll('nav button').forEach((b,i) =>
    b.classList.toggle('active',
      (i===0&&view==='search')||(i===1&&view==='stats')||(i===2&&view==='map')));
  if (view === 'stats') loadStats();
  if (view === 'map')   initMap();
}

// ── MAP ───────────────────────────────────────────────────────────────────
async function initMap() {
  if (_map) return;  // already initialized

  _map = L.map('map', {
    center: [20, 0],
    zoom: 2,
    preferCanvas: true,
  });

  // Dark tile layer from CartoDB
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '© OpenStreetMap © CARTO',
    subdomains: 'abcd',
    maxZoom: 19
  }).addTo(_map);

  // Custom marker icon using accent color
  const icon = L.divIcon({
    className: '',
    html: `<div style="
      width:10px;height:10px;
      background:var(--accent);
      border-radius:50%;
      border:2px solid rgba(200,169,110,0.4);
      box-shadow:0 0 6px rgba(200,169,110,0.6)">
    </div>`,
    iconSize: [10, 10],
    iconAnchor: [5, 5],
  });

  // Load GPS photos from API
  const statEl = document.createElement('div');
  statEl.className = 'map-stat';
  statEl.innerHTML = 'Loading…';
  statEl.style.cssText = 'position:absolute;bottom:32px;left:12px;z-index:1000';
  document.getElementById('mapView').appendChild(statEl);

  const data = await fetch('/api/map_points').then(r => r.json());

  const clusters = L.markerClusterGroup({
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    zoomToBoundsOnClick: true,
    iconCreateFunction: function(cluster) {
      const count = cluster.getChildCount();
      return L.divIcon({
        html: `<div style="
          background:rgba(200,169,110,0.85);
          color:#0f0f0f;
          border-radius:50%;
          width:${count>100?40:count>20?34:28}px;
          height:${count>100?40:count>20?34:28}px;
          display:flex;align-items:center;justify-content:center;
          font-family:'DM Mono',monospace;font-size:11px;font-weight:500;
          border:2px solid rgba(200,169,110,0.3);">${count}</div>`,
        className: '',
        iconSize: [40, 40],
        iconAnchor: [20, 20],
      });
    }
  });

  data.points.forEach(p => {
    const marker = L.marker([p.lat, p.lon], {icon});
    const thumbSrc = `/thumb/${p.id}`;
    const desc = p.desc ? p.desc.slice(0, 80) + (p.desc.length > 80 ? '…' : '') : '';
    marker.bindPopup(`
      <div class="map-popup">
        <img src="${thumbSrc}" onclick="openModal(${p.id})" alt="">
        <div class="map-popup-date">${p.date || ''}</div>
        ${desc ? `<div class="map-popup-desc">${desc}</div>` : ''}
        <span class="map-popup-open" onclick="openModal(${p.id})">View details →</span>
      </div>
    `, {maxWidth: 200});
    clusters.addLayer(marker);
  });

  _map.addLayer(clusters);

  statEl.innerHTML = `<span>${data.points.length.toLocaleString()}</span> photos with GPS`;
}

async function loadStats() {
  const s = await fetch('/api/stats').then(r => r.json());
  const pct = v => Math.round(v/s.total*100);
  const fmt = n => n?.toLocaleString() || '0';

  const maxYear = Math.max(...s.by_year.map(y=>y.count));
  const yearBars = s.by_year.map(y => {
    const h = Math.round((y.count/maxYear)*100);
    return `<div class="year-bar" style="height:${h}%" title="${y.year}: ${fmt(y.count)} photos"></div>`;
  }).join('');
  const firstYear = s.by_year[0]?.year || '';
  const lastYear  = s.by_year[s.by_year.length-1]?.year || '';

  const maxScene = Math.max(...s.top_scenes.map(s=>s.count));
  const sceneBars = s.top_scenes.map(sc =>
    `<div class="bar-row">
      <div class="bar-label">${sc.label}</div>
      <div class="bar-track"><div class="bar-fill green" style="width:${Math.round(sc.count/maxScene*100)}%"></div></div>
      <div class="bar-count">${fmt(sc.count)}</div>
    </div>`).join('');

  const maxPerson = s.top_people[0]?.count || 1;
  const peopleBars = s.top_people.map(p =>
    `<div class="bar-row">
      <div class="bar-label">${p.name}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round(p.count/maxPerson*100)}%"></div></div>
      <div class="bar-count">${fmt(p.count)}</div>
    </div>`).join('');

  document.getElementById('statsContent').innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-number">${fmt(s.total)}</div><div class="stat-label">Total Photos</div></div>
      <div class="stat-card"><div class="stat-number">${fmt(s.with_gemma)}</div><div class="stat-label">AI Described</div></div>
      <div class="stat-card"><div class="stat-number">${fmt(s.with_gps)}</div><div class="stat-label">With GPS</div></div>
      <div class="stat-card"><div class="stat-number">${fmt(s.with_people)}</div><div class="stat-label">With Named People</div></div>
      <div class="stat-card"><div class="stat-number">${fmt(s.favorites)}</div><div class="stat-label">Favorites</div></div>
      <div class="stat-card"><div class="stat-number">${s.year_min}–${s.year_max}</div><div class="stat-label">Year Range</div></div>
    </div>

    <div class="bar-chart">
      <div class="section-title">Photos by Year</div>
      <div class="year-timeline">${yearBars}</div>
      <div class="year-labels"><span>${firstYear}</span><span>${lastYear}</span></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:32px;flex-wrap:wrap">
      <div class="bar-chart">
        <div class="section-title">Top Scenes</div>
        ${sceneBars}
      </div>
      <div class="bar-chart">
        <div class="section-title">Most Photographed People</div>
        ${peopleBars}
      </div>
    </div>
  `;
}

init();
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Photo Intelligence Web UI")
    parser.add_argument("--db",   default=DEFAULT_DB,   help="Path to photos_meta.db")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="Port to listen on")
    parser.add_argument("--host", default="0.0.0.0",    help="Host to bind (0.0.0.0 = all interfaces)")
    args = parser.parse_args()

    global DB_PATH
    DB_PATH = args.db

    if not Path(DB_PATH).exists():
        print(f"ERROR: Database not found: {DB_PATH}")
        return

    print(f"\nPhoto Intelligence")
    print(f"  Database : {DB_PATH}")
    print(f"  Local    : http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print(f"  Network  : http://<this-host>:{args.port}")
    print(f"\nCtrl+C to stop\n")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__ == "__main__":
    main()
