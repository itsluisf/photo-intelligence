#!/bin/sh
# run_export_full.sh — weekly full-sweep backstop for the manifest-gated
# photo-intel export (com.photo-intel.export-full). Runs the unchanged
# 48-window photo_intel_export.py (no --changed-file), catching anything the
# 2-hourly gate structurally misses — e.g. osxphotos export-db lag across a
# manifest reseed, or a face/venue/activity backfill the gate's 4-signal
# changekey somehow doesn't track. See the README.
#
# Own log (export-full.log) so it never races the gated job's export.log
# rotation. Shares .export.lock with run_export.sh so the two never run
# concurrent osxphotos passes against the same osxphotos_export.db.

cd "$HOME/photo-intel" || exit 1

LOG="export-full.log"
MAXBYTES=$((50 * 1024 * 1024))
KEEP=3

. "$HOME/photo-intel/photo_intel_lib.sh"

# Wait up to ~10 min for an in-flight gated run (~8 s when quiet) to finish.
# lock_acquire also reclaims a lock whose owner has died — see photo_intel_lib.sh.
if ! lock_acquire 600 run_export_full.sh; then
    log_line "could not acquire lock after ~10 min; skipping full sweep (owner: $(lock_owner))"
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

log_line "full sweep starting (pid $$)"

PY=/opt/homebrew/bin/python3.12
"$PY" photo_intel_export.py >> "$LOG" 2>&1
# Capture immediately. $? survives the comment below, but not any command added
# between here and the test, which is the kind of edit that breaks silently.
sweep_rc=$?

# After a clean full sweep, refresh the canonical manifest so the gate's baseline
# reflects everything the sweep just reconciled (closes any reseed-boundary gap).
if [ "$sweep_rc" -eq 0 ]; then
    "$PY" manifest_gate.py \
        --manifest manifest/manifest.tsv \
        --manifest-out manifest/manifest.tsv \
        --changed-out /dev/null >> "$LOG" 2>&1
fi
