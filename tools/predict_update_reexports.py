#!/usr/bin/env python3
"""predict_update_reexports.py — predict how many files the next full
`osxphotos export --update --exiftool` sweep would re-export.

osxphotos re-exports a photo whenever the metadata it would write with exiftool
differs from the copy stored in the export DB at the last export — the whole
file, not only its sidecar, and in split mode each one is rsynced again. After a
major macOS upgrade that can be a large share of the library. This script
recomputes that metadata with the same options photo_intel_export.py uses and
compares it, string for string, with what is stored.

Read-only. Dump the stored metadata first:

    E=~/photo-intel/osxphotos_export.db
    sqlite3 -json "file:$E?immutable=1" \\
        "select uuid, filepath, dest_size, exifdata from export_data where exifdata is not null;" \\
        | gzip -1 > /tmp/exif_stored.json.gz

then, with the Python inside the osxphotos install:

    <osxphotos python> tools/predict_update_reexports.py /tmp/exif_stored.json.gz /tmp/pred.json \\
        [--legacy-map map.json.gz] [--limit N]

--legacy-map predicts with the legacy keyword template in place, as the export
runs once [export] legacy_keywords is set. out.json maps each changed uuid to the
tags that differ. See docs/upgrading-macos.md.
"""
import argparse
import collections
import gzip
import json
import os
import random
import time
from pathlib import Path

import osxphotos
from osxphotos.photoexporter import ExportOptions
from osxphotos.sidecars import exiftool_json_sidecar

ap = argparse.ArgumentParser()
ap.add_argument("stored")
ap.add_argument("out")
ap.add_argument("--legacy-map")
ap.add_argument("--limit", type=int, default=0, help="random sample of N assets")
args = ap.parse_args()

stored = collections.defaultdict(list)
for r in json.load(gzip.open(args.stored)):
    if r["filepath"].endswith(".json"):
        continue
    stored[r["uuid"]].append((r["exifdata"], r.get("dest_size") or 0))

templates = ["{label}", "{searchinfo.activity}", "{searchinfo.venue_type}"]
if args.legacy_map:
    os.environ["PHOTO_INTEL_LEGACY_KW"] = str(Path(args.legacy_map).resolve())
    kw = Path(__file__).resolve().parent.parent / "src" / "photo_intel_legacy_kw.py"
    templates.append("{function:" + str(kw) + "::legacy_keywords}")
opts = ExportOptions(exiftool=True, use_persons_as_keywords=True,
                     keyword_template=templates)

t = time.time()
db = osxphotos.PhotosDB()
print("library load", round(time.time() - t), "s", flush=True)
photos = [p for p in db.photos(intrash=False) if p.uuid in stored]
if args.limit:
    random.seed(1)
    photos = random.sample(photos, min(args.limit, len(photos)))

c = collections.Counter()
tags = collections.Counter()
diff_bytes = 0
res = {}
t = time.time()
for p in photos:
    cur = exiftool_json_sidecar(photo=p, options=opts)
    recs = stored[p.uuid]
    changed = any(cur != s for s, _ in recs)
    c["assets"] += 1
    c["changed"] += changed
    if changed:
        diff_bytes += sum(sz for _, sz in recs)
        a = json.loads(cur)[0]
        b = json.loads(recs[0][0])[0]
        dk = sorted(x for x in set(a) | set(b) if a.get(x) != b.get(x))
        tags.update(dk)
        res[p.uuid] = dk
print(f"{c['assets']} assets in {round(time.time() - t)} s; would re-export "
      f"{c['changed']} ({c['changed'] / max(c['assets'], 1):.1%}), "
      f"{diff_bytes / 1e9:.1f} GB of files")
print("differing tags:", tags.most_common(15))
with open(args.out, "w") as fh:
    json.dump(res, fh)
