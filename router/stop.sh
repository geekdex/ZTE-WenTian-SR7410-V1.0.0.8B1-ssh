#!/bin/sh
set -eu
PIDFILE=/var/sr7410-ssh/dropbear.pid
BASE=/Plugin/sr7410-ssh
[ -f "$PIDFILE" ] || exit 0
pid=$(cat "$PIDFILE")
case "$pid" in ''|*[!0-9]*) echo "Invalid PID file" >&2; exit 1;; esac
if kill -0 "$pid" 2>/dev/null; then
    tr '\000' ' ' < "/proc/$pid/cmdline" | grep -Fq "$BASE/sbin/dropbear" || {
        echo "Refusing to stop unrelated process" >&2; exit 1;
    }
    kill "$pid"
    attempt=0
    while kill -0 "$pid" 2>/dev/null; do
        attempt=$((attempt + 1))
        [ "$attempt" -lt 10 ] || { echo "SSH process is still running" >&2; exit 1; }
        sleep 1
    done
fi
rm -f "$PIDFILE"
