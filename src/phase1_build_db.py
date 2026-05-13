#!/usr/bin/env python3
"""
phase1_build_db.py — Build photo metadata SQLite database
Combines: Apple Photos.sqlite + EXIF (via exiftool) + Apple Vision framework

Tested against a ~218,000 asset library (191K photos, 27K videos):
  - File path layout: originals/<first-char-UUID>/<UUID>.<ext>
  - Album join table: Z_<N>ASSETS where N varies by library (probed at runtime)
  - Scene labels: ZSCENECLASSIFICATION with ZSCENEIDENTIFIER integer map
  - ZASSETDESCRIPTION may contain Apple-generated AI descriptions for some photos

Prerequisites:
    pip3 install pyobjc-framework-Vision pyobjc-framework-Quartz pillow tqdm
    brew install exiftool

Usage:
    python3 phase1_build_db.py --library "/path/to/Photos Library.photoslibrary"
    python3 phase1_build_db.py --library "..." --db ~/photos_meta.db --resume
    python3 phase1_build_db.py --library "..." --limit 100      # test run
    python3 phase1_build_db.py --library "..." --skip-vision    # faster, Apple DB + EXIF only
    python3 phase1_build_db.py --library "..." --tmp-dir /path  # override temp file location
"""

import sqlite3
import argparse
import os
import sys
import shutil
import tempfile
import subprocess
import json
import time
import traceback
from pathlib import Path
from datetime import datetime

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:
        def __init__(self, iterable=None, total=None, **kw):
            self._it = iterable or []; self.n = 0
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def __iter__(self):
            for x in self._it: yield x
        def update(self, n=1): self.n += n
        def set_postfix(self, **kw): pass
        def close(self): pass

APPLE_EPOCH = 978307200

def apple_ts(val):
    if val is None: return None
    try:
        return datetime.fromtimestamp(float(val) + APPLE_EPOCH).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────────────────────
# APPLE SCENE IDENTIFIER MAP
# Reverse-engineered from Apple Vision framework. Top identifiers in a typical
# library: 2147482365=outdoor, 2147483644=nature, 881=water, etc.
# ─────────────────────────────────────────────────────────────────────────────

SCENE_LABELS = {
    2147482365: "outdoor",
    2147483644: "nature",
    2147483639: "sky",
    2147483645: "landscape",
    2147483641: "person",
    -2147483641:"person",
    2147482623: "indoor",
    2147481598: "night",
    2147482365: "outdoor",
    881:   "water",
    1309:  "tree",
    1600:  "plant",
    14672: "building",
    12810: "road",
    18451: "food",
    13582: "vehicle",
    1222:  "beach",
    13354: "mountain",
    784:   "flower",
    13872: "city",
    13666: "architecture",
    1761:  "sunset",
    1760:  "sunrise",
    10335: "cloud",
    229:   "grass",
    382:   "dog",
    383:   "cat",
    17199: "snow",
    11060: "forest",
    546:   "bird",
    1733:  "ocean",
    10382: "people",
    636:   "sport",
    715:   "music",
    3483:  "concert",
    374:   "party",
    11844: "travel",
    30:    "family",
    10089: "portrait",
    244:   "food_drink",
    397:   "restaurant",
    38:    "selfie",
    1101:  "stadium",
    1102:  "arena",
    1103:  "field",
    1100:  "court",
    1004:  "celebration",
    1001:  "wedding",
    1003:  "holiday",
    1000:  "birthday",
    1200:  "graduation",
    1201:  "ceremony",
    1300:  "document",
    1400:  "receipt",
    1401:  "text",
    2000:  "screenshot",
    533:   "animal",
    # Additional identified from library analysis
    18219: "monument",
    2147483655: "macro",
    2147482063: "people",
    2147482366: "landscape",
    2147482079: "architecture",
}

def scene_id_to_label(sid):
    return SCENE_LABELS.get(sid, f"scene_{sid}")

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SCHEMA
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    photos_uuid             TEXT UNIQUE,
    filename                TEXT,
    original_filename       TEXT,
    file_path               TEXT,

    date_created            TEXT,
    date_modified           TEXT,
    date_exif               TEXT,
    year                    INTEGER,
    month                   INTEGER,
    day                     INTEGER,
    timezone_name           TEXT,

    latitude                REAL,
    longitude               REAL,
    altitude                REAL,
    gps_source              TEXT,
    gps_accuracy            REAL,

    kind                    INTEGER,
    duration                REAL,
    width                   INTEGER,
    height                  INTEGER,
    orientation             INTEGER,
    file_size               INTEGER,
    uniform_type            TEXT,

    camera_make             TEXT,
    camera_model            TEXT,
    lens_model              TEXT,
    focal_length            REAL,
    aperture                REAL,
    shutter_speed           TEXT,
    iso                     INTEGER,
    flash                   TEXT,
    color_space             TEXT,

    is_favorite             INTEGER DEFAULT 0,
    is_hidden               INTEGER DEFAULT 0,
    is_trashed              INTEGER DEFAULT 0,
    is_screenshot           INTEGER DEFAULT 0,
    hdr_type                INTEGER,
    has_adjustments         INTEGER DEFAULT 0,
    aesthetic_score         REAL,
    curation_score          REAL,
    iconic_score            REAL,

    apple_scene_labels      TEXT,
    apple_scene_top         TEXT,
    apple_description       TEXT,
    accessibility_description TEXT,

    vision_classifications  TEXT,
    vision_face_count       INTEGER,
    vision_text_detected    INTEGER DEFAULT 0,
    vision_text_content     TEXT,
    vision_attention_x      REAL,
    vision_attention_y      REAL,

    named_people            TEXT,
    face_count_apple        INTEGER,
    albums                  TEXT,

    gemma_description       TEXT,
    gemma_tags              TEXT,
    gemma_location_guess    TEXT,
    gemma_processed         INTEGER DEFAULT 0,
    gemma_processed_at      TEXT,

    phase1_processed_at     TEXT,
    phase1_error            TEXT
);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    photos_uuid TEXT UNIQUE,
    full_name   TEXT,
    face_count  INTEGER
);

CREATE TABLE IF NOT EXISTS albums (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    photos_uuid TEXT UNIQUE,
    title       TEXT,
    kind        INTEGER,
    asset_count INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
    filename, original_filename, date_created, timezone_name,
    camera_make, camera_model,
    apple_scene_labels, apple_scene_top,
    apple_description, accessibility_description,
    vision_classifications, vision_text_content,
    named_people, albums,
    gemma_description, gemma_tags, gemma_location_guess,
    content='photos', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS photos_ai AFTER INSERT ON photos BEGIN
    INSERT INTO photos_fts(rowid,
        filename, original_filename, date_created, timezone_name,
        camera_make, camera_model, apple_scene_labels, apple_scene_top,
        apple_description, accessibility_description,
        vision_classifications, vision_text_content,
        named_people, albums, gemma_description, gemma_tags, gemma_location_guess)
    VALUES (new.id,
        new.filename, new.original_filename, new.date_created, new.timezone_name,
        new.camera_make, new.camera_model, new.apple_scene_labels, new.apple_scene_top,
        new.apple_description, new.accessibility_description,
        new.vision_classifications, new.vision_text_content,
        new.named_people, new.albums, new.gemma_description, new.gemma_tags,
        new.gemma_location_guess);
END;

CREATE TRIGGER IF NOT EXISTS photos_au AFTER UPDATE ON photos BEGIN
    INSERT INTO photos_fts(photos_fts, rowid,
        filename, original_filename, date_created, timezone_name,
        camera_make, camera_model, apple_scene_labels, apple_scene_top,
        apple_description, accessibility_description,
        vision_classifications, vision_text_content,
        named_people, albums, gemma_description, gemma_tags, gemma_location_guess)
    VALUES ('delete', old.id,
        old.filename, old.original_filename, old.date_created, old.timezone_name,
        old.camera_make, old.camera_model, old.apple_scene_labels, old.apple_scene_top,
        old.apple_description, old.accessibility_description,
        old.vision_classifications, old.vision_text_content,
        old.named_people, old.albums, old.gemma_description, old.gemma_tags,
        old.gemma_location_guess);
    INSERT INTO photos_fts(rowid,
        filename, original_filename, date_created, timezone_name,
        camera_make, camera_model, apple_scene_labels, apple_scene_top,
        apple_description, accessibility_description,
        vision_classifications, vision_text_content,
        named_people, albums, gemma_description, gemma_tags, gemma_location_guess)
    VALUES (new.id,
        new.filename, new.original_filename, new.date_created, new.timezone_name,
        new.camera_make, new.camera_model, new.apple_scene_labels, new.apple_scene_top,
        new.apple_description, new.accessibility_description,
        new.vision_classifications, new.vision_text_content,
        new.named_people, new.albums, new.gemma_description, new.gemma_tags,
        new.gemma_location_guess);
END;
"""

# ─────────────────────────────────────────────────────────────────────────────
# PHOTOS DB READER
# ─────────────────────────────────────────────────────────────────────────────

class PhotosDB:
    def __init__(self, library_path: Path, tmp_dir: Path = None):
        db_src = library_path / "database" / "Photos.sqlite"
        if not db_src.exists():
            raise FileNotFoundError(f"Not found: {db_src}")
        # Default temp location is the system temp dir. For large libraries on
        # external volumes, pass tmp_dir pointing to that same volume — the
        # Photos.sqlite copy + WAL files can be many GB and may fill your boot
        # drive otherwise.
        if tmp_dir is not None:
            tmp_base = Path(tmp_dir)
            tmp_base.mkdir(parents=True, exist_ok=True)
            self._tmp = Path(tempfile.mkdtemp(prefix="photosdb_", dir=tmp_base))
        else:
            self._tmp = Path(tempfile.mkdtemp(prefix="photosdb_"))
        dst = self._tmp / "Photos.sqlite"
        shutil.copy2(db_src, dst)
        for ext in ["-wal", "-shm"]:
            s = Path(str(db_src) + ext)
            if s.exists(): shutil.copy2(s, Path(str(dst) + ext))
        self.conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.library_path = library_path
        print(f"  Photos.sqlite opened (temp copy)")

    def get_assets(self):
        return self.conn.execute("""
            SELECT
                a.Z_PK, a.ZUUID, a.ZDATECREATED, a.ZMODIFICATIONDATE,
                a.ZLATITUDE, a.ZLONGITUDE, a.ZKIND, a.ZDURATION,
                a.ZFAVORITE, a.ZHIDDEN, a.ZTRASHEDSTATE,
                a.ZWIDTH, a.ZHEIGHT, a.ZORIENTATION,
                a.ZUNIFORMTYPEIDENTIFIER,
                a.ZHDRTYPE, a.ZADJUSTMENTSSTATE,
                a.ZOVERALLAESTHETICSCORE, a.ZCURATIONSCORE, a.ZICONICSCORE,
                a.ZISDETECTEDSCREENSHOT, a.ZFILENAME, a.ZDIRECTORY,
                aa.ZORIGINALFILENAME, aa.ZORIGINALFILESIZE,
                aa.ZGPSHORIZONTALACCURACY, aa.ZTIMEZONENAME,
                aa.ZACCESSIBILITYDESCRIPTION, aa.ZEXIFTIMESTAMPSTRING,
                d.ZLONGDESCRIPTION as ZAPPLE_DESCRIPTION
            FROM ZASSET a
            LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
            LEFT JOIN ZASSETDESCRIPTION d ON d.Z_PK = aa.ZASSETDESCRIPTION
            WHERE a.ZTRASHEDSTATE = 0
            ORDER BY a.ZCLOUDLOCALSTATE DESC, a.Z_PK
        """).fetchall()

    def get_scene_labels(self):
        print("  Loading scene classifications (~30s for 6.9M rows)...")
        cur = self.conn.execute("""
            SELECT aa.ZASSET, sc.ZSCENEIDENTIFIER, sc.ZCONFIDENCE
            FROM ZSCENECLASSIFICATION sc
            JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.Z_PK = sc.ZASSETATTRIBUTES
            WHERE sc.ZCONFIDENCE >= 0.5
              AND sc.ZCLASSIFICATIONTYPE = 0
            ORDER BY aa.ZASSET, sc.ZCONFIDENCE DESC
        """)
        result = {}
        for asset_pk, scene_id, conf in cur.fetchall():
            result.setdefault(asset_pk, [])
            if len(result[asset_pk]) < 8:
                result[asset_pk].append({
                    "label": scene_id_to_label(scene_id),
                    "confidence": round(conf, 3)
                })
        print(f"  Scene labels loaded for {len(result):,} assets")
        return result

    def get_faces_by_asset(self):
        print("  Loading face/person data...")
        cur = self.conn.execute("""
            SELECT df.ZASSETFORFACE, p.ZFULLNAME
            FROM ZDETECTEDFACE df
            JOIN ZPERSON p ON p.Z_PK = df.ZPERSONFORFACE
            WHERE p.ZFULLNAME IS NOT NULL AND p.ZFULLNAME != ''
              AND df.ZHIDDEN = 0 AND df.ZISINTRASH = 0
        """)
        result = {}
        for asset_pk, name in cur.fetchall():
            result.setdefault(asset_pk, [])
            if name not in result[asset_pk]:
                result[asset_pk].append(name)
        print(f"  {sum(len(v) for v in result.values()):,} face-name links across "
              f"{len(result):,} assets")
        return result

    def get_albums(self):
        print("  Loading album data...")
        albums = {}
        cur = self.conn.execute("""
            SELECT Z_PK, ZUUID, ZTITLE, ZKIND, ZCACHEDCOUNT
            FROM ZGENERICALBUM
            WHERE ZTITLE IS NOT NULL AND ZTITLE != ''
              AND ZTRASHEDSTATE = 0
        """)
        for row in cur.fetchall():
            albums[row[0]] = {"uuid": row[1], "title": row[2],
                              "kind": row[3], "count": row[4]}

        asset_albums = {}
        # Try known join table names in order
        for jt in ["Z_33ASSETS", "Z_32ASSETS", "Z_34ASSETS", "Z_31ASSETS", "Z_30ASSETS"]:
            try:
                cur = self.conn.execute(f"SELECT * FROM {jt} LIMIT 1")
                cols = [d[0] for d in cur.description]
                ac = next((c for c in cols if "ALBUM" in c.upper()), None)
                sc = next((c for c in cols if "ASSET" in c.upper()), None)
                if ac and sc:
                    for album_pk, asset_pk in self.conn.execute(
                            f"SELECT {ac}, {sc} FROM {jt}").fetchall():
                        asset_albums.setdefault(asset_pk, []).append(album_pk)
                    print(f"  {len(albums):,} albums, {len(asset_albums):,} "
                          f"asset memberships (via {jt})")
                    break
            except Exception:
                continue
        return albums, asset_albums

    def resolve_file_path(self, uuid: str, filename: str):
        """originals/<first-char>/<UUID>.<ext> — scan by UUID prefix, any extension."""
        if not uuid:
            return None
        prefix = uuid[0].upper()
        folder = self.library_path / "originals" / prefix
        if not folder.exists():
            return None
        # Try exact filename first
        if filename:
            p = folder / filename
            if p.exists(): return p
            p2 = folder / filename.lower()
            if p2.exists(): return p2
        # Fall back: find any file whose stem matches the UUID (extension may differ)
        uuid_upper = uuid.upper()
        uuid_lower = uuid.lower()
        for p in folder.iterdir():
            stem = p.stem.upper()
            if stem == uuid_upper or stem == uuid_lower:
                return p
        return None

    def close(self):
        self.conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

# ─────────────────────────────────────────────────────────────────────────────
# EXIFTOOL
# ─────────────────────────────────────────────────────────────────────────────

def run_exiftool(file_path: Path) -> dict:
    try:
        r = subprocess.run(
            ["exiftool", "-json", "-n", "-q", str(file_path)],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            return data[0] if data else {}
    except Exception:
        pass
    return {}

def extract_exif(exif: dict) -> dict:
    def g(*keys):
        for k in keys:
            v = exif.get(k)
            if v is not None: return v
        return None
    return {
        "date_exif"    : g("DateTimeOriginal","CreateDate"),
        "latitude"     : g("GPSLatitude"),
        "longitude"    : g("GPSLongitude"),
        "altitude"     : g("GPSAltitude"),
        "file_size"    : g("FileSize"),
        "camera_make"  : g("Make"),
        "camera_model" : g("Model"),
        "lens_model"   : g("LensModel","LensID"),
        "focal_length" : g("FocalLength"),
        "aperture"     : g("FNumber","Aperture"),
        "shutter_speed": str(g("ExposureTime","ShutterSpeed") or ""),
        "iso"          : g("ISO"),
        "flash"        : str(g("Flash") or ""),
        "color_space"  : g("ColorSpaceName","ColorSpace"),
    }

# ─────────────────────────────────────────────────────────────────────────────
# APPLE VISION FRAMEWORK
# ─────────────────────────────────────────────────────────────────────────────

def run_vision(file_path: Path) -> dict:
    result = {"classifications": [], "face_count": 0,
              "text_detected": False, "text_content": None,
              "attention_x": None, "attention_y": None}
    try:
        import Vision
        from Foundation import NSURL
        url     = NSURL.fileURLWithPath_(str(file_path))
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
        class_req = Vision.VNClassifyImageRequest.alloc().init()
        face_req  = Vision.VNDetectFaceRectanglesRequest.alloc().init()
        text_req  = Vision.VNRecognizeTextRequest.alloc().init()
        text_req.setRecognitionLevel_(1)
        sal_req   = Vision.VNGenerateAttentionBasedSaliencyImageRequest.alloc().init()
        ok, _ = handler.performRequests_error_(
            [class_req, face_req, text_req, sal_req], None)
        if ok:
            if class_req.results():
                result["classifications"] = [
                    {"label": o.identifier(), "confidence": round(o.confidence(), 3)}
                    for o in class_req.results() if o.confidence() >= 0.05
                ]
            if face_req.results():
                result["face_count"] = len(face_req.results())
            if text_req.results():
                texts = [obs.topCandidates_(1)[0].string()
                         for obs in text_req.results()
                         if obs.topCandidates_(1) and obs.topCandidates_(1)[0].string()]
                if texts:
                    result["text_detected"] = True
                    result["text_content"]  = "\n".join(texts)[:2000]
            if sal_req.results():
                sal = sal_req.results()[0].salientObjects()
                if sal:
                    b = sal[0].boundingBox()
                    result["attention_x"] = round(b.origin.x + b.size.width/2,  3)
                    result["attention_y"] = round(b.origin.y + b.size.height/2, 3)
    except ImportError:
        pass
    except Exception as e:
        result["_error"] = str(e)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.commit()
    return conn

def already_done(conn, uuid):
    cur = conn.execute(
        "SELECT id FROM photos WHERE photos_uuid=? AND phase1_processed_at IS NOT NULL",
        (uuid,))
    return cur.fetchone() is not None

def upsert(conn, data: dict):
    cols = list(data.keys())
    conn.execute(
        f"INSERT INTO photos ({', '.join(cols)}) VALUES ({', '.join('?'*len(cols))}) "
        f"ON CONFLICT(photos_uuid) DO UPDATE SET "
        f"{', '.join(f'{c}=excluded.{c}' for c in cols if c != 'photos_uuid')}",
        list(data.values())
    )

# ─────────────────────────────────────────────────────────────────────────────
# PROCESS ONE ASSET
# ─────────────────────────────────────────────────────────────────────────────

def process_asset(row, library_path, photos_db,
                  scene_map, faces_map, albums_map, asset_albums, args) -> dict:
    r    = dict(row)
    pk   = r["Z_PK"]
    uuid = r["ZUUID"]
    fn   = r.get("ZFILENAME") or ""
    kind = r.get("ZKIND", 0)
    lat  = r.get("ZLATITUDE")
    lon  = r.get("ZLONGITUDE")
    if lat == -180.0: lat = None
    if lon == -180.0: lon = None

    dc = apple_ts(r.get("ZDATECREATED"))
    year = month = day = None
    if dc:
        try:
            dt = datetime.strptime(dc, "%Y-%m-%d %H:%M:%S")
            year, month, day = dt.year, dt.month, dt.day
        except Exception: pass

    scenes     = scene_map.get(pk, [])
    scene_top  = scenes[0]["label"] if scenes else None
    named      = faces_map.get(pk, [])
    album_pks  = asset_albums.get(pk, [])
    album_names= [albums_map[a]["title"] for a in album_pks if a in albums_map]
    file_path  = photos_db.resolve_file_path(uuid, fn)

    data = {
        "photos_uuid"           : uuid,
        "filename"              : fn,
        "original_filename"     : r.get("ZORIGINALFILENAME"),
        "file_path"             : str(file_path) if file_path else None,
        "date_created"          : dc,
        "date_modified"         : apple_ts(r.get("ZMODIFICATIONDATE")),
        "date_exif"             : r.get("ZEXIFTIMESTAMPSTRING"),
        "year"                  : year,
        "month"                 : month,
        "day"                   : day,
        "timezone_name"         : r.get("ZTIMEZONENAME"),
        "latitude"              : lat,
        "longitude"             : lon,
        "gps_source"            : "apple" if lat else None,
        "gps_accuracy"          : r.get("ZGPSHORIZONTALACCURACY"),
        "kind"                  : kind,
        "duration"              : r.get("ZDURATION"),
        "width"                 : r.get("ZWIDTH"),
        "height"                : r.get("ZHEIGHT"),
        "orientation"           : r.get("ZORIENTATION"),
        "file_size"             : r.get("ZORIGINALFILESIZE"),
        "uniform_type"          : r.get("ZUNIFORMTYPEIDENTIFIER"),
        "is_favorite"           : r.get("ZFAVORITE", 0),
        "is_hidden"             : r.get("ZHIDDEN", 0),
        "is_trashed"            : r.get("ZTRASHEDSTATE", 0),
        "is_screenshot"         : r.get("ZISDETECTEDSCREENSHOT", 0),
        "hdr_type"              : r.get("ZHDRTYPE"),
        "has_adjustments"       : 1 if r.get("ZADJUSTMENTSSTATE", 0) else 0,
        "aesthetic_score"       : r.get("ZOVERALLAESTHETICSCORE"),
        "curation_score"        : r.get("ZCURATIONSCORE"),
        "iconic_score"          : r.get("ZICONICSCORE"),
        "apple_scene_labels"    : json.dumps(scenes) if scenes else None,
        "apple_scene_top"       : scene_top,
        "apple_description"     : r.get("ZAPPLE_DESCRIPTION"),
        "accessibility_description": r.get("ZACCESSIBILITYDESCRIPTION"),
        "named_people"          : json.dumps(named) if named else None,
        "face_count_apple"      : len(named) if named else None,
        "albums"                : json.dumps(album_names) if album_names else None,
        "phase1_processed_at"   : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # ── EXIF ─────────────────────────────────────────────────────────────────
    if not args.skip_exif and file_path and file_path.exists():
        exif = run_exiftool(file_path)
        ex   = extract_exif(exif)
        data.update({k: v for k, v in ex.items() if v})
        ex_lat, ex_lon = ex.get("latitude"), ex.get("longitude")
        if ex_lat and ex_lon:
            if not lat:
                data.update({"latitude": ex_lat, "longitude": ex_lon,
                             "altitude": ex.get("altitude"), "gps_source": "exif"})
            else:
                data["gps_source"] = "both"

    # ── Apple Vision ─────────────────────────────────────────────────────────
    if not args.skip_vision and file_path and file_path.exists() and kind == 0:
        vis = run_vision(file_path)
        data.update({
            "vision_classifications": json.dumps(vis["classifications"]) if vis["classifications"] else None,
            "vision_face_count"     : vis["face_count"] or None,
            "vision_text_detected"  : 1 if vis["text_detected"] else 0,
            "vision_text_content"   : vis.get("text_content"),
            "vision_attention_x"    : vis.get("attention_x"),
            "vision_attention_y"    : vis.get("attention_y"),
        })
        if vis.get("_error"):
            data["phase1_error"] = vis["_error"]

    return data

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Phase 1: Build photo metadata DB")
    parser.add_argument("--library", required=True,
                        help='Path to Photos Library, e.g. "~/Pictures/Photos Library.photoslibrary"')
    parser.add_argument("--db",          default=str(Path.home() / "photos_meta.db"))
    parser.add_argument("--tmp-dir",     default=None,
                        help="Directory for temp Photos.sqlite copy (defaults to system temp). "
                             "If your library is on an external volume, point this to that volume "
                             "to avoid filling the boot drive.")
    parser.add_argument("--resume",      action="store_true", help="Skip already-processed")
    parser.add_argument("--limit",       type=int,            help="Test: process only N assets")
    parser.add_argument("--skip-vision", action="store_true", help="Skip Vision.framework")
    parser.add_argument("--skip-exif",   action="store_true", help="Skip exiftool")
    parser.add_argument("--photos-only", action="store_true", help="Skip videos")
    args = parser.parse_args()

    library_path = Path(args.library)
    if not library_path.exists():
        sys.exit(f"ERROR: Library not found: {library_path}")

    print(f"\nLibrary : {library_path}")
    print(f"Database: {args.db}")

    if not args.skip_exif and shutil.which("exiftool") is None:
        print("WARNING: exiftool not found — skipping. Install: brew install exiftool")
        args.skip_exif = True

    print("\nOpening Photos.sqlite...")
    db = PhotosDB(library_path, tmp_dir=Path(args.tmp_dir) if args.tmp_dir else None)

    scene_map             = db.get_scene_labels()
    faces_map             = db.get_faces_by_asset()
    albums_map, asset_alb = db.get_albums()

    print("\nLoading asset list...")
    assets = db.get_assets()
    if args.photos_only:
        assets = [a for a in assets if dict(a).get("ZKIND", 0) == 0]
    if args.limit:
        assets = assets[:args.limit]
    print(f"  {len(assets):,} assets to process\n")

    conn  = init_db(Path(args.db))
    stats = {"ok": 0, "skipped": 0, "error": 0, "no_file": 0}
    start = time.time()

    with tqdm(total=len(assets), unit="asset") as pbar:
        for asset in assets:
            uuid = dict(asset).get("ZUUID")
            if args.resume and uuid and already_done(conn, uuid):
                stats["skipped"] += 1
                pbar.update(1)
                continue
            try:
                data = process_asset(asset, library_path, db,
                                     scene_map, faces_map, albums_map, asset_alb, args)
                if not data.get("file_path"):
                    stats["no_file"] += 1
                upsert(conn, data)
                conn.commit()
                stats["ok"] += 1
            except Exception as e:
                stats["error"] += 1
                print(f"\nERROR {uuid}: {e}")
                if args.limit:
                    traceback.print_exc()
            pbar.update(1)
            pbar.set_postfix(ok=stats["ok"], err=stats["error"],
                             skip=stats["skipped"], no_file=stats["no_file"])

    # Albums table
    print("\nPopulating albums table...")
    for apk, album in albums_map.items():
        ac = sum(1 for v in asset_alb.values() if apk in v)
        conn.execute(
            "INSERT OR REPLACE INTO albums (photos_uuid, title, kind, asset_count) VALUES (?,?,?,?)",
            (album.get("uuid"), album.get("title"), album.get("kind"), ac))
    conn.commit()

    elapsed = time.time() - start
    ok = stats["ok"]
    print(f"\n{'='*55}")
    print(f"Phase 1 complete — {elapsed:.0f}s  ({elapsed/max(ok,1):.2f}s/asset)")
    print(f"  Processed  : {ok:,}")
    print(f"  Skipped    : {stats['skipped']:,}  (--resume)")
    print(f"  No file    : {stats['no_file']:,}  (not downloaded from iCloud)")
    print(f"  Errors     : {stats['error']:,}")

    # Sanity counts
    for label, sql in [
        ("Rows in DB",             "SELECT COUNT(*) FROM photos"),
        ("With scene labels",      "SELECT COUNT(*) FROM photos WHERE apple_scene_top IS NOT NULL"),
        ("With Apple description", "SELECT COUNT(*) FROM photos WHERE apple_description IS NOT NULL"),
        ("With named people",      "SELECT COUNT(*) FROM photos WHERE named_people IS NOT NULL"),
        ("With GPS",               "SELECT COUNT(*) FROM photos WHERE latitude IS NOT NULL"),
        ("No file on disk",        "SELECT COUNT(*) FROM photos WHERE file_path IS NULL"),
    ]:
        n = conn.execute(sql).fetchone()[0]
        print(f"  {label:<28}: {n:,}")

    print(f"\nNext: python3 phase2_gemma.py --db {args.db}")
    db.close()
    conn.close()

if __name__ == "__main__":
    main()
