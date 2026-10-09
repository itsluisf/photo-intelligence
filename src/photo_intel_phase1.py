#!/usr/bin/env python3
"""
photo_intel_phase1.py
Polls dest_dir on the processing host for osxphotos JSON sidecars and ingests them
into photo-intel.db.  Runs continuously; safe to restart at any time.

Run on: the processing host
Usage:
    python3 photo_intel_phase1.py [--config PATH] [--interval SECONDS] [--once]

    --config    path to photo-intel.conf (default: ./photo-intel.conf)
    --interval  polling interval in seconds (default: 300)
    --once      ingest once and exit (useful for testing)
"""

import argparse
import configparser
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import photo_intel_names as names_mod

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("photo_intel_phase1")

# Person-name aliases (photo_intel_names.py). Loaded once per run rather than
# per photo: a run processes tens of thousands of sidecars, and the map is
# static for the duration. No person_aliases.json means no rewriting.
_ALIASES = None


def person_aliases() -> dict:
    global _ALIASES
    if _ALIASES is None:
        _ALIASES = names_mod.load_aliases()
        if _ALIASES:
            log.info("person aliases loaded: %d", len(_ALIASES))
    return _ALIASES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
UUID_RE = re.compile(
    r"^([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})",
    re.IGNORECASE,
)

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp"}

# Extension aliases — collapse equivalent spellings to one canonical form.
# Keeps file_ext stable across import sources so downstream filters work.
EXT_ALIASES = {
    "JPEG": "JPG",
    # Future-proofing slots: enable if/when a future import surfaces them.
    # "TIFF": "TIF",
    # "HEIF": "HEIC",
}


def normalize_ext(ext: str) -> str:
    """Normalize an extension to its canonical spelling (uppercase, no dot)."""
    norm = ext.lstrip(".").upper()
    return EXT_ALIASES.get(norm, norm)


# QuickTime datetime parsing for video sidecars.
# Format: "1980:01:02 12:30:57-05:00" or "1980:01:02 17:30:57" (UTC, no offset).
QT_DT_RE = re.compile(
    r"^(\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2})([+-]\d{2}:\d{2})?$"
)
QT_DATE_KEYS = (
    "QuickTime:CreationDate",       # local wall time + offset (preferred)
    "QuickTime:ContentCreateDate",  # same, fallback
    "QuickTime:CreateDate",         # UTC, no offset — last resort
)

# QuickTime GPS parsing for video sidecars — the same problem as the dates
# above. Videos carry no EXIF, so the EXIF:GPSLatitude/Longitude read below
# silently dropped the coordinates of every video in the library (12,978 rows
# with gps_lat NULL, found 2026-08-01). Values here are already signed, so
# there is no GPSLatitudeRef hemisphere correction to apply.
QT_GPS_KEYS = ("Keys:GPSCoordinates", "UserData:GPSCoordinates")

# "12.3456 -78.9012", optionally with a trailing altitude.
QT_GPS_PLAIN_RE = re.compile(
    r"^\s*([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)(?:\s+[+-]?\d+(?:\.\d+)?)?\s*$"
)
# ISO 6709: "+12.3456-078.9012+021.921/". Not present in this library's
# sidecars, but accepting it costs nothing and avoids losing a future import
# source the way the EXIF-only read did.
QT_GPS_ISO6709_RE = re.compile(
    r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?$"
)


def parse_qt_gps(rec: dict):
    """Pull (lat, lon) from a video sidecar's QuickTime GPS keys.

    Returns None when absent or unparseable. Kept in sync with the copy in
    photo_intel_places.py, which backfilled the pre-fix video rows.
    """
    for key in QT_GPS_KEYS:
        raw = rec.get(key)
        if not raw:
            continue
        text = str(raw).strip()

        match = QT_GPS_PLAIN_RE.match(text) or QT_GPS_ISO6709_RE.match(text)
        if not match:
            continue

        try:
            lat, lon = float(match.group(1)), float(match.group(2))
        except ValueError:
            continue
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            continue
        # 0,0 is the null-island sentinel some cameras write for "no fix".
        if lat == 0 and lon == 0:
            continue
        return lat, lon
    return None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    read = cfg.read(path)
    if not read:
        log.error("Config file not found: %s", path)
        sys.exit(1)
    return cfg

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    uuid                TEXT PRIMARY KEY,
    media_type          TEXT NOT NULL,          -- 'image' or 'video'
    file_ext            TEXT,                   -- e.g. 'HEIC', 'JPG', 'MOV'
    datetime_original   TEXT,                   -- ISO 8601 with offset
    date                TEXT,                   -- YYYY-MM-DD
    time                TEXT,                   -- HH:MM:SS
    tz_offset           TEXT,                   -- e.g. '-05:00'
    gps_lat             REAL,
    gps_lon             REAL,
    gps_lat_ref         TEXT,
    gps_lon_ref         TEXT,
    persons             TEXT,                   -- JSON array
    scene_labels        TEXT,                   -- JSON array
    face_regions        TEXT,                   -- JSON array of region dicts
    sidecar_path        TEXT,
    phase1_processed_at TEXT,
    phase2_processed    INTEGER DEFAULT 0,      -- 0=pending, 1=done, -1=skip
    phase2_processed_at TEXT,
    gemma_description   TEXT,
    gemma_tags          TEXT,                   -- JSON array
    gemma_location_guess TEXT
);

CREATE INDEX IF NOT EXISTS idx_photos_date       ON photos(date);
CREATE INDEX IF NOT EXISTS idx_photos_media_type ON photos(media_type);
CREATE INDEX IF NOT EXISTS idx_photos_phase2     ON photos(phase2_processed);

CREATE TABLE IF NOT EXISTS phase1_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT,
    files_seen  INTEGER,
    inserted    INTEGER,
    updated     INTEGER,
    skipped     INTEGER,
    errors      INTEGER
);

-- Tombstones for photos deleted from the web app. The pipeline is
-- additive-only: staging still holds the file, rsync re-delivers it, and this
-- ingest would re-insert the row on the next tick — so a delete without a
-- tombstone silently reverts within ~2 h. A uuid listed here is never
-- (re-)ingested and never shown. Cleared by photo_intel_reconcile.py once the
-- photo is genuinely gone from Apple Photos, so this table stays small.
CREATE TABLE IF NOT EXISTS suppressed (
    uuid   TEXT PRIMARY KEY,
    at     TEXT,                                -- ISO 8601, when suppressed
    reason TEXT                                 -- free text, e.g. 'web-delete'
);
"""

# Columns the other scripts write that the CREATE TABLE above predates. Added
# here, on every open, so a fresh install has the whole schema after its first
# Phase 1 run and an older database is brought up to date in place. Each is
# nullable, so ALTER never rewrites the table.
#   phase2_error   — Phase 2 / 2b record why a photo failed or was skipped
#   edited_fields  — the web editor's list of hand-corrected columns, which
#                    Phase 2 / 2b leave alone
#   place_*        — photo_intel_places.py (mirrors its PLACE_COLUMNS; its
#                    --migrate adds the same columns and is a no-op after this)
ADDED_COLUMNS = (
    ("phase2_error",       "TEXT"),
    ("edited_fields",      "TEXT"),
    ("place_name",         "TEXT"),
    ("place_aoi",          "TEXT"),
    ("place_city",         "TEXT"),
    ("place_state",        "TEXT"),
    ("place_country",      "TEXT"),
    ("place_country_code", "TEXT"),
    ("place_source",       "TEXT"),
    ("place_updated_at",   "TEXT"),
)

# Full-text index the web app searches. External-content FTS5 over `photos`,
# kept in sync by triggers, so every writer's UPDATE reindexes for free. Same
# definition as migrations/2026-08-01-fts-add-place-name.sql, which remains the
# way to upgrade an index built before place_name existed; IF NOT EXISTS leaves
# any existing index and triggers exactly as they are.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
    uuid UNINDEXED,
    gemma_description,
    gemma_tags,
    persons,
    gemma_location_guess,
    place_name,
    content='photos',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS photos_fts_insert AFTER INSERT ON photos BEGIN
    INSERT INTO photos_fts(rowid, uuid, gemma_description, gemma_tags, persons, gemma_location_guess, place_name)
    VALUES (new.rowid, new.uuid, new.gemma_description, new.gemma_tags, new.persons, new.gemma_location_guess, new.place_name);
END;

CREATE TRIGGER IF NOT EXISTS photos_fts_update AFTER UPDATE ON photos BEGIN
    INSERT INTO photos_fts(photos_fts, rowid, uuid, gemma_description, gemma_tags, persons, gemma_location_guess, place_name)
    VALUES ('delete', old.rowid, old.uuid, old.gemma_description, old.gemma_tags, old.persons, old.gemma_location_guess, old.place_name);
    INSERT INTO photos_fts(rowid, uuid, gemma_description, gemma_tags, persons, gemma_location_guess, place_name)
    VALUES (new.rowid, new.uuid, new.gemma_description, new.gemma_tags, new.persons, new.gemma_location_guess, new.place_name);
END;

CREATE TRIGGER IF NOT EXISTS photos_fts_delete AFTER DELETE ON photos BEGIN
    INSERT INTO photos_fts(photos_fts, rowid, uuid, gemma_description, gemma_tags, persons, gemma_location_guess, place_name)
    VALUES ('delete', old.rowid, old.uuid, old.gemma_description, old.gemma_tags, old.persons, old.gemma_location_guess, old.place_name);
END;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring any database up to the full schema. Idempotent and cheap: two
    PRAGMA/sqlite_master reads when nothing is missing."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(photos)")}
    for col, coltype in ADDED_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {coltype}")
            log.info("schema: added column photos.%s", col)

    had_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='photos_fts'").fetchone()
    conn.executescript(FTS_SCHEMA)
    if not had_fts:
        # A database ingested before the index existed: index what is there.
        conn.execute("INSERT INTO photos_fts(photos_fts) VALUES('rebuild')")
        log.info("schema: created photos_fts and indexed existing rows")
    conn.commit()


def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    ensure_schema(conn)
    return conn


def already_ingested(conn: sqlite3.Connection, uuid: str) -> bool:
    row = conn.execute(
        "SELECT phase1_processed_at FROM photos WHERE uuid = ?", (uuid,)
    ).fetchone()
    return row is not None and row["phase1_processed_at"] is not None


# ---------------------------------------------------------------------------
# Sidecar parsing
# ---------------------------------------------------------------------------
def parse_sidecar(json_path: Path) -> dict | None:
    """
    Parse one osxphotos JSON sidecar. Returns a dict ready for DB insert,
    or None if the file should be skipped.
    """
    # UUID from filename: {uuid}.EXT.json
    stem = json_path.stem          # e.g. "ABC123.HEIC"
    file_ext = normalize_ext(Path(stem).suffix)        # e.g. "HEIC"; JPEG → JPG
    base_stem = Path(stem).stem    # e.g. "ABC123..."

    m = UUID_RE.match(base_stem)
    if not m:
        log.debug("No UUID in filename: %s", json_path.name)
        return None
    uuid = m.group(1).upper()

    media_type = "video" if f".{file_ext.lower()}" in VIDEO_EXTENSIONS else "image"

    try:
        raw = json_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        log.warning("Failed to parse %s: %s", json_path, e)
        return None

    # osxphotos sidecars are a JSON array with one element
    if isinstance(data, list):
        if not data:
            return None
        rec = data[0]
    else:
        rec = data

    # --- datetime ---
    # Images carry EXIF:DateTimeOriginal + EXIF:OffsetTime*.
    # Videos carry no EXIF; fall back to QuickTime:* fields, which embed
    # the TZ offset inside the datetime string itself (e.g.
    # "1980:01:02 12:30:57-05:00") rather than as a separate field.
    # Order matters: CreationDate / ContentCreateDate carry the local
    # wall time with offset; CreateDate is UTC without offset and can
    # cross the calendar boundary, so it is the last resort.
    dt_str = rec.get("EXIF:DateTimeOriginal") or rec.get("EXIF:CreateDate")
    tz_offset = rec.get("EXIF:OffsetTimeOriginal") or rec.get("EXIF:OffsetTime")

    if not dt_str:
        for qt_key in QT_DATE_KEYS:
            qt_val = rec.get(qt_key)
            if not qt_val:
                continue
            qt_m = QT_DT_RE.match(qt_val)
            if qt_m:
                dt_str = qt_m.group(1)
                if qt_m.group(2) and not tz_offset:
                    tz_offset = qt_m.group(2)
                break

    date = time_ = datetime_original = None
    if dt_str:
        try:
            # Format: "2023:01:30 13:39:55"
            dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
            date = dt.strftime("%Y-%m-%d")
            time_ = dt.strftime("%H:%M:%S")
            if tz_offset:
                datetime_original = f"{date}T{time_}{tz_offset}"
            else:
                datetime_original = f"{date}T{time_}"
        except ValueError:
            pass

    # --- GPS ---
    gps_lat = rec.get("EXIF:GPSLatitude")
    gps_lon = rec.get("EXIF:GPSLongitude")
    gps_lat_ref = rec.get("EXIF:GPSLatitudeRef")
    gps_lon_ref = rec.get("EXIF:GPSLongitudeRef")

    # Apply hemisphere sign if not already negative
    if gps_lat is not None and gps_lat_ref == "S" and gps_lat > 0:
        gps_lat = -gps_lat
    if gps_lon is not None and gps_lon_ref == "W" and gps_lon > 0:
        gps_lon = -gps_lon

    # Videos carry no EXIF; fall back to the QuickTime GPS keys. Mirrors the
    # QT_DATE_KEYS fallback above.
    if gps_lat is None or gps_lon is None:
        qt_coords = parse_qt_gps(rec)
        if qt_coords is not None:
            gps_lat, gps_lon = qt_coords
            gps_lat_ref = "N" if gps_lat >= 0 else "S"
            gps_lon_ref = "E" if gps_lon >= 0 else "W"

    # --- people ---
    # XMP:PersonInImage is not just Apple's naming — it also carries face tags
    # baked into imported files by other software, so the same person arrives
    # spelled several ways ("Jane"/"jane"). The alias map collapses those to
    # one canonical spelling; with no alias file it is a no-op.
    raw_persons = rec.get("XMP:PersonInImage", [])
    if isinstance(raw_persons, str):
        raw_persons = [raw_persons]
    persons = names_mod.canonicalize(raw_persons, person_aliases())

    # --- scene labels: keywords minus person names ---
    keywords = rec.get("IPTC:Keywords") or rec.get("XMP:Subject") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    # Both sides are stripped before comparing: some Apple person names carry
    # trailing whitespace ("Cruise ship entertainer "), and comparing a
    # stripped name against an unstripped keyword lets it through as a scene.
    # Match on the raw spellings as well as the canonical ones — the keyword
    # list still holds what was written to the file, so filtering on the
    # canonical name alone would let the original spelling leak into scenes.
    person_set = {p.strip().lower() for p in persons if isinstance(p, str)}
    person_set |= {p.strip().lower() for p in raw_persons if isinstance(p, str)}
    scene_labels = [k for k in keywords if k.strip().lower() not in person_set]

    # --- face regions ---
    region_info = rec.get("XMP-mwg-rs:RegionInfo", {})
    face_regions = []
    if region_info:
        for r in region_info.get("RegionList", []):
            if r.get("Type") == "Face":
                face_regions.append({
                    "name": r.get("Name"),
                    "area": r.get("Area", {}),
                })

    return {
        "uuid":               uuid,
        "media_type":         media_type,
        "file_ext":           file_ext,
        "datetime_original":  datetime_original,
        "date":               date,
        "time":               time_,
        "tz_offset":          tz_offset,
        "gps_lat":            gps_lat,
        "gps_lon":            gps_lon,
        "gps_lat_ref":        gps_lat_ref,
        "gps_lon_ref":        gps_lon_ref,
        "persons":            json.dumps(persons),
        "scene_labels":       json.dumps(scene_labels),
        "face_regions":       json.dumps(face_regions),
        "sidecar_path":       str(json_path),
        "phase1_processed_at": datetime.now(timezone.utc).isoformat(),
        # Videos skip Phase 2
        "phase2_processed":   -1 if media_type == "video" else 0,
    }


# ---------------------------------------------------------------------------
# Ingest run
# ---------------------------------------------------------------------------
def run_ingest(conn: sqlite3.Connection, dest_dir: Path) -> dict:
    stats = {"files_seen": 0, "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    json_files = sorted(dest_dir.rglob("*.json"))
    stats["files_seen"] = len(json_files)

    # Tombstones, loaded once — the set is small and this runs per sidecar.
    # Without it a web-app delete reverts on the next tick: staging still has
    # the file, rsync re-delivers it, and the sidecar below re-inserts the row.
    suppressed = {r[0] for r in conn.execute("SELECT uuid FROM suppressed")}
    if suppressed:
        log.info("%d suppressed uuid(s) will be skipped", len(suppressed))

    for json_path in json_files:
        # Skip exportdb and other non-sidecar JSON files
        if json_path.name.startswith("."):
            stats["skipped"] += 1
            continue

        # Extract UUID to check if already ingested
        base_stem = Path(json_path.stem).stem
        m = UUID_RE.match(base_stem)
        if not m:
            stats["skipped"] += 1
            continue

        uuid = m.group(1).upper()
        if uuid in suppressed:
            stats["skipped"] += 1
            continue

        if already_ingested(conn, uuid):
            stats["skipped"] += 1
            continue

        # osxphotos writes the JSON sidecar even when the original is
        # missing from iCloud ("Skipping missing original photo"), so a
        # sidecar can arrive without its media file. Ingesting it would
        # create a row Phase 2 fails on forever and the web app renders
        # as a dead card. Skip — the row is created on a later run once
        # the media file lands.
        if not json_path.with_suffix("").exists():
            log.warning("sidecar has no media file yet, skipping: %s",
                        json_path.name)
            stats["skipped"] += 1
            continue

        record = parse_sidecar(json_path)
        if record is None:
            stats["errors"] += 1
            continue

        try:
            conn.execute("""
                INSERT INTO photos (
                    uuid, media_type, file_ext,
                    datetime_original, date, time, tz_offset,
                    gps_lat, gps_lon, gps_lat_ref, gps_lon_ref,
                    persons, scene_labels, face_regions,
                    sidecar_path, phase1_processed_at, phase2_processed
                ) VALUES (
                    :uuid, :media_type, :file_ext,
                    :datetime_original, :date, :time, :tz_offset,
                    :gps_lat, :gps_lon, :gps_lat_ref, :gps_lon_ref,
                    :persons, :scene_labels, :face_regions,
                    :sidecar_path, :phase1_processed_at, :phase2_processed
                )
                ON CONFLICT(uuid) DO UPDATE SET
                    phase1_processed_at = excluded.phase1_processed_at,
                    sidecar_path        = excluded.sidecar_path,
                    -- Fill GPS only where it is currently NULL. COALESCE
                    -- never overwrites a value we already have, so this is
                    -- safe to run over the whole library, but it does let a
                    -- parser fix like QT_GPS_KEYS repair existing rows on the
                    -- next pass instead of needing a one-off backfill script.
                    gps_lat     = COALESCE(photos.gps_lat,     excluded.gps_lat),
                    gps_lon     = COALESCE(photos.gps_lon,     excluded.gps_lon),
                    gps_lat_ref = COALESCE(photos.gps_lat_ref, excluded.gps_lat_ref),
                    gps_lon_ref = COALESCE(photos.gps_lon_ref, excluded.gps_lon_ref)
            """, record)
            stats["inserted"] += 1
        except Exception as e:
            log.warning("DB insert failed for %s: %s", uuid, e)
            stats["errors"] += 1

    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="photo-intel Phase 1 ingest")
    parser.add_argument("--config", default="photo-intel.conf")
    parser.add_argument("--interval", type=int, default=300,
                        help="Polling interval in seconds (default: 300)")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit")
    args = parser.parse_args()

    cfg = load_config(args.config)
    dest_dir = Path(cfg.get("paths", "dest_dir"))
    db_path  = cfg.get("paths", "db_path")

    if not dest_dir.exists():
        log.error("dest_dir does not exist: %s", dest_dir)
        sys.exit(1)

    db_path_obj = Path(db_path)
    db_path_obj.parent.mkdir(parents=True, exist_ok=True)

    conn = open_db(db_path)
    log.info("DB: %s", db_path)
    log.info("dest_dir: %s", dest_dir)
    log.info("Polling every %ds  (--once: %s)", args.interval, args.once)

    while True:
        run_at = datetime.now(timezone.utc).isoformat()
        log.info("Ingest run starting...")
        stats = run_ingest(conn, dest_dir)

        conn.execute("""
            INSERT INTO phase1_log (run_at, files_seen, inserted, updated, skipped, errors)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (run_at, stats["files_seen"], stats["inserted"],
              stats["updated"], stats["skipped"], stats["errors"]))
        conn.commit()

        log.info(
            "Done — seen: %d  inserted: %d  skipped: %d  errors: %d",
            stats["files_seen"], stats["inserted"], stats["skipped"], stats["errors"]
        )

        if args.once:
            break

        log.info("Sleeping %ds...", args.interval)
        time.sleep(args.interval)

    conn.close()

    # Exit non-zero only when the run had work in hand and none of it
    # succeeded — i.e. every sidecar seen errored. Individual parse/insert
    # errors are counted, logged, and recorded in phase1_log; a few are not a
    # unit failure. A run that only *skips* is a success: skipped means the
    # file was seen and correctly needed no work, which is the steady state.
    # An empty dest_dir yields no errors and exits 0. Symmetric with
    # photo_intel_phase2.py and photo_intel_video.py, which exit 1 only on zero
    # progress against a non-empty queue. Reachable in --once mode (what the
    # timer runs); the daemon loop never leaves the while.
    progress = stats["inserted"] + stats["updated"] + stats["skipped"]
    if stats["errors"] and not progress:
        log.error("every one of the %d sidecars seen failed to ingest — "
                  "exiting non-zero", stats["files_seen"])
        sys.exit(1)


if __name__ == "__main__":
    main()
