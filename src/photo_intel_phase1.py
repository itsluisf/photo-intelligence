#!/usr/bin/env python3
"""
photo_intel_phase1.py
Polls dest_dir on the processing host for osxphotos JSON sidecars and
ingests them into photo-intel.db.  Runs continuously; safe to restart at
any time.

Run on: the processing host (same Mac in local mode; Linux/GPU box in split mode)
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("photo_intel_phase1")

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
"""

def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
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

    # --- people ---
    persons = rec.get("XMP:PersonInImage", [])
    if isinstance(persons, str):
        persons = [persons]

    # --- scene labels: keywords minus person names ---
    keywords = rec.get("IPTC:Keywords") or rec.get("XMP:Subject") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    person_set = set(p.lower() for p in persons)
    scene_labels = [k for k in keywords if k.lower() not in person_set]

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
                    sidecar_path        = excluded.sidecar_path
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


if __name__ == "__main__":
    main()
