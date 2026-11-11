#!/bin/sh
# Run from the unpacked, hash-verified installation bundle.
set -eu
umask 077
BASE=/Plugin/sr7410-ssh
[ "$(id -u)" = 0 ] || { echo "A root Telnet shell is required" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "Only aarch64 is supported" >&2; exit 1; }
[ -d /Plugin ] && [ -w /Plugin ] || { echo "/Plugin is not writable" >&2; exit 1; }
[ ! -e "$BASE" ] && [ ! -L "$BASE" ] || { echo "$BASE already exists; refusing to overwrite" >&2; exit 1; }
for command in ip mount hexdump cmp tar awk readlink grep tr; do
    command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done
# Refuse other software's runtime relocation links; never replace them.
if [ -e /var/zsl ] || [ -L /var/zsl ]; then
    [ -L /var/zsl ] && [ "$(readlink /var/zsl)" = "$BASE/lib" ] || {
        echo "/var/zsl is used by another installation" >&2; exit 1;
    }
fi
if [ -e /var/zse ] || [ -L /var/zse ]; then
    [ -L /var/zse ] && [ "$(readlink /var/zse)" = /etc ] || {
        echo "/var/zse conflicts with this installation" >&2; exit 1;
    }
fi
[ -s authorized_keys ] && [ -s settings.conf ] && [ -f VERSION ] || {
    echo "Incomplete installation bundle" >&2; exit 1;
}
# Leave room for installation and the persistent optional boot bundle.
available=$(df -k /Plugin | awk 'END {print $4}')
case "$available" in ''|*[!0-9]*) echo "Cannot determine free space" >&2; exit 1;; esac
[ "$available" -ge 8192 ] || { echo "At least 8 MiB free in /Plugin is required" >&2; exit 1; }
mkdir -m 700 "$BASE"
cp -a . "$BASE/"
chmod 700 "$BASE" "$BASE/"*.sh
chmod -R 755 "$BASE/sbin" "$BASE/lib"
chmod 600 "$BASE/authorized_keys" "$BASE/settings.conf"
if ! "$BASE/start.sh"; then
    echo "Files retained for diagnosis. Autostart was NOT enabled." >&2
    exit 1
fi
echo "SSH runtime installed; autostart remains disabled"
