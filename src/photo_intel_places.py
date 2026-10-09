#!/usr/bin/env python3
"""
photo_intel_places.py
Authoritative place names for photo-intel, taken from Apple Photos' own
reverse geocoding instead of being inferred by the VLM.

Before this existed, Phase 2 / 2b were handed bare GPS coordinates in the
prompt and asked to name the place. A 12B model cannot do coordinate
arithmetic, so it pattern-matches on what it recognises instead: a
minor-league ballpark ~50 miles outside a capital city came back as that
capital's famous stadium, at "100% confidence". The OS had already
reverse-geocoded the same photo down to the venue name — nothing was
reading it.

Run on: the export host (--dump) and the processing host (--apply, --backfill-video-gps)

Modes:
    --dump PATH             the export host. Read place info for every asset in the
                            Photos library via osxphotos, write JSON to PATH.
    --apply PATH            the processing host. Write the dump's place_* fields into
                            the DB. Implies --migrate.
    --backfill-video-gps    the processing host. Re-parse stored video sidecars for the
                            QuickTime GPS keys Phase 1 dropped before the
                            QT_GPS_KEYS fix. One-time; Phase 1 handles new
                            videos itself once patched.
    --migrate               the processing host. Add the place_* columns if missing.

Usage:
    python3 photo_intel_places.py --config photo-intel.conf --dump places.json
    python3 photo_intel_places.py --config photo-intel.conf --apply places.json
    python3 photo_intel_places.py --config photo-intel.conf --backfill-video-gps

    --dry-run works with every mode: reports counts, writes nothing.
"""

# the export host's system python3 may be older than the processing host's and evaluates annotations
# eagerly, so "str | None" would fail at def time there. Deferring keeps one
# copy of this script runnable on both hosts.
from __future__ import annotations

import argparse
import configparser
import json
import logging
import math
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("photo_intel_places")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Columns added to photos. All nullable — the migration is additive so it
# never rewrites the table, and dropping them restores the prior behaviour.
PLACE_COLUMNS = (
    ("place_name",         "TEXT"),  # Apple's full name string, verbatim
    ("place_aoi",          "TEXT"),  # area of interest — the venue, if any
    ("place_city",         "TEXT"),
    ("place_state",        "TEXT"),
    ("place_country",      "TEXT"),
    ("place_country_code", "TEXT"),
    ("place_source",       "TEXT"),  # 'apple' — room for a geocoder fallback
    ("place_updated_at",   "TEXT"),
)

# osxphotos template fields for one dump row. The trailing comma in
# "{field,}" joins multi-valued fields rather than emitting a row per value.
DUMP_FIELDS = (
    ("uuid",    "{uuid}"),
    ("name",    "{place.name,}"),
    ("aoi",     "{place.name.area_of_interest,}"),
    ("city",    "{place.address.city,}"),
    ("state",   "{place.address.state_province,}"),
    ("country", "{place.address.country,}"),
    ("cc",      "{place.country_code,}"),
)

# Video GPS lives in QuickTime metadata, not EXIF. Same shape as Phase 1's
# QT_DATE_KEYS fallback, which handles the equivalent problem for dates.
QT_GPS_KEYS = ("Keys:GPSCoordinates", "UserData:GPSCoordinates")

# "12.3456 -78.9012" or "12.3456 -78.9012 21.921" (altitude ignored).
QT_GPS_PLAIN_RE = re.compile(
    r"^\s*([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)(?:\s+[+-]?\d+(?:\.\d+)?)?\s*$"
)
# ISO 6709: "+12.3456-078.9012+021.921/" — not seen in this library's
# sidecars, but it is the format the spec allows, so accept it rather than
# silently dropping a whole import source's videos the way EXIF-only did.
QT_GPS_ISO6709_RE = re.compile(
    r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?$"
)

OSXPHOTOS_CANDIDATES = (
    "osxphotos",
    str(Path.home() / ".local/bin/osxphotos"),
    "/opt/homebrew/bin/osxphotos",
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        log.error("Config file not found: %s", path)
        sys.exit(1)
    return cfg


def find_osxphotos(explicit: str | None) -> str:
    if explicit:
        return explicit
    for cand in OSXPHOTOS_CANDIDATES:
        if Path(cand).exists() or shutil.which(cand):
            return cand
    log.error("osxphotos not found; pass --osxphotos PATH")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # The scheduled apply can land while Phase 1 (every 2h) or Phase 2b
    # (overnight) holds a write lock. Wait rather than dying on "database is
    # locked" — the bulk UPDATE itself only takes a few seconds.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrate(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Add any missing place_* columns. Idempotent."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    added = 0
    for col, coltype in PLACE_COLUMNS:
        if col in have:
            continue
        if dry_run:
            log.info("[dry-run] would add column %s %s", col, coltype)
        else:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {coltype}")
            log.info("Added column %s %s", col, coltype)
        added += 1
    if added and not dry_run:
        conn.commit()
    if not added:
        log.info("Schema already current — no columns added")
    return added


# ---------------------------------------------------------------------------
# Display formatting — imported by photo_intel_web / _phase2 / _video
# ---------------------------------------------------------------------------
def place_display(row) -> str | None:
    """Compose a short, readable place string from the place_* columns.

    Apple's own place.name is accurate but verbose — "Fenway Park, Boston,
    Greater Boston Area, United States". That full string stays in place_name
    (it is what the FTS index searches, and the metro area is a useful search
    term); this is the version meant for a human to read.

    US:      "Fenway Park, Boston, MA"
    non-US:  "Eiffel Tower, Paris, France"   — the country beats a region
             name nobody outside it would recognise.
    """
    def get(key):
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return None
        return str(value).strip() or None if value is not None else None

    country_code = get("place_country_code")
    tail = get("place_state") if country_code == "US" else get("place_country")

    parts, seen = [], set()
    for part in (get("place_aoi"), get("place_city"), tail):
        if not part:
            continue
        # Apple often repeats the city as the area of interest.
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        parts.append(part)

    if parts:
        return ", ".join(parts)
    # No components resolved — fall back to Apple's raw string.
    return get("place_name")


# ---------------------------------------------------------------------------
# Mode: --dump  (the export host)
# ---------------------------------------------------------------------------
def run_dump(library: str, out_path: Path, osxphotos: str,
             dry_run: bool, added_in_last: str | None = None) -> int:
    """Export uuid -> place for assets in the library.

    With added_in_last (an osxphotos TIME_DELTA such as "5h"), only assets
    added to the library in that window are queried and the result is MERGED
    into any existing file at out_path rather than replacing it.

    Merging matters: --apply is a full re-apply of whatever it is given, and
    the 08:50 apply is the one that has to beat the 09:00 Phase 2 sweep. If an
    incremental dump replaced the file, a failed morning apply could never be
    recovered by a later one, because the later file would hold only that
    window's handful of photos. Merging keeps places.json a complete picture,
    so every apply stays idempotent and any apply can cover for a failed one.

    Library load dominates the cost (~60s of the ~5.5 min full dump), so an
    incremental pass is ~90s rather than seconds. Cheap enough to run before
    each Phase 2 sweep, not cheap enough to run continuously.
    """
    cmd = [osxphotos, "query", "--library", library, "--mute", "--json"]
    if added_in_last:
        cmd += ["--added-in-last", added_in_last]
    for name, template in DUMP_FIELDS:
        cmd += ["--field", name, template]

    log.info("Running: %s", " ".join(cmd))
    if dry_run:
        log.info("[dry-run] would write %s", out_path)
        return 0

    started = datetime.now(timezone.utc)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("osxphotos failed (exit %d): %s",
                  result.returncode, result.stderr[-2000:])
        sys.exit(1)

    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        log.error("could not parse osxphotos output: %s", e)
        sys.exit(1)

    # Keep only rows that actually carry a place — the library has ~69k
    # assets with no GPS at all, and shipping empty rows just inflates the
    # file and the rsync.
    kept = [r for r in records if (r.get("name") or "").strip()]

    # Incremental dumps merge into the existing file; see the docstring.
    # An unreadable or corrupt existing file is treated as empty rather than
    # fatal — losing the merge is recoverable at the next full dump, refusing
    # to write at all is not.
    merged_from = 0
    if added_in_last and out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("existing %s unreadable (%s) — writing incremental "
                        "set alone; the next full dump restores it",
                        out_path, e)
            existing = []
        by_uuid = {r["uuid"]: r for r in existing if r.get("uuid")}
        merged_from = len(by_uuid)
        for r in kept:
            if r.get("uuid"):
                by_uuid[r["uuid"]] = r
        kept = list(by_uuid.values())

    # Write via a temp file so an interrupted dump can never leave a
    # half-written file for --apply to read.
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp_path.open("w") as fh:
        json.dump(kept, fh)
    tmp_path.replace(out_path)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    if added_in_last:
        log.info("Incremental (%s): %d assets queried, merged into %d "
                 "existing -> %d total  (%.0fs, %.1f MB)",
                 added_in_last, len(records), merged_from, len(kept),
                 elapsed, out_path.stat().st_size / 1e6)
    else:
        log.info("Dumped %d assets, %d with a place -> %s  (%.0fs, %.1f MB)",
                 len(records), len(kept), out_path, elapsed,
                 out_path.stat().st_size / 1e6)
    return len(kept)


# ---------------------------------------------------------------------------
# Mode: --apply  (the processing host)
# ---------------------------------------------------------------------------
def _clean(value) -> str | None:
    """osxphotos emits '' for absent fields; the DB wants NULL."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def run_apply(conn: sqlite3.Connection, dump_path: Path,
              dry_run: bool) -> dict:
    if not dump_path.exists():
        log.error("dump not found: %s", dump_path)
        sys.exit(1)

    with dump_path.open() as fh:
        records = json.load(fh)
    log.info("Loaded %d records from %s", len(records), dump_path)

    known = {r["uuid"] for r in conn.execute("SELECT uuid FROM photos")}
    # Hand-corrected places are off limits. This UPDATE is otherwise
    # unconditional, so without this set a place typed into the web UI would
    # survive only until the next places run — the edit would look like it
    # took, then silently revert hours later.
    manual = {r["uuid"] for r in conn.execute(
        "SELECT uuid FROM photos WHERE place_source = 'manual'")}
    now = datetime.now(timezone.utc).isoformat()

    rows, unknown, empty, protected = [], 0, 0, 0
    for rec in records:
        uuid = rec.get("uuid")
        if not uuid:
            continue
        if uuid not in known:
            # In the library but not ingested — shared albums, or an asset
            # whose media file has not landed on the processing host yet.
            unknown += 1
            continue
        if uuid in manual:
            protected += 1
            continue
        name = _clean(rec.get("name"))
        if not name:
            empty += 1
            continue
        rows.append((
            name,
            _clean(rec.get("aoi")),
            _clean(rec.get("city")),
            _clean(rec.get("state")),
            _clean(rec.get("country")),
            _clean(rec.get("cc")),
            "apple",
            now,
            uuid,
        ))

    log.info("Matched %d rows to ingest  (skipped: %d not in DB, %d no place, "
             "%d hand-corrected)", len(rows), unknown, empty, protected)

    if dry_run:
        log.info("[dry-run] would update %d rows", len(rows))
        return {"updated": 0, "unknown": unknown, "empty": empty,
                "protected": protected}

    conn.executemany(
        "UPDATE photos SET place_name=?, place_aoi=?, place_city=?, "
        "place_state=?, place_country=?, place_country_code=?, "
        "place_source=?, place_updated_at=? WHERE uuid=?",
        rows,
    )
    conn.commit()
    log.info("Updated %d rows", len(rows))
    return {"updated": len(rows), "unknown": unknown, "empty": empty,
            "protected": protected}


# ---------------------------------------------------------------------------
# Mode: --backfill-video-gps  (the processing host)
# ---------------------------------------------------------------------------
def parse_qt_gps(rec: dict) -> tuple[float, float] | None:
    """Pull (lat, lon) from a video sidecar's QuickTime GPS keys.

    Values are already signed, so unlike the EXIF path there is no
    GPSLatitudeRef/GPSLongitudeRef hemisphere correction to apply.
    """
    for key in QT_GPS_KEYS:
        raw = rec.get(key)
        if not raw:
            continue
        text = str(raw).strip()

        match = QT_GPS_PLAIN_RE.match(text)
        if not match:
            match = QT_GPS_ISO6709_RE.match(text)
        if not match:
            continue

        try:
            lat, lon = float(match.group(1)), float(match.group(2))
        except ValueError:
            continue
        # 0,0 is the null island sentinel some cameras write for "no fix".
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            continue
        if lat == 0 and lon == 0:
            continue
        return lat, lon
    return None


def run_backfill_video_gps(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Recover GPS for video rows ingested before the QT_GPS_KEYS fix.

    Phase 1's upsert is ON CONFLICT DO UPDATE SET phase1_processed_at,
    sidecar_path — it deliberately leaves GPS alone on existing rows, so
    simply re-running Phase 1 will not repair these. Hence a targeted pass.
    """
    rows = conn.execute(
        "SELECT uuid, sidecar_path FROM photos "
        "WHERE media_type = 'video' AND gps_lat IS NULL"
    ).fetchall()
    log.info("%d video rows with no GPS", len(rows))

    updates, missing, no_gps, bad = [], 0, 0, 0
    for row in rows:
        sidecar = row["sidecar_path"]
        if not sidecar or not Path(sidecar).exists():
            missing += 1
            continue
        try:
            with open(sidecar) as fh:
                data = json.load(fh)
        except Exception:
            bad += 1
            continue
        rec = data[0] if isinstance(data, list) and data else data
        if not isinstance(rec, dict):
            bad += 1
            continue

        coords = parse_qt_gps(rec)
        if coords is None:
            no_gps += 1
            continue
        lat, lon = coords
        updates.append((
            lat, lon,
            "N" if lat >= 0 else "S",
            "E" if lon >= 0 else "W",
            row["uuid"],
        ))

    log.info("Recovered GPS for %d videos  (no GPS in sidecar: %d, "
             "sidecar missing: %d, unreadable: %d)",
             len(updates), no_gps, missing, bad)

    if dry_run:
        log.info("[dry-run] would update %d rows", len(updates))
        return {"updated": 0, "no_gps": no_gps, "missing": missing, "bad": bad}

    conn.executemany(
        "UPDATE photos SET gps_lat=?, gps_lon=?, gps_lat_ref=?, gps_lon_ref=? "
        "WHERE uuid=?",
        updates,
    )
    conn.commit()
    log.info("Updated %d rows", len(updates))
    return {"updated": len(updates), "no_gps": no_gps,
            "missing": missing, "bad": bad}


# ---------------------------------------------------------------------------
# Mode: --nearby-fallback  (the processing host)
# ---------------------------------------------------------------------------
# Some rows have GPS but no place, and never will from --apply: the photo is
# gone from the Photos library (culled as a near-duplicate, say), so the dump
# has nothing to carry for it. Others are assets the OS geotagged but never
# reverse-geocoded — it returns an empty place for them.
#
# In both cases a neighbour usually knows the answer. Photos taken seconds
# apart at the same spot are common, and one of them typically does carry a
# resolved place. This borrows from the nearest already-placed photo.
#
# Borrowed rows are marked place_source='nearby', NOT 'apple', so an inferred
# place is never mistaken for an authoritative one. A later --apply that finds
# a real place for the row overwrites it (run_apply writes place_source='apple'
# for everything in the dump except place_source='manual'), so this can only
# ever fill a hole, never hold one open. It cannot touch a hand-corrected place
# either: it only selects rows whose place_name is NULL or ''.

# ~1.1 km per cell at the equator — comfortably larger than any sane radius,
# so a 3x3 neighbourhood always contains every candidate.
_GRID_DEG = 0.01
_M_PER_DEG_LAT = 111_320.0


def _cell(lat: float, lon: float) -> tuple:
    return (int(lat // _GRID_DEG), int(lon // _GRID_DEG))


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular approximation. Exact enough well under a kilometre,
    which is the only range this is ever asked about."""
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dx = (lon2 - lon1) * math.cos(mean_lat) * _M_PER_DEG_LAT
    dy = (lat2 - lat1) * _M_PER_DEG_LAT
    return math.hypot(dx, dy)


PLACE_FIELDS = ("place_name", "place_aoi", "place_city", "place_state",
                "place_country", "place_country_code")


def run_nearby_fallback(conn, radius_m: float = 100.0,
                        dry_run: bool = False) -> dict:
    """Fill place_* on GPS rows with no place from the nearest placed photo."""
    placed = conn.execute(
        "SELECT gps_lat, gps_lon, " + ", ".join(PLACE_FIELDS) + " "
        "FROM photos "
        "WHERE place_name IS NOT NULL AND place_name != '' "
        "  AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL"
    ).fetchall()

    orphans = conn.execute(
        "SELECT uuid, gps_lat, gps_lon FROM photos "
        "WHERE (place_name IS NULL OR place_name = '') "
        "  AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL"
    ).fetchall()

    log.info("Nearby fallback: %d rows need a place, %d placed rows to "
             "borrow from (radius %.0f m)", len(orphans), len(placed), radius_m)
    if not orphans or not placed:
        return {"resolved": 0, "unresolved": len(orphans)}

    grid = {}
    for row in placed:
        grid.setdefault(_cell(row["gps_lat"], row["gps_lon"]), []).append(row)

    updates, unresolved = [], 0
    for orph in orphans:
        lat, lon = orph["gps_lat"], orph["gps_lon"]
        cx, cy = _cell(lat, lon)
        best, best_d = None, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for cand in grid.get((cx + dx, cy + dy), ()):
                    d = _metres(lat, lon, cand["gps_lat"], cand["gps_lon"])
                    if d <= radius_m and (best_d is None or d < best_d):
                        best, best_d = cand, d
        if best is None:
            unresolved += 1
            continue
        updates.append((
            *(best[f] for f in PLACE_FIELDS),
            datetime.now(timezone.utc).isoformat(),
            orph["uuid"],
        ))

    if dry_run:
        log.info("[dry-run] would resolve %d rows from a neighbour "
                 "(%d still unresolved)", len(updates), unresolved)
        return {"resolved": len(updates), "unresolved": unresolved}

    conn.executemany(
        "UPDATE photos SET "
        + ", ".join(f"{f}=?" for f in PLACE_FIELDS)
        + ", place_source='nearby', place_updated_at=? WHERE uuid=?",
        updates,
    )
    conn.commit()
    log.info("Resolved %d rows from a neighbour (%d still unresolved)",
             len(updates), unresolved)
    return {"resolved": len(updates), "unresolved": unresolved}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="photo-intel place names from Apple Photos")
    parser.add_argument("--config", default="photo-intel.conf")
    parser.add_argument("--dump", metavar="PATH",
                        help="the export host: write uuid->place JSON to PATH")
    parser.add_argument("--apply", metavar="PATH",
                        help="the processing host: load uuid->place JSON from PATH")
    parser.add_argument("--backfill-video-gps", action="store_true",
                        help="the processing host: recover video GPS from sidecars")
    parser.add_argument("--migrate", action="store_true",
                        help="the processing host: add place_* columns and exit")
    parser.add_argument("--added-in-last", metavar="DELTA",
                        help="the export host: with --dump, query only assets added in "
                             "the last DELTA (osxphotos TIME_DELTA, e.g. 5h) "
                             "and merge into the existing file")
    parser.add_argument("--nearby-fallback", action="store_true",
                        help="the processing host: fill rows that have GPS but "
                             "no place from the nearest already-placed photo, "
                             "marked place_source='nearby'")
    parser.add_argument("--nearby-radius", type=float, default=100.0,
                        metavar="M",
                        help="max metres to borrow a place across "
                             "(default 100)")
    parser.add_argument("--osxphotos", help="path to the osxphotos binary")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change; write nothing")
    args = parser.parse_args()

    if not any((args.dump, args.apply, args.backfill_video_gps, args.migrate,
                args.nearby_fallback)):
        parser.error("pick a mode: --dump, --apply, --backfill-video-gps, "
                     "--nearby-fallback or --migrate")
    if args.added_in_last and not args.dump:
        parser.error("--added-in-last only applies to --dump")

    cfg = load_config(args.config)

    if args.dump:
        library = cfg.get("paths", "library")
        run_dump(library, Path(args.dump),
                 find_osxphotos(args.osxphotos), args.dry_run,
                 args.added_in_last)
        return

    db_path = cfg.get("paths", "db_path")
    conn = open_db(db_path)
    log.info("DB: %s", db_path)

    try:
        migrate(conn, args.dry_run)
        if args.migrate and not (args.apply or args.backfill_video_gps
                                 or args.nearby_fallback):
            return
        if args.backfill_video_gps:
            run_backfill_video_gps(conn, args.dry_run)
        if args.apply:
            run_apply(conn, Path(args.apply), args.dry_run)
        # Always after --apply: authoritative places land first, so the
        # fallback only ever fills what is genuinely still empty.
        if args.nearby_fallback:
            run_nearby_fallback(conn, args.nearby_radius, args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
