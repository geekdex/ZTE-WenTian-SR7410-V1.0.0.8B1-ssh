#!/bin/sh
set -eu
BASE=/Plugin/sr7410-ssh
[ "$(id -u)" = 0 ] && [ -f "$BASE/VERSION" ] && [ ! -L "$BASE" ] || exit 1
"$BASE/boot.sh" disable
"$BASE/stop.sh"
if [ -L /var/zsl ] && [ "$(readlink /var/zsl)" = "$BASE/lib" ]; then
    rm /var/zsl
fi
rm -rf "$BASE"
echo "SSH removed. RAM PTY mount remains until reboot. Telnet settings are unchanged."
