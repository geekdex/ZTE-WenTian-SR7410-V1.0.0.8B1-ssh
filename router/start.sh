#!/bin/sh
# Dedicated runtime only. Never change the firmware's libc, /etc/passwd or /dev.
set -eu
umask 077
BASE=/Plugin/sr7410-ssh
RUN=/var/sr7410-ssh
mkdir -p "$RUN"
[ -f "$BASE/settings.conf" ] || { echo "Missing settings.conf" >&2; exit 1; }
# shellcheck source=/dev/null
. "$BASE/settings.conf"
case "$SSH_PORT" in ''|*[!0-9]*) exit 1;; esac
mkdir "$RUN/start.lock" 2>/dev/null || {
    echo "Startup is already running or a stale /var/sr7410-ssh/start.lock exists" >&2
    exit 1
}
trap 'rmdir "$RUN/start.lock"' EXIT

link_checked() {
    target=$1
    link=$2
    if [ -L "$link" ]; then
        [ "$(readlink "$link")" = "$target" ] || {
            echo "Conflicting runtime link: $link" >&2; exit 1;
        }
    elif [ -e "$link" ]; then
        echo "Existing path conflicts with runtime: $link" >&2
        exit 1
    else
        ln -s "$target" "$link"
    fi
}

# Prepare PTYs even when an SSH daemon is already running.
mkdir -p /var/pts
if ! grep -q ' /var/pts devpts ' /proc/mounts; then
    mount -t devpts -o newinstance,ptmxmode=0666,mode=0620,gid=0 devpts /var/pts
fi
link_checked /var/pts/ptmx /var/ptmx
link_checked "$BASE/lib" /var/zsl
link_checked /etc /var/zse
if [ -f "$RUN/dropbear.pid" ]; then
    pid=$(cat "$RUN/dropbear.pid")
    case "$pid" in ''|*[!0-9]*) echo "Invalid PID file" >&2; exit 1;; esac
    if kill -0 "$pid" 2>/dev/null; then
        tr '\000' ' ' < "/proc/$pid/cmdline" | grep -Fq "$BASE/sbin/dropbear" || {
            echo "PID file belongs to another process" >&2; exit 1;
        }
        exit 0
    fi
    rm -f "$RUN/dropbear.pid"
fi
attempt=0
until ip -4 addr show br0 2>/dev/null | grep -Fq "inet $SSH_LAN_IP/"; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 120 ] || { echo "LAN address not ready" >&2; exit 1; }
    sleep 1
done
if [ ! -s "$BASE/dropbear_ed25519_host_key" ]; then
    "$BASE/sbin/dropbearkey" -t ed25519 -f "$BASE/dropbear_ed25519_host_key" >/dev/null 2>&1
fi
chmod 600 "$BASE/dropbear_ed25519_host_key" "$BASE/authorized_keys"
"$BASE/sbin/dropbear" -F -E -s -D "$BASE" \
    -r "$BASE/dropbear_ed25519_host_key" \
    -p "$SSH_LAN_IP:$SSH_PORT" -p "127.0.0.1:$SSH_PORT" \
    -P "$RUN/dropbear.pid" </dev/null >/dev/null 2>&1 &
attempt=0
until [ -s "$RUN/dropbear.pid" ]; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 10 ] || { echo "SSH did not start" >&2; exit 1; }
    sleep 1
done
pid=$(cat "$RUN/dropbear.pid")
kill -0 "$pid" || exit 1
