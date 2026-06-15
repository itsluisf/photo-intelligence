#!/usr/bin/env python3
"""
explore_photos_db.py — Phase 0: Photos.sqlite Schema Explorer
Run this FIRST before building the production pipeline.
Outputs a report of what Apple has stored so we know what to extract.

Usage:
    python3 explore_photos_db.py
    python3 explore_photos_db.py --library "~/Pictures/Photos Library.photoslibrary"
    python3 explore_photos_db.py --full   # include row samples from all tables
"""

import sqlite3
import argparse
import os
import sys
import shutil
import tempfile
from pathlib import Path
from datetime import datetime

# ── Apple epoch: Jan 1 2001 (Core Data / CFAbsoluteTime) ──────────────────────
APPLE_EPOCH_OFFSET = 978307200

def apple_ts(val):
    """Convert Apple timestamp to human-readable UTC string."""
    if val is None:
        return None
    try:
        return datetime.utcfromtimestamp(float(val) + APPLE_EPOCH_OFFSET).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(val)

def find_photos_library():
    """Try to locate the Photos library automatically."""
    candidates = [
        Path.home() / "Pictures" / "Photos Library.photoslibrary",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None

def copy_db_to_temp(library_path: Path) -> Path:
    """
    Copy Photos.sqlite + WAL to a temp dir so we can open read-only
    without risking the live library. Returns path to the copy.
    """
    db_src = library_path / "database" / "Photos.sqlite"
    if not db_src.exists():
        sys.exit(f"ERROR: Cannot find {db_src}")

    tmp = Path(tempfile.mkdtemp(prefix="photos_explore_"))
    db_dst = tmp / "Photos.sqlite"
    shutil.copy2(db_src, db_dst)

    # Copy WAL and SHM if present (needed for consistent read)
    for ext in ["-wal", "-shm"]:
        src = db_src.with_suffix(db_src.suffix + ext)
        if src.exists():
            shutil.copy2(src, db_dst.with_suffix(db_dst.suffix + ext))

    return db_dst

def get_tables(conn):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [row[0] for row in cur.fetchall()]

def get_columns(conn, table):
    cur = conn.execute(f"PRAGMA table_info('{table}')")
    return [(row[1], row[2]) for row in cur.fetchall()]  # (name, type)

def row_count(conn, table):
    try:
        cur = conn.execute(f"SELECT COUNT(*) FROM '{table}'")
        return cur.fetchone()[0]
    except Exception:
        return "?"

def sample_rows(conn, table, n=3):
    try:
        cur = conn.execute(f"SELECT * FROM '{table}' LIMIT {n}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    except Exception as e:
        return [], [f"ERROR: {e}"]

# ── Tables we specifically care about ─────────────────────────────────────────
TABLES_OF_INTEREST = [
    "ZASSET",
    "ZADDITIONALASSETATTRIBUTES",
    "ZDETECTEDFACE",
    "ZPERSON",
    "ZGENERICFACE",
    "ZSCENE",
    "ZASSETDESCRIPTION",
    "ZCOMPUTEDASSETATTRIBUTES",
    "ZCLOUDMASTER",
    "ZALBUM",
    "ZGENERICALBUM",
    "ZMEMORY",
    "ZMOMENT",
    "ZCUSTOMRENDEREDASSET",
    "ZUNMANAGEDADJUSTMENT",
]

# ── Column name hints for what's interesting ──────────────────────────────────
INTERESTING_HINTS = [
    "latitude", "longitude", "location", "gps",
    "date", "time", "created", "modified",
    "filename", "path", "directory", "uniform",
    "scene", "label", "category", "classification",
    "face", "person", "name", "cluster",
    "width", "height", "duration", "orientation",
    "description", "caption", "keyword",
    "favorite", "hidden", "trashed",
    "country", "city", "state", "place",
]

def flag_interesting(col_name: str) -> str:
    col_lower = col_name.lower()
    for hint in INTERESTING_HINTS:
        if hint in col_lower:
            return " ◀"
    return ""

def main():
    parser = argparse.ArgumentParser(description="Explore Photos.sqlite schema")
    parser.add_argument("--library", help="Path to .photoslibrary bundle")
    parser.add_argument("--full", action="store_true", help="Show sample rows for ALL tables")
    parser.add_argument("--table", help="Show detailed sample for a specific table")
    parser.add_argument("--out", help="Write report to file instead of stdout")
    args = parser.parse_args()

    # Find library
    if args.library:
        library_path = Path(args.library)
    else:
        library_path = find_photos_library()
        if not library_path:
            sys.exit("ERROR: Could not find Photos Library. Use --library /path/to/Photos Library.photoslibrary")

    print(f"Library: {library_path}", flush=True)

    db_path = copy_db_to_temp(library_path)
    print(f"Working copy: {db_path}", flush=True)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    tables = get_tables(conn)
    lines = []

    lines.append("=" * 70)
    lines.append(f"Photos.sqlite Schema Report")
    lines.append(f"Library : {library_path}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Tables  : {len(tables)}")
    lines.append("=" * 70)

    # ── Summary table list ────────────────────────────────────────────────────
    lines.append("\n── ALL TABLES ──────────────────────────────────────────────────────\n")
    for t in tables:
        rc = row_count(conn, t)
        marker = " ★" if t in TABLES_OF_INTEREST else ""
        lines.append(f"  {t:<50} {str(rc):>8} rows{marker}")

    # ── Detailed columns for tables of interest ───────────────────────────────
    lines.append("\n\n── TABLES OF INTEREST (columns) ────────────────────────────────────\n")
    for t in TABLES_OF_INTEREST:
        if t not in tables:
            lines.append(f"\n  {t}  — NOT PRESENT in this library\n")
            continue
        rc = row_count(conn, t)
        cols = get_columns(conn, t)
        lines.append(f"\n{'─'*60}")
        lines.append(f"  TABLE: {t}  ({rc} rows)")
        lines.append(f"{'─'*60}")
        for col_name, col_type in cols:
            lines.append(f"    {col_name:<45} {col_type:<10}{flag_interesting(col_name)}")

    # ── Sample rows for tables of interest ───────────────────────────────────
    lines.append("\n\n── SAMPLE ROWS (tables of interest) ────────────────────────────────\n")
    sample_targets = TABLES_OF_INTEREST if args.full else [
        "ZASSET", "ZADDITIONALASSETATTRIBUTES", "ZDETECTEDFACE", "ZPERSON", "ZSCENE"
    ]
    if args.table:
        sample_targets = [args.table.upper()]

    for t in sample_targets:
        if t not in tables:
            continue
        cols, rows = sample_rows(conn, t, n=3)
        lines.append(f"\n{'═'*60}")
        lines.append(f"  TABLE: {t}")
        lines.append(f"{'═'*60}")
        if not rows:
            lines.append("  (empty)")
            continue
        for i, row in enumerate(rows):
            lines.append(f"\n  Row {i+1}:")
            if isinstance(row, str):
                lines.append(f"    {row}")
                continue
            for col, val in zip(cols, row):
                # Pretty-print timestamps
                if any(x in col.lower() for x in ["date", "time", "created", "modified"]):
                    pretty = apple_ts(val)
                    lines.append(f"    {col:<45}: {pretty}  (raw: {val})")
                elif isinstance(val, (bytes, bytearray)):
                    lines.append(f"    {col:<45}: <blob {len(val)} bytes>")
                else:
                    val_str = str(val)[:120] if val is not None else "NULL"
                    lines.append(f"    {col:<45}: {val_str}")

    # ── Quick stats on ZASSET ─────────────────────────────────────────────────
    if "ZASSET" in tables:
        lines.append("\n\n── ZASSET QUICK STATS ──────────────────────────────────────────────\n")
        try:
            # Asset kinds
            cur = conn.execute("SELECT ZKIND, COUNT(*) FROM ZASSET GROUP BY ZKIND ORDER BY COUNT(*) DESC")
            lines.append("  Asset kinds (ZKIND):")
            for row in cur.fetchall():
                kind_map = {0: "photo", 1: "video", 2: "audio"}
                lines.append(f"    {kind_map.get(row[0], row[0])}: {row[1]}")

            # Date range
            cur = conn.execute("SELECT MIN(ZDATECREATED), MAX(ZDATECREATED) FROM ZASSET WHERE ZDATECREATED IS NOT NULL")
            row = cur.fetchone()
            if row:
                lines.append(f"\n  Date range:")
                lines.append(f"    Oldest : {apple_ts(row[0])}")
                lines.append(f"    Newest : {apple_ts(row[1])}")

            # GPS coverage
            cur = conn.execute("SELECT COUNT(*) FROM ZASSET WHERE ZLATITUDE IS NOT NULL AND ZLATITUDE != -180.0")
            gps_count = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM ZASSET")
            total = cur.fetchone()[0]
            lines.append(f"\n  GPS coverage: {gps_count}/{total} assets ({100*gps_count//total if total else 0}%)")

        except Exception as e:
            lines.append(f"  Stats error: {e}")

    # ── Check for face/person data ────────────────────────────────────────────
    if "ZPERSON" in tables:
        lines.append("\n\n── FACE/PERSON SUMMARY ─────────────────────────────────────────────\n")
        try:
            cur = conn.execute("SELECT COUNT(*) FROM ZPERSON")
            lines.append(f"  People clusters : {cur.fetchone()[0]}")
            cur = conn.execute("SELECT COUNT(*) FROM ZPERSON WHERE ZFULLNAME IS NOT NULL")
            lines.append(f"  Named people    : {cur.fetchone()[0]}")
            cur = conn.execute("SELECT COUNT(*) FROM ZDETECTEDFACE")
            lines.append(f"  Detected faces  : {cur.fetchone()[0]}")
        except Exception as e:
            lines.append(f"  {e}")

    # ── Output ────────────────────────────────────────────────────────────────
    report = "\n".join(lines)

    if args.out:
        Path(args.out).write_text(report)
        print(f"\nReport written to: {args.out}")
    else:
        print(report)

    conn.close()

    # Cleanup temp files
    import shutil as _sh
    _sh.rmtree(db_path.parent, ignore_errors=True)

if __name__ == "__main__":
    main()
