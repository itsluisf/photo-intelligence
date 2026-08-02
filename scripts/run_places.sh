#!/bin/sh
# run_places.sh — dump Apple Photos place names and ship them to the
# processing host (com.photo-intel.places / com.photo-intel.places-incr).
#
# Apple's reverse geocoding is the authoritative source for where a photo was
# taken. Phase 2 / 2b used to be handed bare GPS coordinates and asked to name
# the place, which they do badly — a ballpark 50 miles outside a capital city
# gets confidently labelled with the capital's stadium. This runs on the Mac
# because only the Mac has the Photos library.
#
# Usage:  run_places.sh [TIME_DELTA]
#   no arg     full dump of the library (a few minutes for a large one).
#   TIME_DELTA incremental: query only assets added in that window and merge
#              into the existing file (~1 min — library load dominates).
#
# ORDERING IS THE POINT — see "Place names" in the README. A dump must land
# before the Phase 2 sweep that will consume the new photos. A sweep that runs
# first describes them from bare GPS, and because the row is never revisited
# once phase2_processed=1, that wrong description is permanent and the GPU time
# that produced it is wasted. Pair each dump with an apply ~10 min ahead of a
# sweep; the exact clock times depend on your own sweep schedule.
#
# The daily full dump is not redundant with the incrementals: --added-in-last
# only sees newly ADDED assets, so a photo the OS re-geocodes later would never
# appear in an incremental.
#
# Deliberately decoupled: a failed dump leaves the previous places.json in
# place and the apply is a harmless no-op re-write.

set -e

cd /Users/youruser/photo-intel || exit 1

# NOT a temp directory. An incremental dump merges into whatever is already at
# this path, so if the file were pruned the merge would silently produce a file
# holding only that window's handful of photos, and rsync would ship that over
# the complete one. The database itself is safe either way — --apply only
# updates rows it matches and never clears a place — but the file would stop
# being a usable fallback for an earlier apply that failed.
DUMP=/Users/youruser/photo-intel/places.json
DEST=user@processing-host:/srv/photo-intel/places.json

if [ -n "$1" ]; then
    python3 photo_intel_places.py --config photo-intel.conf \
        --dump "$DUMP" --added-in-last "$1"
else
    python3 photo_intel_places.py --config photo-intel.conf --dump "$DUMP"
fi

rsync -a "$DUMP" "$DEST"

echo "$(date '+%Y-%m-%d %H:%M:%S')  places dump shipped${1:+ (incremental $1)}"
