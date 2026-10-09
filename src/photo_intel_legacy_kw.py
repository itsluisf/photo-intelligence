"""photo_intel_legacy_kw.py — osxphotos {function:} keyword template that carries
each photo's pre-upgrade keywords forward into exports made after a major macOS
upgrade.

WHY
    A major macOS release (seen on 26.7 -> 27) rebuilds Photos' search index from
    scratch. The rebuilt index can sit well below the old one for weeks: on the
    library this was developed against, macOS 27 had activity and venue-type
    labels for 20-40% fewer assets than 26.7 had. Because photo_intel_export.py writes
    {searchinfo.activity} / {searchinfo.venue_type} into each file's keywords,
    the first full --update sweep after the upgrade would have stripped
    keywords such as Theme Park, Museum or Baseball Stadium from ~15% of files.

    This template returns the keywords each file carried BEFORE the upgrade, so
    osxphotos unions them with the live templates instead of replacing them.
    Photos added since the upgrade are not in the map and get nothing extra.

USE
    1. Before upgrading macOS, copy the osxphotos export DB somewhere safe
       (see docs/upgrading-macos.md).
    2. After upgrading, build the map from that frozen copy:
           python3 build_legacy_keywords.py <frozen-export.db> <map.json.gz>
    3. Point photo-intel.conf at it:
           [export]
           legacy_keywords = /path/to/map.json.gz
       photo_intel_export.py then adds
           --keyword-template '{function:<this file>::legacy_keywords}'
       and passes the map path in PHOTO_INTEL_LEGACY_KW.

The map holds no person names: build_legacy_keywords.py removes every
XMP:PersonInImage value, so person keywords keep following live face data.
"""

import gzip
import json
import os

_MAP = None


def legacy_keywords(photo, options=None, args=None, **kwargs):
    # osxphotos imports this module once per process; load the map lazily, once.
    # No fallback when the variable is missing: an empty answer would silently
    # strip the very keywords this template exists to keep.
    global _MAP
    if _MAP is None:
        with gzip.open(os.environ["PHOTO_INTEL_LEGACY_KW"], "rt") as fh:
            _MAP = json.load(fh)
    return _MAP.get(photo.uuid, [])
