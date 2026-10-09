#!/bin/sh
# photo_intel_lib.sh — shared helpers for the photo-intel launchers on the Mac.
# Sourced (never executed) by run_export.sh, run_export_full.sh and
# export_watchdog.sh. Deploy it next to them in ~/photo-intel/.
#
#   lock_acquire [wait_s] [label]   take .export.lock, optionally waiting;
#                                   reclaims a lock whose owner is gone
#   lock_release                    drop a lock THIS process took
#   log_line <text>                 timestamped append to $LOG
#   notify <subject> <body>         alert via $PHOTO_INTEL_NOTIFY_CMD, if set
#
# STALE LOCKS — why this file exists
#   .export.lock is a directory; mkdir is the atomic test-and-set. Earlier
#   versions never reclaimed one, so a hung or SIGKILLed export left the
#   directory behind forever and every later run skipped on it.
#
#   That happened in practice: an export hung holding the lock, launchd would
#   not start a second instance of the same job while the first was alive, and
#   the pipeline went dark for three days with NO export.log output at all —
#   no runs and no "skipping" lines either.
#
#   So the lock now records its holder in .export.lock/owner:
#       <pid> <ISO8601 UTC> <label>
#   and a would-be taker reclaims it when that pid is gone — or is alive but is
#   no longer a photo-intel process, since pids get reused. A lock with no owner
#   file (an older version's, or a hand run's) is reclaimed on age alone after
#   LOCK_STALE_HOURS; the weekly full sweep is the longest legitimate holder at
#   roughly 2 h, so 6 h is comfortably clear of it.

LOCK="${LOCK:-$HOME/photo-intel/.export.lock}"
LOCK_STALE_HOURS="${LOCK_STALE_HOURS:-6}"
LOCK_HELD=0

# log_line <text> — timestamped, to $LOG if the caller set one, else stderr.
log_line() {
    if [ -n "$LOG" ]; then
        echo "$(date -u +%FT%TZ) $1" >> "$LOG"
    else
        echo "$(date -u +%FT%TZ) $1" >&2
    fi
}

lock_owner() {
    cat "$LOCK/owner" 2>/dev/null || echo "none recorded"
}

# _lock_owner_alive — 0 if the lock is held by a live photo-intel process,
# 1 if it is stale and safe to reclaim.
_lock_owner_alive() {
    if [ ! -f "$LOCK/owner" ]; then
        _age=$(( $(date +%s) - $(stat -f%m "$LOCK" 2>/dev/null || date +%s) ))
        [ "$_age" -lt $(( LOCK_STALE_HOURS * 3600 )) ]
        return $?
    fi
    _pid=$(awk 'NR==1 {print $1}' "$LOCK/owner" 2>/dev/null)
    case "$_pid" in
        ''|*[!0-9]*) return 1 ;;   # unreadable or garbage -> reclaimable
    esac
    kill -0 "$_pid" 2>/dev/null || return 1
    # Alive, but pids are reused. Only ours if it still looks like the pipeline.
    # Targeted at one pid, so this cannot self-match the way `pgrep -f` would.
    ps -o command= -p "$_pid" 2>/dev/null | grep -q 'photo[-_]intel' || return 1
    return 0
}

# lock_acquire [wait_s] [label] — 0 acquired (caller must lock_release),
# 1 held by a live run for the whole wait.
lock_acquire() {
    _wait="${1:-0}"
    _label="${2:-$(basename "$0")}"
    _waited=0
    _reclaims=0
    while :; do
        if mkdir "$LOCK" 2>/dev/null; then
            printf '%s %s %s\n' "$$" "$(date -u +%FT%TZ)" "$_label" \
                > "$LOCK/owner"
            LOCK_HELD=1
            return 0
        fi
        if ! _lock_owner_alive; then
            _reclaims=$(( _reclaims + 1 ))
            if [ "$_reclaims" -gt 3 ]; then
                log_line "ERROR could not reclaim stale $LOCK after 3 attempts"
                return 1
            fi
            log_line "reclaiming stale lock $LOCK (owner: $(lock_owner))"
            rm -rf "$LOCK"
            continue
        fi
        [ "$_waited" -ge "$_wait" ] && return 1
        sleep 10
        _waited=$(( _waited + 10 ))
    done
}

# lock_release — only ever removes a lock this process actually took, so a
# failed acquire can never delete the live holder's lock.
lock_release() {
    [ "$LOCK_HELD" = 1 ] || return 0
    rm -rf "$LOCK"
    LOCK_HELD=0
}

# notify <subject> <body> — runs $PHOTO_INTEL_NOTIFY_CMD with the subject as
# its one argument and the body on stdin. Set it in the launchd plist's
# EnvironmentVariables to the path of a small script that delivers a message —
# one that posts to ntfy, Pushover or Slack, or pipes into sendmail on a host
# with a working MTA (stock macOS has none configured).
# Unset, the alert is only logged. Best-effort: a notification failure never
# fails the caller, but it IS logged — a notifier that fails silently is
# indistinguishable from one that has nothing to say.
notify() {
    if [ -z "$PHOTO_INTEL_NOTIFY_CMD" ]; then
        log_line "notify: PHOTO_INTEL_NOTIFY_CMD not set; alert logged only: $1"
        return 0
    fi
    printf '%s\n' "$2" | "$PHOTO_INTEL_NOTIFY_CMD" "$1"
    # Capture immediately: a second $? would report the `if` test, not the command.
    mrc=$?
    if [ "$mrc" -eq 0 ]; then
        log_line "notify: sent"
    else
        log_line "notify: FAILED (exit $mrc) — NO notification delivered"
    fi
}
