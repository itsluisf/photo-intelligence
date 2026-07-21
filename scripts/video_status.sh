#!/bin/sh
# video_status.sh — one-shot status of photo-intel Phase 2b video enrichment
# (photo_intel_video.py, systemd photo-intel-video.service, nightly timer).
# Answers "is it running / how far along / when does it finish" without having
# to reverse-engineer systemd + the DB each time. Read-only.
#
# Reads db_path from photo-intel.conf so it follows your configured layout.
# Override with:  DB=/path/to/photo-intel.db ./video_status.sh
CONF="${CONF:-$HOME/photo-intel/photo-intel.conf}"
if [ -z "$DB" ]; then
    DB=$(awk -F= '/^[[:space:]]*db_path[[:space:]]*=/ {sub(/^[[:space:]]+/,"",$2); sub(/[[:space:]]+$/,"",$2); print $2; exit}' "$CONF" 2>/dev/null)
fi
if [ -z "$DB" ] || [ ! -f "$DB" ]; then
    echo "photo-intel.db not found (DB='$DB', conf='$CONF')." >&2
    echo "Set DB=/path/to/photo-intel.db or CONF=/path/to/photo-intel.conf." >&2
    exit 1
fi

echo "== photo-intel Phase 2b — video enrichment =="
sqlite3 -column -header "$DB" "
  SELECT
    (SELECT COUNT(*) FROM photos WHERE media_type='video')                              AS total,
    (SELECT COUNT(*) FROM photos WHERE media_type='video' AND phase2_processed=1)        AS described,
    (SELECT COUNT(*) FROM photos WHERE media_type='video' AND phase2_processed NOT IN (1,2)) AS pending,
    (SELECT COUNT(*) FROM photos WHERE media_type='video' AND phase2_processed=2)        AS skipped;"

last24=$(sqlite3 "$DB" "SELECT COUNT(*) FROM photos WHERE media_type='video' AND phase2_processed=1 AND phase2_processed_at > datetime('now','-24 hours');")
pending=$(sqlite3 "$DB" "SELECT COUNT(*) FROM photos WHERE media_type='video' AND phase2_processed NOT IN (1,2);")
echo
echo "described in last 24h : $last24"
if [ "${last24:-0}" -gt 0 ]; then
    nights=$(( (pending + last24 - 1) / last24 ))
    echo "ETA at that rate      : ~$nights more nightly runs to clear $pending pending"
fi

echo
echo "== last run =="
systemctl status photo-intel-video.service --no-pager 2>/dev/null | sed -n '1,4p'
echo
echo "== next scheduled =="
systemctl list-timers photo-intel-video.timer --no-pager 2>/dev/null | sed -n '1,2p'
