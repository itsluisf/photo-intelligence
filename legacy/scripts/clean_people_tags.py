#!/usr/bin/env python3
"""
Strip 'people:N' entries from gemma_tags JSON arrays in photos_meta.db.

The Gemma prompt produced tags like ["family", "indoor", "people:3", ...]. The
people-count value isn't useful as a free-text tag — it pollutes tag frequency
counts and the search filter dropdown. This script removes those entries while
leaving all other tags untouched.

Safe to re-run; idempotent.

Usage:
    python3 clean_people_tags.py --db ~/photos_meta.db
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

PEOPLE_RE = re.compile(r"^people:.+$", re.IGNORECASE)

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", default=str(Path.home() / "photos_meta.db"),
                        help="Path to photos_meta.db")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cur = conn.execute("""
        SELECT id, gemma_tags
        FROM photos
        WHERE gemma_processed = 1
          AND gemma_tags IS NOT NULL
          AND gemma_tags LIKE '%people:%'
    """)
    rows = cur.fetchall()
    print(f"Candidate rows: {len(rows):,}")

    updated = 0
    parse_errors = 0
    unchanged = 0

    for row in rows:
        try:
            tags = json.loads(row["gemma_tags"])
        except (json.JSONDecodeError, TypeError):
            parse_errors += 1
            continue

        if not isinstance(tags, list):
            parse_errors += 1
            continue

        cleaned = [t for t in tags if not (isinstance(t, str) and PEOPLE_RE.match(t.strip()))]

        if len(cleaned) == len(tags):
            unchanged += 1
            continue

        new_json = json.dumps(cleaned, ensure_ascii=False)
        conn.execute("UPDATE photos SET gemma_tags = ? WHERE id = ?", (new_json, row["id"]))
        updated += 1

    conn.commit()
    conn.close()

    print(f"Updated:       {updated:,}")
    print(f"Unchanged:     {unchanged:,}  (matched LIKE but no people:N element)")
    print(f"Parse errors:  {parse_errors:,}")

if __name__ == "__main__":
    main()
