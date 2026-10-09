#!/usr/bin/env python3
"""search_info_counts.py — count how many photos carry each Photos search-index field.

    <osxphotos python> tools/search_info_counts.py [out.json]

Read-only. Loads the Photos library (a minute or two for a large one) and prints,
for each search-info field, how many photos have at least one value. `labels`,
`activities` and `venue_types` are the three the export writes as keywords.

Use it to tell when macOS has finished rebuilding the search index after a major
upgrade: run it before upgrading for a baseline, then poll afterwards until the
three counts are close to the baseline AND unchanged between two runs an hour
apart. `labels` returns first; activities and venue types come from a later
analysis pass and can lag by days. See docs/upgrading-macos.md.

With out.json, also writes {cloud_guid: {field: [values]}} for diffing two runs.

Run it with the Python inside the osxphotos install, e.g.
~/.local/pipx/venvs/osxphotos/bin/python.
"""
import collections
import json
import sys
import time

import osxphotos

FIELDS = ["labels", "activities", "venue_types", "venues", "bodies_of_water",
          "locality_names", "media_types", "source", "holidays"]

t = time.time()
db = osxphotos.PhotosDB()
counts = collections.Counter()
out = {}
for p in db.photos(intrash=False):
    if not p.cloud_guid:
        continue
    si = p.search_info
    d = {}
    for f in FIELDS:
        v = getattr(si, f)
        v = [v] if isinstance(v, str) else (v or [])
        d[f] = sorted(x for x in v if x)
        counts[f] += bool(d[f])
    out[p.cloud_guid] = d
print(f"osxphotos {osxphotos.__version__}, {round(time.time() - t)} s")
for f in FIELDS:
    print(f"  {f:16} {counts[f]:>8,}")
if len(sys.argv) > 1:
    with open(sys.argv[1], "w") as fh:
        json.dump(out, fh)
