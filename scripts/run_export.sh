#!/bin/sh
# run_export.sh — launcher for the photo-intel export launchd job
# (com.photo-intel.export). Manifest-gated incremental export (cutover
# 2026-07-21):
#   1. manifest_gate.py builds a fresh manifest from Photos.sqlite and diffs it
#      against the canonical baseline, emitting only changed UUIDs.
#   2. If anything changed, photo_intel_export.py re-exports just those UUIDs
#      (one osxphotos --uuid-from-file pass per affected year window) instead of
#      sweeping all 48 windows every 2 h.
#   3. The manifest is promoted to canonical ONLY after a successful export, so a
#      failed export doesn't swallow pending changes.
# The weekly com.photo-intel.export-full job runs the unchanged full sweep
# as a backstop for anything the gate structurally misses (e.g. osxphotos
# export-db lag across a manifest reseed).
#
# A shared lock (.export.lock, also taken by run_export_full.sh) guarantees only
# one export runs at a time — the gated tick simply skips if a weekly full sweep
# is in progress; no changes are lost (the manifest is not promoted on a skip).
#
# Rotates export.log at job start (idle, no fd race). Keeps KEEP gzip archives.

cd "$HOME/photo-intel" || exit 1

LOG="export.log"
MAXBYTES=$((50 * 1024 * 1024))   # rotate when export.log exceeds 50 MB
KEEP=5                            # gzip archives to retain

. "$HOME/photo-intel/photo_intel_lib.sh"

# Don't wait: the next tick is 2 h away and the manifest is not promoted on a
# skip, so nothing is lost. lock_acquire reclaims a lock whose owner has died —
# without that, a hung export left one behind and every later tick skipped on
# it forever (see photo_intel_lib.sh, STALE LOCKS).
if ! lock_acquire 0 run_export.sh; then
    log_line "another export holds the lock; skipping gated run (owner: $(lock_owner))"
    exit 0
fi
trap lock_release EXIT

if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG")" -gt "$MAXBYTES" ]; then
    rm -f "$LOG.$KEEP.gz"
    i=$((KEEP - 1))
    while [ "$i" -ge 1 ]; do
        [ -f "$LOG.$i.gz" ] && mv "$LOG.$i.gz" "$LOG.$((i + 1)).gz"
        i=$((i - 1))
    done
    mv "$LOG" "$LOG.1"
    gzip "$LOG.1"
fi

# Marks the run as STARTED. A hung run otherwise writes nothing at all, leaving
# no record of which run wedged or when — this line is that record.
log_line "gated export starting (pid $$)"

PY=/opt/homebrew/bin/python3.12
mkdir -p manifest

# Build manifest + diff vs canonical baseline; emit changed UUIDs. The freshly
# built manifest lands in manifest.new.tsv and is promoted only on export success.
"$PY" manifest_gate.py \
    --changed-out manifest/changed.tsv \
    --manifest-out manifest/manifest.new.tsv >> "$LOG" 2>&1

if [ -s manifest/changed.tsv ]; then
    if "$PY" photo_intel_export.py --changed-file manifest/changed.tsv >> "$LOG" 2>&1; then
        mv manifest/manifest.new.tsv manifest/manifest.tsv
    else
        echo "$(date -u +%FT%TZ) export FAILED; manifest not promoted, changes retained for next run" >> "$LOG"
        rm -f manifest/manifest.new.tsv
    fi
else
    # No changes — still advance the baseline (cheap, keeps it current).
    mv manifest/manifest.new.tsv manifest/manifest.tsv
fi
