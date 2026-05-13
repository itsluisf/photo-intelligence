#!/usr/bin/env python3
"""
query_photos.py — Search and explore your photo metadata DB

Usage:
    python3 query_photos.py --db ~/photos_meta.db --search "beach sunset"
    python3 query_photos.py --db ~/photos_meta.db --search "Bilbao"
    python3 query_photos.py --db ~/photos_meta.db --year 2019
    python3 query_photos.py --db ~/photos_meta.db --person "Alex"
    python3 query_photos.py --db ~/photos_meta.db --stats
    python3 query_photos.py --db ~/photos_meta.db --faces
    python3 query_photos.py --db ~/photos_meta.db --no-gps
    python3 query_photos.py --db ~/photos_meta.db --export results.json
"""

import sqlite3
import argparse
import json
from pathlib import Path
from datetime import datetime

def open_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def print_row(row, verbose=False):
    r = dict(row)
    path = r.get("file_path") or r.get("filename", "?")
    date = r.get("date_created", "?")[:10] if r.get("date_created") else "?"
    desc = r.get("gemma_description", "")
    loc  = r.get("gemma_location_guess") or f"{r.get('city','')}, {r.get('country','')}".strip(", ") or ""
    tags = ""
    if r.get("gemma_tags"):
        try:
            tags = ", ".join(json.loads(r["gemma_tags"])[:6])
        except Exception:
            pass

    print(f"\n  {Path(path).name}  [{date}]")
    if loc:
        print(f"  📍 {loc}")
    if desc:
        print(f"  📝 {desc[:200]}")
    if tags:
        print(f"  🏷  {tags}")
    if verbose:
        if r.get("latitude"):
            print(f"  🌐 GPS: {r['latitude']:.4f}, {r['longitude']:.4f}")
        if r.get("camera_model"):
            print(f"  📷 {r.get('camera_make','')} {r['camera_model']}")
        if r.get("named_people"):
            try:
                people = json.loads(r["named_people"])
                print(f"  👤 {', '.join(people)}")
            except Exception:
                pass

def cmd_search(conn, query, limit=20, verbose=False):
    """Full-text search across all indexed fields."""
    print(f"\nSearching for: '{query}'\n{'─'*50}")
    try:
        cur = conn.execute(f"""
            SELECT p.* FROM photos p
            JOIN photos_fts f ON p.id = f.rowid
            WHERE photos_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit))
        rows = cur.fetchall()
    except Exception:
        # Fallback: LIKE search
        pattern = f"%{query}%"
        cur = conn.execute("""
            SELECT * FROM photos
            WHERE gemma_description LIKE ? OR gemma_tags LIKE ?
               OR gemma_location_guess LIKE ? OR country LIKE ?
               OR city LIKE ? OR named_people LIKE ?
               OR vision_text_content LIKE ?
            LIMIT ?
        """, (pattern,)*7 + (limit,))
        rows = cur.fetchall()

    print(f"{len(rows)} results:")
    for row in rows:
        print_row(row, verbose)

def cmd_stats(conn):
    """Show database statistics."""
    print("\n── Database Statistics ──────────────────────────────\n")

    cur = conn.execute("SELECT COUNT(*) FROM photos WHERE kind=0")
    photos = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM photos WHERE kind=1")
    videos = cur.fetchone()[0]
    print(f"  Total photos : {photos:,}")
    print(f"  Total videos : {videos:,}")

    cur = conn.execute("SELECT COUNT(*) FROM photos WHERE gemma_processed=1")
    print(f"  Gemma done   : {cur.fetchone()[0]:,}")

    cur = conn.execute("SELECT COUNT(*) FROM photos WHERE latitude IS NOT NULL")
    gps = cur.fetchone()[0]
    print(f"  With GPS     : {gps:,} ({100*gps//max(photos,1)}%)")

    cur = conn.execute("SELECT COUNT(*) FROM photos WHERE vision_face_count > 0")
    print(f"  Faces found  : {cur.fetchone()[0]:,}")

    cur = conn.execute("SELECT MIN(year), MAX(year) FROM photos WHERE year IS NOT NULL")
    row = cur.fetchone()
    print(f"  Year range   : {row[0]} – {row[1]}")

    print(f"\n  Photos by year:")
    cur = conn.execute("SELECT year, COUNT(*) c FROM photos WHERE year IS NOT NULL GROUP BY year ORDER BY year")
    for row in cur.fetchall():
        bar = "█" * min(int(row[1]/50), 40)
        print(f"    {row[0]}  {bar} {row[1]:,}")

    print(f"\n  Top cameras:")
    cur = conn.execute("""
        SELECT camera_model, COUNT(*) c FROM photos
        WHERE camera_model IS NOT NULL
        GROUP BY camera_model ORDER BY c DESC LIMIT 8
    """)
    for row in cur.fetchall():
        print(f"    {row[0]:<35} {row[1]:,}")

    print(f"\n  Top countries (GPS/Gemma):")
    cur = conn.execute("""
        SELECT country, COUNT(*) c FROM photos
        WHERE country IS NOT NULL
        GROUP BY country ORDER BY c DESC LIMIT 10
    """)
    for row in cur.fetchall():
        print(f"    {row[0]:<30} {row[1]:,}")

def cmd_faces(conn):
    """Show named people summary."""
    print("\n── Named People ─────────────────────────────────────\n")
    cur = conn.execute("SELECT full_name, face_count FROM people ORDER BY face_count DESC")
    rows = cur.fetchall()
    if not rows:
        print("  No named people in DB yet.")
        return
    for row in rows:
        print(f"  {row[0]:<30} {row[1]:,} photos")

def cmd_year(conn, year, limit=20, verbose=False):
    print(f"\nPhotos from {year}:\n{'─'*50}")
    cur = conn.execute("SELECT * FROM photos WHERE year=? AND kind=0 ORDER BY date_created LIMIT ?", (year, limit))
    rows = cur.fetchall()
    print(f"{len(rows)} results (limit {limit}):")
    for row in rows:
        print_row(row, verbose)

def cmd_person(conn, name, limit=20, verbose=False):
    print(f"\nPhotos with '{name}':\n{'─'*50}")
    pattern = f'%{name}%'
    cur = conn.execute("""
        SELECT * FROM photos
        WHERE named_people LIKE ? OR gemma_description LIKE ?
        ORDER BY date_created DESC LIMIT ?
    """, (pattern, pattern, limit))
    rows = cur.fetchall()
    print(f"{len(rows)} results:")
    for row in rows:
        print_row(row, verbose)

def cmd_no_gps(conn, limit=20):
    print(f"\nPhotos without GPS ({limit} shown):\n{'─'*50}")
    cur = conn.execute("""
        SELECT filename, date_created, camera_model, gemma_location_guess
        FROM photos
        WHERE latitude IS NULL AND kind=0
        ORDER BY date_created DESC LIMIT ?
    """, (limit,))
    for row in cur.fetchall():
        loc = row[3] or "unknown"
        date = (row[1] or "?")[:10]
        print(f"  {row[0]:<40} {date}  {loc}")

def cmd_export(conn, out_path, search=None):
    """Export results to JSON."""
    if search:
        pattern = f"%{search}%"
        cur = conn.execute("""
            SELECT * FROM photos
            WHERE gemma_description LIKE ? OR gemma_tags LIKE ?
               OR country LIKE ? OR city LIKE ?
            LIMIT 500
        """, (pattern,)*4)
    else:
        cur = conn.execute("SELECT * FROM photos LIMIT 1000")

    rows = [dict(r) for r in cur.fetchall()]
    Path(out_path).write_text(json.dumps(rows, indent=2, default=str))
    print(f"Exported {len(rows)} rows to {out_path}")

def main():
    parser = argparse.ArgumentParser(description="Query photo metadata DB")
    parser.add_argument("--db", default=str(Path.home() / "photos_meta.db"))
    parser.add_argument("--search", help="Full-text search query")
    parser.add_argument("--year", type=int, help="Filter by year")
    parser.add_argument("--person", help="Filter by person name")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--faces", action="store_true", help="Show named people")
    parser.add_argument("--no-gps", action="store_true", help="Show photos without GPS")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--export", help="Export results to JSON file")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        import sys; sys.exit(f"DB not found: {db_path}")

    conn = open_db(db_path)

    if args.stats:
        cmd_stats(conn)
    elif args.faces:
        cmd_faces(conn)
    elif args.search:
        cmd_search(conn, args.search, args.limit, args.verbose)
        if args.export:
            cmd_export(conn, args.export, args.search)
    elif args.year:
        cmd_year(conn, args.year, args.limit, args.verbose)
    elif args.person:
        cmd_person(conn, args.person, args.limit, args.verbose)
    elif args.no_gps:
        cmd_no_gps(conn, args.limit)
    elif args.export:
        cmd_export(conn, args.export)
    else:
        parser.print_help()

    conn.close()

if __name__ == "__main__":
    main()
