#!/usr/bin/env python3
"""
photo_intel_web.py — Local web UI for the photo-intel database.

Runs on the processing host, beside photo-intel.db. Accessible from any device on the
local network. Default URL: http://localhost:5052

This is the photo-intel successor to the legacy photo_web.py (which ran on
the export host against photos_meta.db). Ported to the photo-intel schema:

  - Primary key is `uuid` (TEXT), not an integer `id`. All photo routes
    are keyed by uuid.
  - `media_type = 'image'` replaces the legacy `kind = 0`.
  - There is NO `file_path` column. The image file for a uuid is located
    via a uuid->path index built by walking `dest_dir`, exactly as
    photo_intel_phase2.py does. The index is refreshed by a background
    thread so newly-rsynced photos appear without a restart.
  - Search uses a photos_fts FTS5 table (content='photos') with
    sync triggers. Smart Search adds Gemma-generated query expansion.
  - No `is_favorite` / `is_screenshot` / camera EXIF / apple_description
    columns exist, so those legacy filters / modal sections are dropped.
  - `year` is derived from substr(date,1,4) — there is no year column.

Image serving:
  - Thumbnails: Pillow + pillow-heif (Linux — no macOS `sips`). Cached to
    disk under <dest_dir>/.thumb_cache/ as JPEG.
  - /original/<uuid> resolves the path and confirms it is inside dest_dir
    before send_file (sandbox check — closes the legacy security FIXME).

Usage:
    source ~/photo-intel/venv/bin/activate
    python3 ~/photo-intel/photo_intel_web.py
    python3 ~/photo-intel/photo_intel_web.py --port 5052
    python3 ~/photo-intel/photo_intel_web.py --config ~/photo-intel/photo-intel.conf
"""

import sqlite3
import argparse
import configparser
import json
import io
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort, Response
from flask import render_template_string

# Shared with Phase 2 / 2b so the place string a viewer reads is the same one
# the VLM was told. Sibling module in this script's own directory.
from photo_intel_places import place_display
# Apple-vs-legacy person vocabulary; see the module docstring for why the
# persons column holds names Apple Photos has never heard of.
import photo_intel_names as names_mod

try:
    from PIL import Image, ImageOps
except ImportError:
    raise SystemExit("ERROR: Pillow not installed. Run: pip3 install pillow")

# pillow-heif registers HEIC/HEIF support with Pillow. Required on Linux —
# the legacy macOS `sips` path does not exist here.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except ImportError:
    HEIF_OK = False

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PORT     = 5052
THUMB_SIZE       = 400    # px, longest edge for grid thumbnails
MODAL_SIZE       = 800    # px, longest edge for the modal image
SHARE_SIZE       = 2048   # px, longest edge for the /share rendition
SHARE_QUALITY    = 90     # JPEG quality for /share — this one leaves the house
PAGE_SIZE        = 48     # photos per page
INDEX_REFRESH_S  = 600    # uuid->path index refresh interval (seconds)

# Curated Gemma tag vocabulary for the search dropdown and Top Scenes chart.
# Carried over verbatim from the legacy photo_web.py so the scene filter
# behaves identically. Alphabetized; rendered directly as the <option> list.
CURATED_TAGS = [
    "afternoon", "architecture", "baseball", "calm", "candid",
    "cityscape", "city_street", "cloudy", "crowd", "document",
    "evening", "family", "formal", "friends", "home_interior",
    "indoor", "joyful", "landscape", "midday", "morning",
    "museum", "nature", "night", "ocean", "outdoor",
    "park", "portrait", "restaurant", "sports", "stadium",
]

# Placeholder description strings that should never be shown as real text.
BAD_DESC = {"parse error", "file not found", "image encode failed", None, ""}

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP + GLOBAL STATE
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

DB_PATH   = None      # set in main()
DEST_DIR  = None      # set in main()
THUMB_DIR = None      # set in main(), = DEST_DIR/.thumb_cache
OLLAMA_URL   = None   # set in main()
OLLAMA_MODEL = None   # set in main()
# One shared secret gates every mutating route (/api/delete, /api/photo/<uuid>/edit).
# Set in main() from [web] delete_token — the config key kept its original name
# from when delete was the only write. Empty disables all of them.
WRITE_TOKEN = None

# uuid -> Path index. Built at startup, refreshed by a background thread.
_file_index      = {}
_file_index_lock = threading.Lock()

# DB uuids with no file on disk (e.g. osxphotos exported a sidecar for an
# iCloud-missing original). Excluded from /api/search so the grid never
# shows a dead card. Recomputed whenever the file index is (re)built.
_missing_uuids   = set()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    return conn


def get_write_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# UUID -> PATH INDEX
# ─────────────────────────────────────────────────────────────────────────────

IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tiff", ".tif", ".bmp", ".webp"}
# Mirrors VIDEO_EXTENSIONS in photo_intel_phase1.py.
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp"}

def pick_for_index(existing: "Path | None", candidate: "Path") -> "Path":
    """Return whichever path should own the stem slot.
    An image extension always beats a non-image (e.g. .mov Live Photo clip)."""
    if existing is None:
        return candidate
    if candidate.suffix.lower() in IMAGE_EXTS and existing.suffix.lower() not in IMAGE_EXTS:
        return candidate
    return existing

def build_file_index() -> dict:
    """Walk dest_dir and map each file stem (the photo uuid) to its Path.
    Mirrors the index photo_intel_phase2.py builds before its run.
    pick_for_index() ensures a still image always wins over a Live Photo .mov."""
    idx = {}
    if not DEST_DIR.exists():
        return idx
    for p in DEST_DIR.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".json":
            continue
        if THUMB_DIR in p.parents:
            continue
        idx[p.stem] = pick_for_index(idx.get(p.stem), p)
    return idx


def compute_missing_uuids(idx: dict) -> set:
    """DB uuids that have no file in the index. An empty index means the
    photo volume is unreachable — return an empty set rather than
    declaring the whole library missing."""
    if not idx:
        return set()
    conn = get_db()
    rows = conn.execute("SELECT uuid FROM photos").fetchall()
    conn.close()
    return {r[0] for r in rows if r[0] not in idx}


def refresh_index_loop():
    """Background thread: rebuild the uuid->path index periodically so
    photos delivered by a later rsync become visible without a restart."""
    global _missing_uuids
    while True:
        time.sleep(INDEX_REFRESH_S)
        try:
            new_idx = build_file_index()
            missing = compute_missing_uuids(new_idx)
            with _file_index_lock:
                _file_index.clear()
                _file_index.update(new_idx)
                _missing_uuids = missing
        except Exception as e:
            print(f"  index refresh error: {e}", flush=True)


def lookup_path(uuid: str):
    """Return the Path for a uuid, or None if not in the index."""
    with _file_index_lock:
        return _file_index.get(uuid)


def missing_uuids() -> list:
    """Snapshot of DB uuids with no local file, for SQL exclusion. Capped:
    a huge set means something is wrong with the volume, and a thousand
    bound params is where filtering stops being worth it."""
    with _file_index_lock:
        missing = list(_missing_uuids)
    return missing if len(missing) <= 500 else []


# ─────────────────────────────────────────────────────────────────────────────
# THUMBNAIL GENERATION  (Pillow + pillow-heif, disk-cached)
# ─────────────────────────────────────────────────────────────────────────────

def extract_video_frame(src: Path) -> bytes | None:
    """One early frame of a video as JPEG bytes via ffmpeg, used as the
    grid/modal poster for video rows. Tries t=1s first; a clip shorter
    than a second falls back to the first frame."""
    for ss in ("1", "0"):
        try:
            r = subprocess.run(
                ["ffmpeg", "-ss", ss, "-i", str(src), "-frames:v", "1",
                 "-f", "image2pipe", "-vcodec", "mjpeg",
                 "-loglevel", "error", "-"],
                capture_output=True, timeout=30)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            return None
    return None


def make_thumbnail(uuid: str, src: Path, size: int,
                   quality: int = 78) -> bytes | None:
    """Return JPEG bytes for a thumbnail of `src` at the given longest-edge
    size. Cached on disk under THUMB_DIR as <uuid>_<size>.jpg. Video
    sources are thumbnailed from an ffmpeg-extracted frame.

    `quality` is in the cache filename when it is not the default, so /share's
    higher-quality rendition and a same-size thumbnail cannot collide."""
    suffix_q   = f"_q{quality}" if quality != 78 else ""
    cache_path = THUMB_DIR / f"{uuid}_{size}{suffix_q}.jpg"
    if cache_path.exists():
        try:
            return cache_path.read_bytes()
        except Exception:
            pass

    suffix = src.suffix.lower()
    if suffix in (".heic", ".heif") and not HEIF_OK:
        return None

    try:
        if suffix in VIDEO_EXTS:
            frame = extract_video_frame(src)
            if not frame:
                return None
            img = Image.open(io.BytesIO(frame))
        else:
            img = Image.open(src)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        scale = min(size / w, size / h)
        if scale < 1:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
    except Exception:
        return None

    try:
        tmp = cache_path.with_suffix(".jpg.tmp")
        tmp.write_bytes(data)
        tmp.replace(cache_path)
    except Exception:
        pass  # cache write failure is non-fatal — still return the bytes

    return data


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# Indexed columns of photos_fts, plus short aliases for the gemma_-prefixed
# ones. A "col:term" query is scoped to that column; anything else with a colon
# in it is treated as ordinary text. static/app.js's SCOPE_HINT lists the same
# names — change both together.
FTS_COLUMNS = {
    "gemma_description":    "gemma_description",
    "gemma_tags":           "gemma_tags",
    "persons":              "persons",
    "gemma_location_guess": "gemma_location_guess",
    "place_name":           "place_name",
    # aliases
    "description":          "gemma_description",
    "tags":                 "gemma_tags",
    "person":               "persons",
    "location":             "gemma_location_guess",
    "place":                "place_name",
}

# Columns the database's photos_fts actually has, read once. An index built
# before place_name existed (see migrations/) lacks it, and scoping to a column
# the index does not have is an FTS5 error — a 500 instead of a search. A scope
# to a known but missing column drops the prefix and searches the word
# unscoped instead.
_fts_present = None


def _fts_columns() -> set:
    global _fts_present
    if _fts_present is None:
        try:
            conn = get_db()
            _fts_present = {r[1] for r in conn.execute(
                "PRAGMA table_info(photos_fts)")}
            conn.close()
        except Exception:
            return set()
    return _fts_present


def fts_quote(term: str) -> str:
    """Wrap a single term for FTS5 MATCH. Exact token — prefix matching causes false
    positives for short words (e.g. 'bird*' matches 'birthday').

    A leading "col:" is honoured when col is an indexed photos_fts column (see
    FTS_COLUMNS), producing 'place_name:"boston"' — the quotes go *inside* the
    colon. Quoting the whole thing instead gives FTS5 the phrase
    "place boston", which matches nothing and looks like a broken search.
    Everything else keeps the old behaviour: the term is quoted whole, so a
    stray colon, operator or paren is inert text rather than FTS5 syntax."""
    col, sep, rest = term.partition(":")
    if sep:
        target = FTS_COLUMNS.get(col.strip().lower())
        rest = rest.replace('"', ' ').strip()
        if target and rest:
            if target in _fts_columns():
                return target + ':"' + rest + '"'
            return '"' + rest + '"'
    return '"' + term.replace('"', ' ').strip() + '"'


def build_fts_match(q: str, expand: list) -> str:
    """Build FTS5 MATCH expression: q tokens ANDed, expand terms ORed in.
    Returns None when there is nothing to match."""
    q_tokens = [fts_quote(w) for w in q.split() if w.strip()]
    exp_tokens = [fts_quote(e) for e in expand if e.strip()]
    exp_tokens = [t for t in exp_tokens if t not in q_tokens]
    if not q_tokens and not exp_tokens:
        return None
    parts = []
    if q_tokens:
        parts.append(' AND '.join(q_tokens))
    parts.extend(exp_tokens)
    return ' OR '.join(parts)


def clean_desc(value):
    """Return the description if it is real text, else None."""
    if value is None:
        return None
    return None if str(value).strip().lower() in BAD_DESC else value


def parse_json_array(value):
    """Parse a JSON-array text column into a Python list; [] on failure."""
    if not value:
        return []
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

def static_version(filename: str) -> int:
    """mtime of a static asset, used as a cache-busting query param so
    deployed CSS/JS changes land on a plain reload."""
    try:
        return int((Path(app.static_folder) / filename).stat().st_mtime)
    except OSError:
        return 0


@app.route("/")
def index():
    tag_options = "\n".join(
        f'    <option value="{t}">{t.replace("_", " ").title()}</option>'
        for t in CURATED_TAGS
    )
    resp = Response(render_template_string(
        HTML_TEMPLATE, tag_options=tag_options,
        css_v=static_version("app.css"), js_v=static_version("app.js")))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/search")
def search():
    q         = request.args.get("q", "").strip()
    year      = request.args.get("year", "").strip()
    year_from = request.args.get("year_from", "").strip()
    year_to   = request.args.get("year_to", "").strip()
    person    = request.args.get("person", "").strip()
    scene     = request.args.get("scene", "").strip()
    has_gemma = request.args.get("has_gemma", "").strip()
    expand_raw = request.args.get("expand", "").strip()
    expand    = [e.strip() for e in expand_raw.split(",") if e.strip()] if expand_raw else []
    page      = max(1, int(request.args.get("page", 1)))
    offset    = (page - 1) * PAGE_SIZE

    conn   = get_db()
    params = []
    # Videos included since 2026-06-12 (open item #20) — they carry a
    # play badge in the grid and stream in the modal.
    where  = ["media_type IN ('image', 'video')"]

    # Rows whose file is absent on disk render as dead cards — hide them.
    no_file = missing_uuids()
    if no_file:
        where.append(f"uuid NOT IN ({','.join('?' * len(no_file))})")
        params.extend(no_file)

    if has_gemma == "1":
        where.append("phase2_processed = 1")
    if year:
        where.append("substr(date, 1, 4) = ?")
        params.append(year)
    if year_from:
        where.append("substr(date, 1, 4) >= ?")
        params.append(year_from)
    if year_to:
        where.append("substr(date, 1, 4) <= ?")
        params.append(year_to)
    # person may be comma-separated ("Jane Doe,John Doe") — every
    # named person must appear in the photo's persons array.
    for name in (n.strip() for n in person.split(",")):
        if name:
            where.append("persons LIKE ?")
            params.append(f"%{name}%")
    if scene:
        # exact tag match inside the gemma_tags JSON array
        where.append("EXISTS (SELECT 1 FROM json_each(photos.gemma_tags) "
                     "WHERE value = ?)")
        params.append(scene)
    if q or expand:
        fts_expr = build_fts_match(q, expand)
        if fts_expr:
            where.append(
                "photos.rowid IN (SELECT rowid FROM photos_fts WHERE photos_fts MATCH ?)"
            )
            params.append(fts_expr)

    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM photos WHERE {where_sql}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM photos WHERE {where_sql} "
        f"ORDER BY date DESC, time DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, offset],
    ).fetchall()
    conn.close()

    results = []
    for row in rows:
        r = dict(row)
        tags   = parse_json_array(r.get("gemma_tags"))[:8]
        people = parse_json_array(r.get("persons"))
        uuid   = r["uuid"]
        results.append({
            "uuid"        : uuid,
            "media_type"  : r.get("media_type"),
            "date"        : r.get("date") or "",
            "year"        : (r.get("date") or "")[:4],
            "scene"       : tags[0] if tags else None,
            "description" : clean_desc(r.get("gemma_description")),
            # Apple's reverse geocode when we have it; the VLM's guess is
            # only a fallback for assets with no GPS at all.
            "location"    : place_display(r) or r.get("gemma_location_guess"),
            "location_is_guess": place_display(r) is None,
            "tags"        : tags,
            "people"      : people,
            "has_file"    : lookup_path(uuid) is not None,
            "has_gps"     : r.get("gps_lat") is not None,
            "gemma_done"  : r.get("phase2_processed") == 1,
        })

    return jsonify({
        "results": results,
        "total"  : total,
        "page"   : page,
        "pages"  : max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
    })


@app.route("/api/photo/<uuid>")
def photo_detail(uuid):
    conn = get_db()
    row  = conn.execute("SELECT * FROM photos WHERE uuid=?", (uuid,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    r = dict(row)
    for field in ("gemma_tags", "persons", "scene_labels", "face_regions"):
        if r.get(field):
            r[field] = parse_json_array(r[field])
    r["has_file"] = lookup_path(uuid) is not None
    # Composed server-side so the modal and the result cards agree.
    r["place_display"] = place_display(r)
    return jsonify(r)


@app.route("/thumb/<uuid>")
def thumbnail(uuid):
    size = request.args.get("size", type=int) or THUMB_SIZE
    size = max(80, min(size, 1600))
    src  = lookup_path(uuid)
    if src is None:
        abort(404)
    src = _sandboxed_path(src)
    if src is None:
        abort(403)
    if not src.exists():
        abort(404)
    data = make_thumbnail(uuid, src, size)
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


def _sandboxed_path(src):
    """Resolve `src` and confirm it sits inside DEST_DIR (the export tree).

    Returns the resolved Path, or None if it escapes the sandbox or cannot be
    resolved. Central guard for every route that reads or deletes a file by its
    DB-stored path (`/original`, `/thumb`, `/api/delete`). The uuid in each
    route is a DB key rather than a client-supplied path, so the first line of
    defense is the ingest-side invariant that the file-path column only holds
    paths inside DEST_DIR; this enforces that invariant in code, closing the
    send_file/unlink path-trust FIXME.
    """
    try:
        resolved = src.resolve(strict=False)
    except OSError:
        return None
    if not resolved.is_relative_to(DEST_DIR.resolve()):
        return None
    return resolved


@app.route("/original/<uuid>")
def original(uuid):
    src = lookup_path(uuid)
    if src is None:
        abort(404)
    resolved = _sandboxed_path(src)
    if resolved is None:
        abort(403)
    if not resolved.exists():
        abort(404)
    # conditional=True enables Range requests — required for <video> seek.
    return send_file(str(resolved), conditional=True)


def share_filename(uuid: str, ext: str) -> str:
    """`1980-08-19-150144.jpg` — a name the share sheet can show and a recipient
    can file, instead of a uuid. Date and time come from the DB, never from the
    request; the strip is belt-and-braces so nothing can reach the
    Content-Disposition header that would need quoting."""
    stem = uuid
    try:
        conn = get_db()
        row  = conn.execute("SELECT date, time FROM photos WHERE uuid=?",
                            (uuid,)).fetchone()
        conn.close()
        if row and row["date"]:
            t = "".join(c for c in (row["time"] or "") if c.isdigit())
            stem = row["date"] + (f"-{t}" if t else "")
    except Exception:
        pass
    stem = "".join(c for c in stem if c.isalnum() or c in "-_")
    return f"{stem}.{ext}"


@app.route("/share/<uuid>")
def share(uuid):
    """A share-ready rendition of one photo, for the browser's share sheet.

    Deliberately not `/original`, for three reasons: much of an Apple Photos
    library is HEIC, and an HEIC handed to a non-Apple recipient is an
    unopenable file; `/original` serves the bytes as stored, while
    make_thumbnail applies the EXIF orientation, so a photo shot sideways goes
    out upright; and the on-disk name is a uuid. Videos have none of those
    problems, so they are passed through byte-for-byte.

    Same `lookup_path` -> `_sandboxed_path` chain as every other file-serving
    route — the uuid is a DB key, never a client-supplied path."""
    src = lookup_path(uuid)
    if src is None:
        abort(404)
    resolved = _sandboxed_path(src)
    if resolved is None:
        abort(403)
    if not resolved.exists():
        abort(404)

    suffix = resolved.suffix.lower()
    if suffix in VIDEO_EXTS:
        # conditional=True keeps Range support. The Content-Disposition is written
        # by hand rather than left to as_attachment/download_name: send_file emits
        # an *unquoted* filename token, so the two media types would hand the
        # client two different header forms. One quoted form for both.
        resp = send_file(str(resolved), conditional=True)
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{share_filename(uuid, suffix.lstrip("."))}"')
        return resp

    data = make_thumbnail(uuid, resolved, SHARE_SIZE, quality=SHARE_QUALITY)
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={
        "Content-Disposition":
            f'attachment; filename="{share_filename(uuid, "jpg")}"',
        "Cache-Control": "private, max-age=3600",
    })


@app.route("/api/stats")
def stats():
    conn = get_db()
    s = {}
    for label, sql in [
        ("total",       "SELECT COUNT(*) FROM photos WHERE media_type IN ('image','video')"),
        ("with_gemma",  "SELECT COUNT(*) FROM photos WHERE media_type IN ('image','video') "
                        "AND phase2_processed=1"),
        ("with_gps",    "SELECT COUNT(*) FROM photos WHERE media_type IN ('image','video') "
                        "AND gps_lat IS NOT NULL"),
        ("with_people", "SELECT COUNT(*) FROM photos WHERE media_type IN ('image','video') "
                        "AND persons IS NOT NULL AND persons != '[]'"),
        ("year_min",    "SELECT MIN(substr(date,1,4)) FROM photos "
                        "WHERE date IS NOT NULL AND date != ''"),
        ("year_max",    "SELECT MAX(substr(date,1,4)) FROM photos "
                        "WHERE date IS NOT NULL AND date != ''"),
    ]:
        s[label] = conn.execute(sql).fetchone()[0]

    # Top scenes — Gemma tags restricted to the curated vocabulary.
    placeholders = ",".join("?" * len(CURATED_TAGS))
    scenes = conn.execute(f"""
        SELECT value AS tag, COUNT(*) c
        FROM photos, json_each(photos.gemma_tags)
        WHERE media_type IN ('image','video')
          AND phase2_processed=1
          AND value IN ({placeholders})
        GROUP BY value
        ORDER BY c DESC
        LIMIT 12
    """, CURATED_TAGS).fetchall()
    s["top_scenes"] = [{"label": r[0], "count": r[1]} for r in scenes]

    # Top people — counted from the persons JSON arrays.
    people_rows = conn.execute(
        "SELECT persons FROM photos "
        "WHERE media_type IN ('image','video') AND persons IS NOT NULL AND persons != '[]'"
    ).fetchall()
    people_count = {}
    for row in people_rows:
        for name in parse_json_array(row[0]):
            if name and name.strip():
                people_count[name] = people_count.get(name, 0) + 1
    s["top_people"] = sorted(
        ({"name": k, "count": v} for k, v in people_count.items()),
        key=lambda x: -x["count"],
    )[:12]

    # Photos per year.
    years = conn.execute("""
        SELECT substr(date,1,4) y, COUNT(*) c FROM photos
        WHERE media_type IN ('image','video') AND date IS NOT NULL AND date != ''
        GROUP BY y ORDER BY y
    """).fetchall()
    s["by_year"] = [{"year": r[0], "count": r[1]} for r in years]

    conn.close()
    return jsonify(s)


@app.route("/api/years")
def years():
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT substr(date,1,4) y FROM photos
        WHERE media_type IN ('image','video') AND date IS NOT NULL AND date != ''
        ORDER BY y DESC
    """).fetchall()
    conn.close()
    return jsonify([r[0] for r in rows if r[0]])


@app.route("/api/people")
def people():
    """Person names for the People typeahead, most-photographed first.

    `?vocab=apple` (the default) returns only people actually named in Apple
    Photos. The rest of the `persons` vocabulary is legacy XMP face tags
    carried in from imported files — case variants, nicknames and
    relationship words like "Mommy" off old scans — which typically appear on
    one or two photos each, so offering them as suggestions buries the real
    names.

    Nothing is hidden from search by this: the People control is a free-text
    input, so a legacy tag is still found by typing it. `?vocab=all` returns
    the unfiltered list.

    When `apple_persons` has never been refreshed (photo_intel_apple_vocab.py
    + photo_intel_names_admin.py refresh-vocab) the table is empty or absent,
    which means "vocabulary unknown" — everything is returned, i.e. the
    behavior before this filter existed.
    """
    want_all = request.args.get("vocab", "apple").lower() == "all"
    conn = get_db()
    known = set() if want_all else names_mod.apple_person_names(conn)
    rows = conn.execute(
        "SELECT persons FROM photos "
        "WHERE media_type IN ('image','video') AND persons IS NOT NULL AND persons != '[]'"
    ).fetchall()
    conn.close()
    count = {}
    for row in rows:
        names = {n.strip() for n in parse_json_array(row[0])
                 if isinstance(n, str) and n.strip()}
        if known:
            names = {n for n in names if n.lower() in known}
        for name in names:
            count[name] = count.get(name, 0) + 1
    return jsonify(sorted(count.keys(), key=lambda n: -count[n]))


@app.route("/api/map_points")
def map_points():
    """Photos and videos with GPS, honoring the same filters as /api/search."""
    q          = request.args.get("q", "").strip()
    year       = request.args.get("year", "").strip()
    year_from  = request.args.get("year_from", "").strip()
    year_to    = request.args.get("year_to", "").strip()
    person     = request.args.get("person", "").strip()
    scene      = request.args.get("scene", "").strip()
    has_gemma  = request.args.get("has_gemma", "").strip()
    expand_raw = request.args.get("expand", "").strip()
    expand     = [e.strip() for e in expand_raw.split(",") if e.strip()] if expand_raw else []

    conn   = get_db()
    params = []
    where  = ["media_type IN ('image','video')",
              "gps_lat IS NOT NULL", "gps_lon IS NOT NULL"]

    if has_gemma == "1":
        where.append("phase2_processed = 1")
    if year:
        where.append("substr(date, 1, 4) = ?")
        params.append(year)
    if year_from:
        where.append("substr(date, 1, 4) >= ?")
        params.append(year_from)
    if year_to:
        where.append("substr(date, 1, 4) <= ?")
        params.append(year_to)
    for name in (n.strip() for n in person.split(",")):
        if name:
            where.append("persons LIKE ?")
            params.append(f"%{name}%")
    if scene:
        where.append("EXISTS (SELECT 1 FROM json_each(photos.gemma_tags) "
                     "WHERE value = ?)")
        params.append(scene)
    if q or expand:
        fts_expr = build_fts_match(q, expand)
        if fts_expr:
            where.append(
                "photos.rowid IN (SELECT rowid FROM photos_fts WHERE photos_fts MATCH ?)"
            )
            params.append(fts_expr)

    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"SELECT uuid, gps_lat, gps_lon, date, gemma_description "
        f"FROM photos WHERE {where_sql} ORDER BY date DESC", params
    ).fetchall()
    conn.close()
    points = []
    for r in rows:
        uuid = r[0]
        if lookup_path(uuid) is None:
            continue  # no local file — nothing to show in the popup
        points.append({
            "uuid": uuid,
            "lat" : r[1],
            "lon" : r[2],
            "date": (r[3] or "")[:10],
            "desc": clean_desc(r[4]),
        })
    return jsonify({"points": points})


# ─────────────────────────────────────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────


def repair_json_quotes(text: str) -> str:
    """Escape unescaped interior double-quotes inside JSON string values.
    gemma4:e4b emits structurally-correct JSON but does not escape `"`
    inside prose values. Carried from photo_intel_phase2.py. No-op on
    already-valid JSON."""
    out, in_str, i = [], False, 0
    while i < len(text):
        c = text[i]
        if c == '"':
            if not in_str:
                in_str = True
                out.append(c)
            else:
                j = i + 1
                while j < len(text) and text[j] in " \t\r\n":
                    j += 1
                if j < len(text) and text[j] in ",:}]":
                    in_str = False
                    out.append(c)
                else:
                    out.append('\\"')
        else:
            out.append(c)
        i += 1
    return "".join(out)


_people_cache = {"names": None, "ts": 0.0}
_PEOPLE_TTL_S = 600  # the people vocabulary changes only when Phase 1 ingests


def people_list():
    """Distinct named persons across the library — the closed list the
    model is allowed to pick from. Cached: smart_parse is on the hot
    path of every Smart/voice search and must not pay a full-table
    scan per request.

    Restricted to the Apple Photos vocabulary for the same reason
    /api/people is: feeding the legacy XMP tail to the model both bloats the
    prompt and invites resolving "mommy" or a nickname to a filter that
    matches almost nothing. Falls back to every name while apple_persons is
    unpopulated."""
    now = time.time()
    if _people_cache["names"] is not None and now - _people_cache["ts"] < _PEOPLE_TTL_S:
        return _people_cache["names"]
    conn = get_db()
    known = names_mod.apple_person_names(conn)
    rows = conn.execute(
        "SELECT persons FROM photos "
        "WHERE media_type IN ('image','video') AND persons IS NOT NULL AND persons != '[]'"
    ).fetchall()
    conn.close()
    names = set()
    for row in rows:
        for name in parse_json_array(row[0]):
            name = (name or "").strip() if isinstance(name, str) else ""
            if not name:
                continue
            if known and name.lower() not in known:
                continue
            names.add(name)
    _people_cache["names"] = sorted(names)
    _people_cache["ts"] = now
    return _people_cache["names"]


@app.route("/api/smart_parse")
def smart_parse():
    """Natural-language query -> structured filters via Ollama. Returns
    {q, year, person, scene}; any field the model cannot fill confidently
    from the real vocabulary is left blank. On any failure returns {} so
    the UI falls back to a plain search."""
    text = request.args.get("q", "").strip()
    if not text:
        return jsonify({})

    try:
        import ollama
    except ImportError:
        return jsonify({"error": "ollama package not installed"}), 200

    people = people_list()
    prompt = (
        "You convert a photo-search request into structured filters.\n"
        "Return ONLY a JSON object, no prose, with exactly these keys:\n"
        '  {"q": "", "year": "", "year_from": "", "year_to": "", "persons": [], "scene": "", "terms": []}\n\n'
        "Rules:\n"
        "- persons: every person the request names, each copied as the\n"
        "  EXACTLY matching full name from the PEOPLE list. A first name\n"
        "  alone (e.g. 'Jane') counts as naming that person if exactly\n"
        "  one PEOPLE entry has that first name. If no PEOPLE entry\n"
        "  matches, omit that person. Use [] when nobody is named.\n"
        "- q: keep ALL descriptive words from the request — subject,\n"
        "  colors, sizes, adjectives. 'yellow bird' -> q='yellow bird',\n"
        "  NOT q='bird'. NEVER put a person's name in q.\n"
        "  Use \"\" if there is no visual subject.\n"
        "- year: a 4-digit year if EXACTLY one is named, else \"\".\n"
        "- year_from / year_to: for a range or decade, the 4-digit\n"
        "  bounds, inclusive. 'the 90s' -> year_from=1990, year_to=1999.\n"
        "  'between 1993 and 1995' -> year_from=1993, year_to=1995.\n"
        "  'early 80s' -> year_from=1980, year_to=1984. When these are\n"
        "  set, year must be \"\". All \"\" when no year is mentioned.\n"
        "- scene: copy EXACTLY one tag from the SCENE TAGS list if one\n"
        "  clearly applies, else \"\".\n"
        "- terms: 2-4 alternative phrasings for the WHOLE concept in q\n"
        "  — other words a description might use for the same thing.\n"
        "  Do NOT split q into parts and list them separately. Do NOT\n"
        "  use broad categories or hypernyms.\n"
        "  'yellow bird' -> terms: ['canary','goldfinch','warbler']\n"
        "  NOT terms: ['yellow','avian','feathered']\n"
        "  Only fill if q is not empty, else [].\n"
        "- Never invent a person or scene not in the lists.\n\n"
        "Example: request 'show me yellow birds' ->\n"
        '  {"q": "yellow bird", "year": "", "year_from": "", "year_to": "", "persons": [], "scene": "", "terms": ["canary", "goldfinch", "warbler"]}\n'
        "Example: request 'pictures of Jane Doe at the park' ->\n"
        '  {"q": "park", "year": "", "year_from": "", "year_to": "", "persons": ["Jane Doe"], "scene": "park", "terms": ["playground", "picnic"]}\n'
        "Example: request 'show me Jane and John in 2019' ->\n"
        '  {"q": "", "year": "2019", "year_from": "", "year_to": "", "persons": ["Jane Doe", "John Doe"], "scene": "", "terms": []}\n'
        "Example: request 'beach photos from the early 90s' ->\n"
        '  {"q": "beach", "year": "", "year_from": "1990", "year_to": "1994", "persons": [], "scene": "ocean", "terms": ["shore", "sand", "seaside"]}\n'
        "Example: request 'birthday party' ->\n"
        '  {"q": "birthday party", "year": "", "year_from": "", "year_to": "", "persons": [], "scene": "", "terms": ["cake", "candles", "balloons"]}\n\n'
        f"PEOPLE: {', '.join(people)}\n"
        f"SCENE TAGS: {', '.join(CURATED_TAGS)}\n\n"
        f"REQUEST: {text}\n"
    )

    try:
        client = ollama.Client(host=OLLAMA_URL)
        resp = client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            think=False,
            options={"num_predict": 320, "temperature": 0},
        )
        raw = resp["message"]["content"].strip()
    except Exception as e:
        return jsonify({"error": str(e)}), 200

    a, b = raw.find("{"), raw.rfind("}")
    parsed = {}
    if a != -1 and b != -1 and b > a:
        chunk = raw[a:b + 1]
        try:
            parsed = json.loads(chunk)
        except Exception:
            try:
                parsed = json.loads(repair_json_quotes(chunk))
            except Exception:
                parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    out = {"q": "", "year": "", "year_from": "", "year_to": "",
           "person": "", "persons": [], "scene": "", "terms": []}
    out["q"] = str(parsed.get("q", "") or "").strip()

    for key in ("year", "year_from", "year_to"):
        yr = str(parsed.get(key, "") or "").strip()
        if len(yr) == 4 and yr.isdigit():
            out[key] = yr
    # A single year and a range are mutually exclusive; a lone bound is
    # meaningless without its partner.
    if out["year"]:
        out["year_from"] = out["year_to"] = ""
    elif not (out["year_from"] and out["year_to"]):
        out["year_from"] = out["year_to"] = ""

    def resolve_person(name):
        match = next((n for n in people if n.lower() == name.lower()), "")
        if not match:
            match = next((n for n in people
                          if name.lower() in n.lower() or n.lower() in name.lower()), "")
        return match

    persons_raw = parsed.get("persons", [])
    # Tolerate the legacy single-string "person" key if the model emits it.
    if not persons_raw and parsed.get("person"):
        persons_raw = [parsed["person"]]
    if isinstance(persons_raw, list):
        for pr in persons_raw:
            pr = str(pr or "").strip()
            if not pr:
                continue
            match = resolve_person(pr)
            if match and match not in out["persons"]:
                out["persons"].append(match)
    # person (singular) kept for the Search tab's single-select dropdown.
    out["person"] = out["persons"][0] if out["persons"] else ""

    # Defense in depth: if the model named nobody, scan the raw request
    # for any first name that uniquely identifies one PEOPLE entry.
    # Resolves bare first names the model declined to fill.
    if not out["persons"]:
        req_words = {w.strip(",.!?;:").lower()
                     for w in text.split() if len(w.strip(",.!?;:")) > 2}
        first_name_map = {}
        for full in people:
            fn = full.split()[0].lower()
            first_name_map.setdefault(fn, []).append(full)
        for fn, fulls in first_name_map.items():
            if len(fulls) == 1 and fn in req_words:
                out["persons"].append(fulls[0])
        out["person"] = out["persons"][0] if out["persons"] else ""

    # If a resolved person's name leaked into q, strip it back out.
    # Whole-word match only — a bare replace() eats substrings inside
    # other words (person 'Ana' would turn 'banana' into 'ban').
    if out["persons"] and out["q"]:
        cleaned = out["q"]
        for full in out["persons"]:
            for part in full.split():
                cleaned = re.sub(rf"\b{re.escape(part)}\b", "", cleaned,
                                 flags=re.IGNORECASE)
        out["q"] = " ".join(cleaned.split()).strip(" ,-")

    sc = str(parsed.get("scene", "") or "").strip().lower()
    if sc in CURATED_TAGS:
        out["scene"] = sc

    terms_raw = parsed.get("terms", [])
    if isinstance(terms_raw, list):
        out["terms"] = [str(t).strip() for t in terms_raw if str(t).strip()][:8]

    return jsonify(out)



# ── Country code → human-readable name (common subset) ──────────────────────
_CC_NAMES = {
    "US": "United States", "MX": "Mexico", "CA": "Canada",
    "GB": "United Kingdom", "FR": "France", "DE": "Germany",
    "ES": "Spain", "IT": "Italy", "PT": "Portugal", "NL": "Netherlands",
    "BE": "Belgium", "CH": "Switzerland", "AT": "Austria", "GR": "Greece",
    "TR": "Turkey", "IL": "Israel", "EG": "Egypt", "MA": "Morocco",
    "ZA": "South Africa", "KE": "Kenya", "TZ": "Tanzania",
    "JP": "Japan", "CN": "China", "KR": "South Korea", "TH": "Thailand",
    "VN": "Vietnam", "ID": "Indonesia", "MY": "Malaysia", "SG": "Singapore",
    "IN": "India", "AU": "Australia", "NZ": "New Zealand",
    "BR": "Brazil", "AR": "Argentina", "CO": "Colombia", "PE": "Peru",
    "CU": "Cuba", "DO": "Dominican Republic", "PR": "Puerto Rico",
    "CR": "Costa Rica", "PA": "Panama", "GT": "Guatemala",
    "CZ": "Czech Republic", "PL": "Poland", "HU": "Hungary",
    "HR": "Croatia", "BA": "Bosnia and Herzegovina",
    "RU": "Russia", "UA": "Ukraine",
    "AE": "UAE", "SA": "Saudi Arabia", "JO": "Jordan",
    "AG": "Antigua and Barbuda",
    "AS": "American Samoa",
    "AW": "Aruba",
    "BB": "Barbados",
    "BM": "Bermuda",
    "BQ": "Bonaire",
    "BS": "Bahamas",
    "BZ": "Belize",
    "CW": "Curacao",
    "DM": "Dominica",
    "FJ": "Fiji",
    "GI": "Gibraltar",
    "HN": "Honduras",
    "HT": "Haiti",
    "IE": "Ireland",
    "IS": "Iceland",
    "KY": "Cayman Islands",
    "MC": "Monaco",
    "ME": "Montenegro",
    "NO": "Norway",
    "PF": "French Polynesia",
    "SX": "Sint Maarten",
    "TC": "Turks and Caicos",
    "VA": "Vatican City",
    "VG": "British Virgin Islands",
    "VI": "US Virgin Islands",
    "WS": "Samoa",
}

_rg = None


def _geocoder():
    """Lazy-load reverse_geocoder — first call loads ~30 MB dataset."""
    global _rg
    if _rg is None:
        import reverse_geocoder
        _rg = reverse_geocoder
    return _rg


@app.route("/api/locations")
def locations():
    """GPS-based geographic summary grouped by year and country.
    Params: year_from, year_to, min_photos (default 1).
    First call is slow (~5 s) while reverse_geocoder loads its dataset."""
    year_from  = request.args.get("year_from", "").strip()
    year_to    = request.args.get("year_to",   "").strip()
    min_photos = max(1, int(request.args.get("min_photos", 1)))

    conn  = get_db()
    where = ["gps_lat IS NOT NULL", "gps_lon IS NOT NULL",
             "date IS NOT NULL", "date != ''"]
    params = []
    if year_from:
        where.append("CAST(substr(date,1,4) AS INT) >= ?")
        params.append(int(year_from))
    if year_to:
        where.append("CAST(substr(date,1,4) AS INT) <= ?")
        params.append(int(year_to))

    rows = conn.execute(
        "SELECT substr(date,1,4) AS yr, "
        "       ROUND(gps_lat,2) AS lat, ROUND(gps_lon,2) AS lon, "
        "       COUNT(*) AS cnt "
        "FROM photos "
        "WHERE " + " AND ".join(where) + " "
        "GROUP BY yr, ROUND(gps_lat,2), ROUND(gps_lon,2) "
        "HAVING COUNT(*) >= ? "
        "ORDER BY yr, COUNT(*) DESC",
        params + [min_photos]
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({"summary": [], "by_year": {}})

    # Batch-geocode all unique (lat, lon) pairs in one pass.
    unique_coords = list({(float(r["lat"]), float(r["lon"])) for r in rows})
    rg = _geocoder()
    geocoded = rg.search(unique_coords, verbose=False)
    coord_map = {coord: geo for coord, geo in zip(unique_coords, geocoded)}

    # Aggregate: year -> cc -> {name, count, cities{city: count}}
    by_year = {}
    for row in rows:
        yr    = row["yr"]
        coord = (float(row["lat"]), float(row["lon"]))
        geo   = coord_map.get(coord, {})
        cc    = geo.get("cc", "??")
        city  = geo.get("name", "Unknown")
        cnt   = row["cnt"]

        by_year.setdefault(yr, {})
        by_year[yr].setdefault(cc, {
            "name": _CC_NAMES.get(cc, cc),
            "count": 0,
            "cities": {}
        })
        by_year[yr][cc]["count"] += cnt
        cities = by_year[yr][cc]["cities"]
        cities[city] = cities.get(city, 0) + cnt

    # Build flat summary list ordered by year.
    summary = []
    for yr in sorted(by_year):
        countries = sorted(by_year[yr].items(), key=lambda x: -x[1]["count"])
        summary.append({
            "year": yr,
            "countries": [
                {
                    "cc":         cc,
                    "name":       info["name"],
                    "photos":     info["count"],
                    "top_cities": [
                        {"city": city, "photos": cnt}
                        for city, cnt in sorted(
                            info["cities"].items(), key=lambda x: -x[1]
                        )[:5]
                    ],
                }
                for cc, info in countries
            ],
        })

    return jsonify({"summary": summary, "by_year": by_year})


_UUID_RE = re.compile(r'^[0-9A-Fa-f\-]{8,36}$')


@app.route("/api/delete", methods=["POST"])
def delete_photos():
    # Destructive endpoint — requires the shared secret from
    # photo-intel.conf [web] delete_token. Fail closed when unset.
    if not WRITE_TOKEN:
        return jsonify({"error": "delete disabled: no delete_token configured"}), 403
    if request.headers.get("X-Delete-Token", "") != WRITE_TOKEN:
        return jsonify({"error": "invalid or missing delete token"}), 403

    data  = request.get_json(silent=True) or {}
    uuids = data.get("uuids", [])
    if not uuids or not isinstance(uuids, list):
        return jsonify({"error": "uuids required"}), 400
    uuids = [u for u in uuids if isinstance(u, str) and _UUID_RE.match(u)]
    if not uuids:
        return jsonify({"error": "no valid uuids"}), 400

    errors = []
    deleted_files = []

    for uuid in uuids:
        path = lookup_path(uuid)
        if path:
            resolved = _sandboxed_path(path)
            if resolved is None:
                errors.append(f"{uuid}: refused (outside sandbox)")
                continue
            # Remove every file for this uuid, not just the one the index
            # picked: the sidecar (<name>.json) and any Live Photo .mov live
            # beside it. Leaving the sidecar was the old bug — Phase 1 would
            # re-ingest it and the "deleted" photo came back within ~2 h.
            for sibling in sorted(resolved.parent.glob(f"{uuid}.*")):
                sib = _sandboxed_path(sibling)
                if sib is None:
                    errors.append(f"{uuid}: refused (outside sandbox)")
                    continue
                try:
                    sib.unlink()
                    deleted_files.append(str(sib))
                except Exception as e:
                    errors.append(f"{uuid}: {e}")
        for thumb in THUMB_DIR.glob(f"{uuid}_*.jpg"):
            try:
                thumb.unlink()
            except Exception:
                pass

    conn = get_write_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Tombstone BEFORE dropping the row. The photo is still in Apple Photos, so
    # staging keeps its copy and the next rsync re-delivers the file — the
    # tombstone is what stops Phase 1 re-inserting a row for it, which is what
    # actually keeps it out of the UI. Written first so that a crash between
    # these two statements fails safe (a tombstone with its row still present
    # is harmless; a dropped row with no tombstone resurrects).
    conn.executemany(
        "INSERT OR IGNORE INTO suppressed (uuid, at, reason) VALUES (?, ?, 'web-delete')",
        [(u, now) for u in uuids])
    placeholders = ",".join("?" * len(uuids))
    conn.execute(f"DELETE FROM photos WHERE uuid IN ({placeholders})", uuids)
    conn.commit()
    conn.close()

    with _file_index_lock:
        for uuid in uuids:
            _file_index.pop(uuid, None)

    return jsonify({"deleted": len(uuids), "files": deleted_files,
                    "suppressed": len(uuids), "errors": errors})


# ─── manual metadata edit ──────────────────────────────────────────
# Apple's face recognition, Gemma's descriptions and Apple's reverse geocode are
# all wrong often enough to need a correction path, and without one the only
# option is sqlite3 on the processing host. Every column this route writes was
# checked against every other writer of `photos` first:
#
#   persons / gemma_tags / gemma_description
#       Phase 1's upsert (ON CONFLICT DO UPDATE) sets only phase1_processed_at,
#       sidecar_path and COALESCEd GPS — it never touches these. Phase 2 and
#       Phase 2b write gemma_description / gemma_tags only for rows still at
#       phase2_processed=0, i.e. not yet enriched — and even then keep any
#       column named in edited_fields (below).
#   date
#       Nothing recomputes it after Phase 1's insert.
#   place_*
#       photo_intel_places.py --apply rewrites these for every uuid in Apple's
#       dump EXCEPT place_source='manual', which this route sets. The nearby
#       fallback only fills rows whose place_name is NULL/''.
#
# Every edit is also recorded in `edited_fields` (a JSON list of column names).
# Phase 2 and Phase 2b keep any gemma_description / gemma_tags named there, so
# a description or tags typed in before the model reaches a photo survive its
# enrichment instead of being replaced. Any new machine writer over `photos`
# must honor `edited_fields` the same way. The FTS triggers reindex on UPDATE,
# so search follows an edit with no extra work here.
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Bounds exist so a stuck client can't write a novel into the FTS index.
MAX_LIST_ITEMS = 60
MAX_ITEM_CHARS = 120
MAX_DESC_CHARS = 4000


def _ensure_edited_fields(conn) -> None:
    """Add photos.edited_fields if this database predates it (Phase 2 / 2b add
    it the same way). Called on the write connection before the first edit."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    if "edited_fields" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN edited_fields TEXT")
        conn.commit()


def _clean_str_list(value):
    """Trim, drop blanks, de-dupe case-insensitively, keep first-seen order."""
    if not isinstance(value, list):
        raise ValueError("must be a list of strings")
    out, seen = [], set()
    for item in value[:MAX_LIST_ITEMS]:
        if not isinstance(item, str):
            raise ValueError("list items must be strings")
        text = " ".join(item.split())[:MAX_ITEM_CHARS]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _retimed_datetime_original(existing: str, new_date: str):
    """Swap the date half of an ISO datetime_original, keeping time + offset.

    Leaving it stale would put `date` and `datetime_original` in disagreement
    for anything that reads the latter. Unparseable or absent values are left
    alone rather than guessed at.
    """
    if not existing or not isinstance(existing, str) or len(existing) < 10:
        return None
    if not _DATE_RE.match(existing[:10]):
        return None
    return new_date + existing[10:]


@app.route("/api/photo/<uuid>/edit", methods=["POST"])
def edit_photo(uuid):
    """Apply a partial metadata correction. Only the keys sent are written."""
    if not WRITE_TOKEN:
        return jsonify({"error": "editing disabled: no delete_token configured"}), 403
    if request.headers.get("X-Edit-Token", "") != WRITE_TOKEN:
        return jsonify({"error": "invalid or missing edit token"}), 403
    if not _UUID_RE.match(uuid):
        return jsonify({"error": "bad uuid"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "json object required"}), 400

    conn = get_db()
    row  = conn.execute("SELECT * FROM photos WHERE uuid=?", (uuid,)).fetchone()
    conn.close()
    if not row:
        abort(404)

    sets, params, changed = [], [], []

    try:
        if "persons" in data:
            names = _clean_str_list(data["persons"])
            sets.append("persons = ?")
            params.append(json.dumps(names))
            changed.append("persons")

        if "gemma_tags" in data:
            tags = _clean_str_list(data["gemma_tags"])
            sets.append("gemma_tags = ?")
            params.append(json.dumps(tags))
            changed.append("gemma_tags")

        if "gemma_description" in data:
            desc = data["gemma_description"]
            if not isinstance(desc, str):
                raise ValueError("gemma_description must be a string")
            desc = desc.strip()[:MAX_DESC_CHARS]
            sets.append("gemma_description = ?")
            params.append(desc or None)
            changed.append("gemma_description")

        if "date" in data:
            new_date = str(data["date"]).strip()
            if not _DATE_RE.match(new_date):
                raise ValueError("date must be YYYY-MM-DD")
            try:
                datetime.strptime(new_date, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"{new_date} is not a real date")
            sets.append("date = ?")
            params.append(new_date)
            changed.append("date")
            retimed = _retimed_datetime_original(row["datetime_original"], new_date)
            if retimed:
                sets.append("datetime_original = ?")
                params.append(retimed)

        if "place_name" in data:
            if "place_source" not in row.keys():
                raise ValueError("place editing needs the place columns — run "
                                 "photo_intel_places.py --migrate first")
            place = data["place_name"]
            if not isinstance(place, str):
                raise ValueError("place_name must be a string")
            place = " ".join(place.split())[:MAX_ITEM_CHARS * 2]
            # A typed place replaces the whole Apple breakdown rather than
            # sitting on top of it — otherwise place_display() would compose
            # the new name with the old city/state and read as a place that
            # does not exist. Clearing it hands the row back to the geocoder.
            sets.extend(["place_name = ?", "place_aoi = NULL", "place_city = NULL",
                         "place_state = NULL", "place_country = NULL",
                         "place_country_code = NULL", "place_source = ?",
                         "place_updated_at = ?"])
            params.extend([place or None, "manual" if place else None,
                           datetime.now(timezone.utc).isoformat(timespec="seconds")])
            changed.append("place_name")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not sets:
        return jsonify({"error": "nothing to change"}), 400

    conn = get_write_db()
    _ensure_edited_fields(conn)
    # Accumulate rather than replace: editing the description today must not
    # un-protect the tags corrected last week. `date` covers datetime_original
    # too, since retiming writes them as one.
    prior = conn.execute("SELECT edited_fields FROM photos WHERE uuid=?",
                         (uuid,)).fetchone()
    already = set(parse_json_array(prior[0] if prior else None))
    sets.append("edited_fields = ?")
    params.append(json.dumps(sorted(already | set(changed))))
    conn.execute(f"UPDATE photos SET {', '.join(sets)} WHERE uuid = ?",
                 params + [uuid])
    conn.commit()
    conn.close()

    conn = get_db()
    fresh = conn.execute("SELECT * FROM photos WHERE uuid=?", (uuid,)).fetchone()
    conn.close()
    out = dict(fresh)
    for field in ("gemma_tags", "persons", "scene_labels", "face_regions"):
        if out.get(field):
            out[field] = parse_json_array(out[field])
    out["has_file"]      = lookup_path(uuid) is not None
    out["place_display"] = place_display(out)
    return jsonify({"ok": True, "changed": changed, "photo": out})


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Photo Intelligence</title>
<link rel="stylesheet" href="/static/app.css?v={{ css_v }}"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.css"/>
</head>
<body>

<header>
  <div class="logo">Photo <span>Intelligence</span></div>
  <nav>
    <button class="active" id="micBtn" onclick="voiceTabClick()"
            title="Click and speak to search photos">
      <svg class="mic-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="9" y="2" width="6" height="11" rx="3"/>
        <path d="M5 10a7 7 0 0 0 14 0"/>
        <line x1="12" y1="19" x2="12" y2="22"/>
        <line x1="8" y1="22" x2="16" y2="22"/>
      </svg>
      Voice
    </button>
    <button onclick="showView('search')">Search</button>
    <button onclick="showView('stats')">Library Stats</button>
    <button onclick="showView('map')">Map</button>
  </nav>
</header>

<div id="voiceStatusBar" style="display:none">
  <span id="micStatus" class="mic-status"></span>
</div>

<div class="search-bar" id="searchBar" style="display:none">
  <div class="search-row">
    <button class="filter-btn active" id="smartToggle" onclick="toggleSmart()"
            title="Smart Search uses AI to interpret your query">&#10024; Smart</button>
    <div class="search-input-wrap">
      <span class="search-icon" id="searchIcon">&#10024;</span>
      <input type="text" id="searchInput"
             placeholder="Smart Search - describe what you are looking for..."
             onkeydown="if(event.key==='Enter') runSearch()">
    </div>
    <button class="search-btn" id="searchBtn" onclick="runSearch()">Search</button>
    <button class="reset-btn" onclick="resetFilters()">Reset</button>
    <button class="select-btn" id="selectBtn" onclick="toggleSelectMode()">Select</button>
  </div>
  <div class="filter-row">
    <span class="filter-row-label">Filters</span>
    <select id="yearFilter" onchange="doSearch()">
      <option value="">All years</option>
    </select>
    <input id="personFilter" list="personOptions" autocomplete="off"
           placeholder="All people" onchange="doSearch()">
    <datalist id="personOptions"></datalist>
    <select id="sceneFilter" onchange="doSearch()">
      <option value="">All scenes</option>
{{ tag_options|safe }}
    </select>
    <button class="filter-btn" id="gemmaBtn" onclick="toggleGemma()">AI Described</button>
  </div>
</div>
<div id="smartStatusBar" style="display:none;padding:0 32px 12px;background:var(--bg2);border-bottom:1px solid var(--border)">
  <span id="smartStatus" style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:0.1em;color:var(--text3)"></span>
</div>

<main id="voiceView">
  <div class="results-info" id="voiceResultsInfo" style="display:none">
    <div>Showing <span id="voiceResultCount">0</span> photos</div>
    <div id="voicePageInfo"></div>
  </div>
  <div id="voiceGrid" class="grid"></div>
  <div class="pagination" id="voicePagination"></div>
</main>

<main id="searchView" style="display:none">
  <div class="results-info" id="resultsInfo" style="display:none">
    <div>Showing <span id="resultCount">0</span> photos</div>
    <div id="pageInfo"></div>
  </div>
  <div id="photoGrid" class="grid"></div>
  <div class="pagination" id="pagination"></div>
</main>

<main id="statsView" style="display:none">
  <div id="statsContent"><div class="loading">Loading stats</div></div>
</main>

<main id="mapView" style="display:none;padding:0">
  <div style="display:flex;height:calc(100vh - 140px)">
    <div id="map" style="flex:1;height:100%"></div>
    <div id="mapPanel" class="map-panel">
      <div class="map-panel-head">
        <div class="map-panel-title" id="mapPanelTitle">Photos</div>
        <div class="map-panel-close" onclick="closeMapPanel()">&#10005;</div>
      </div>
      <div class="map-panel-grid" id="mapPanelGrid"></div>
    </div>
  </div>
</main>

<div class="modal-overlay" id="modal" onclick="closeModal(event)">
  <button class="modal-close" onclick="closeModalBtn()">&#10005;</button>
  <div class="modal" id="modalContent">
    <div class="modal-image-wrap">
      <img id="modalImg" src="" alt="">
      <video id="modalVid" controls preload="metadata" style="display:none"></video>
    </div>
    <div class="modal-info" id="modalInfo"></div>
  </div>
</div>

<div class="delete-bar" id="deleteBar">
  <div class="delete-bar-count"><span id="deleteCount">0</span> photo(s) selected</div>
  <button class="delete-bar-cancel" onclick="toggleSelectMode()">Cancel</button>
  <button class="delete-bar-confirm" id="deleteConfirmBtn" onclick="deleteSelected()">Delete</button>
</div>

<script src="/static/app.js?v={{ js_v }}"></script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"ERROR: config not found: {path}")
    cp = configparser.ConfigParser()
    cp.read(path)
    return {
        "db_path":      cp.get("paths", "db_path"),
        "dest_dir":     cp.get("paths", "dest_dir"),
        "ollama_url":   cp.get("ollama", "url", fallback="http://localhost:11434"),
        "ollama_model": cp.get("ollama", "model", fallback="gemma4:e4b"),
        "delete_token": cp.get("web", "delete_token", fallback=""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="photo-intel web UI")
    parser.add_argument("--config",
                        default=str(Path.home() / "photo-intel" / "photo-intel.conf"),
                        help="Path to photo-intel.conf")
    parser.add_argument("--db",   help="Override db_path from the config")
    parser.add_argument("--dest", help="Override dest_dir from the config")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to bind (0.0.0.0 = all interfaces)")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    global DB_PATH, DEST_DIR, THUMB_DIR, OLLAMA_URL, OLLAMA_MODEL, WRITE_TOKEN
    DB_PATH  = args.db   or cfg["db_path"]
    DEST_DIR = Path(args.dest or cfg["dest_dir"]).resolve()
    THUMB_DIR = DEST_DIR / ".thumb_cache"
    OLLAMA_URL   = cfg["ollama_url"]
    OLLAMA_MODEL = cfg["ollama_model"]
    WRITE_TOKEN = cfg["delete_token"]
    if not WRITE_TOKEN:
        print("  WARNING  : no [web] delete_token in config — photo delete "
              "and metadata editing are disabled.")

    if not Path(DB_PATH).exists():
        raise SystemExit(f"ERROR: database not found: {DB_PATH}")
    if not DEST_DIR.exists():
        raise SystemExit(f"ERROR: dest_dir not found: {DEST_DIR}")
    THUMB_DIR.mkdir(exist_ok=True)

    print("\nphoto-intel web UI")
    print(f"  Database : {DB_PATH}")
    print(f"  Photos   : {DEST_DIR}")
    print(f"  Thumbs   : {THUMB_DIR}")
    if not HEIF_OK:
        print("  WARNING  : pillow-heif not installed — HEIC thumbnails will fail.")
    # Tombstone table. Phase 1 owns the canonical definition (its SCHEMA runs
    # on every open), but /api/delete must not fail just because this app
    # happened to start first on a fresh database.
    _c = get_write_db()
    _c.execute("""CREATE TABLE IF NOT EXISTS suppressed (
                      uuid   TEXT PRIMARY KEY,
                      at     TEXT,
                      reason TEXT
                  )""")
    _c.commit()
    _n_sup = _c.execute("SELECT COUNT(*) FROM suppressed").fetchone()[0]
    _c.close()
    if _n_sup:
        print(f"  Suppressed: {_n_sup} tombstoned uuid(s)")

    print("  Indexing photo files ...", flush=True)

    global _missing_uuids
    idx = build_file_index()
    missing = compute_missing_uuids(idx)
    with _file_index_lock:
        _file_index.update(idx)
        _missing_uuids = missing
    print(f"  Indexed {len(_file_index):,} files", flush=True)
    if missing:
        print(f"  {len(missing)} DB row(s) have no local file — hidden from search", flush=True)

    t = threading.Thread(target=refresh_index_loop, daemon=True)
    t.start()

    print(f"  URL      : http://processing-host:{args.port}")
    print(f"  Local    : http://localhost:{args.port}")
    print("\nCtrl+C to stop\n")

    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8)
    except ImportError:
        # Flask dev server fallback — fine on the LAN, but long thumbnail
        # generations can stall other requests. `pip install waitress`
        # in the venv to use the production server.
        app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
