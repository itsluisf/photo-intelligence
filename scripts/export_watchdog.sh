#!/bin/sh
# export_watchdog.sh — notices when the photo-intel export stops exporting
# (com.photo-intel.export-watchdog, every 2 h).
#
# WHY
#   The 2-hourly gated export is silent by design: a quiet tick writes one line
#   and exits in seconds. That means "working perfectly" and "not running at
#   all" look identical from the outside. In practice an export once hung
#   holding .export.lock; launchd will not start a second instance of a
#   StartInterval job while the first is alive, so every later tick was
#   suppressed and export.log recorded nothing whatsoever for three days.
#
# WHAT IT CHECKS
#   1. export.log mtime — a heard-from timestamp, not a state field. Every tick
#      touches it, including a skip, so a frozen mtime means the launcher is not
#      running at all. Stale beyond STALE_HOURS -> alert.
#   2. .export.lock age — a lock held past HUNG_HOURS is an export that is
#      wedged rather than busy (the weekly full sweep, the longest legitimate
#      holder, takes ~2 h; a full re-export after a macOS upgrade can take
#      longer, and this alert is expected then).
#
# ALERTS go through notify() in photo_intel_lib.sh — set PHOTO_INTEL_NOTIFY_CMD
# in the plist, or they are only written to watchdog.log.
#
# WHAT IT CANNOT CATCH
#   This is a LaunchAgent, like every other photo-intel job, so it only runs
#   while you are logged in to the GUI. If the Mac sits at the login window
#   after a reboot, the export does not run AND neither does this watchdog.
#   Closing that gap needs a LaunchDaemon (root, loads at boot without a
#   session), or an external dead-man check fed by this script.
#
# It never takes .export.lock — it only stats things.

cd "$HOME/photo-intel" || exit 1

. "$HOME/photo-intel/photo_intel_lib.sh"

LOG="watchdog.log"
STALE_HOURS=6          # export.log untouched this long = pipeline is dark
HUNG_HOURS=4           # lock held this long = an export is wedged
ALERT_EVERY_H=24       # re-alert cadence per condition, so it does not spam
STATE="$HOME/photo-intel/.watchdog.state"
HOST=$(hostname -s)

now=$(date +%s)

# should_alert <key> — 0 if this condition has not alerted within
# ALERT_EVERY_H. Records the alert time on success.
should_alert() {
    _key="$1"
    _last=$(awk -v k="$_key" '$1==k {print $2}' "$STATE" 2>/dev/null | tail -1)
    if [ -n "$_last" ] && [ $(( now - _last )) -lt $(( ALERT_EVERY_H * 3600 )) ]; then
        return 1
    fi
    clear_alert "$_key"
    printf '%s %s\n' "$_key" "$now" >> "$STATE"
    return 0
}

# clear_alert <key> — forget a condition so the NEXT occurrence alerts at once
# instead of waiting out the cadence from a long-resolved incident.
clear_alert() {
    [ -f "$STATE" ] || return 0
    awk -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp" 2>/dev/null \
        && mv "$STATE.tmp" "$STATE"
}

# ---------------------------------------------------------------------------
# 1. Is the export still reporting in?
# ---------------------------------------------------------------------------
if [ -f export.log ]; then
    log_mtime=$(stat -f%m export.log)
    log_age_h=$(( (now - log_mtime) / 3600 ))
else
    log_mtime=0
    log_age_h=999
fi

if [ "$log_age_h" -ge "$STALE_HOURS" ]; then
    if should_alert export_stale; then
        log_line "ALERT export.log has not been written for ${log_age_h}h"
        notify "photo-intel export has not run in ${log_age_h}h" \
"The 2-hourly photo-intel export has written nothing to export.log for
${log_age_h} hours. Every tick writes a line — including one that skips on the
lock — so a frozen log means the launcher is not running at all, not that
there was nothing to export.

Host:  $HOST
When:  $(date -u +%FT%TZ)
Last export.log write: $(date -r "$log_mtime" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null)

Most likely causes, in order:
  1. A previous run is still alive and holding the job slot — launchd will not
     start a second instance. Check:  ps -o pid,etime,command -p \$(awk 'NR==1{print \$1}' ~/photo-intel/.export.lock/owner 2>/dev/null)
  2. The Mac was at the login window; these are LaunchAgents and need a GUI
     session. Check:  last reboot | head -3
  3. The job is unloaded.  launchctl list | grep photo-intel.export

Then:
    tail -5 ~/photo-intel/export.log
    ls -la ~/photo-intel/.export.lock 2>/dev/null && cat ~/photo-intel/.export.lock/owner"
    fi
else
    clear_alert export_stale
fi

# ---------------------------------------------------------------------------
# 2. Is an export wedged on the lock?
# ---------------------------------------------------------------------------
if [ -d "$LOCK" ]; then
    lock_mtime=$(stat -f%m "$LOCK" 2>/dev/null || echo "$now")
    lock_age_h=$(( (now - lock_mtime) / 3600 ))
    if [ "$lock_age_h" -ge "$HUNG_HOURS" ]; then
        if should_alert lock_hung; then
            log_line "ALERT .export.lock held for ${lock_age_h}h by $(lock_owner)"
            notify "photo-intel export lock held ${lock_age_h}h — export wedged?" \
"$LOCK has been held for ${lock_age_h} hours. The longest routine holder is
the weekly full sweep at roughly 2 h, so this is probably a wedged export
rather than a busy one (unless a post-upgrade full re-export is running).
While it is held, every gated tick skips.

Host:  $HOST
When:  $(date -u +%FT%TZ)
Owner: $(lock_owner)

The launchers reclaim a lock whose owner has died, so a lock this old means the
owning process is still ALIVE and stuck. Identify it before killing it:

    cat ~/photo-intel/.export.lock/owner
    ps -o pid,etime,command -p <pid>
    tail -5 ~/photo-intel/export.log"
        fi
    else
        clear_alert lock_hung
    fi
else
    clear_alert lock_hung
fi

# Quiet success leaves no log line — this runs 12x a day and a heartbeat here
# would just bury the ALERT lines it exists to make findable.
exit 0
