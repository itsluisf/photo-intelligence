#!/usr/bin/env python3
"""build_legacy_keywords.py — build the map read by photo_intel_legacy_kw.py.

    python3 build_legacy_keywords.py <frozen-export.db> <map.json.gz>

Reads a FROZEN copy of the osxphotos export DB taken before the macOS upgrade —
never the live one, because osxphotos rewrites exifdata there on every export —
and writes {uuid: [keywords]}: each file's XMP:Subject (or IPTC:Keywords) minus
every XMP:PersonInImage name seen anywhere in the DB, so person keywords keep
following live face data and a renamed person is never resurrected.

The map is derived from your library. Keep it out of version control.
"""
import collections
import gzip
import json
import sqlite3
import sys

if len(sys.argv) != 3:
    sys.exit(__doc__)
SRC, OUT = sys.argv[1], sys.argv[2]

con = sqlite3.connect(f"file:{SRC}?immutable=1", uri=True)
rows = [(u, json.loads(x)[0]) for u, x in con.execute(
    "select uuid, exifdata from export_data "
    "where exifdata is not null and filepath not like '%.json'")]

persons = set()
for _, d in rows:
    persons.update(d.get("XMP:PersonInImage") or [])
per = collections.defaultdict(set)
for u, d in rows:
    per[u] |= set(d.get("XMP:Subject") or d.get("IPTC:Keywords") or [])

out = {u: sorted(k - persons) for u, k in per.items() if k - persons}
with gzip.open(OUT, "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
print(f"{len(rows)} rows, {len(out)} uuids with keywords, "
      f"{len(persons)} person names excluded -> {OUT}")
