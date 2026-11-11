#!/bin/sh
# Called by the host installer after full byte read-back verification.
set -eu
umask 077
BASE=/Plugin/sr7410-ssh
STOCK=/etc/dlspeed_rt.tar.gz
OVERRIDE=/Plugin/apps/dlspeed_rt.tar.gz
STATE="$BASE/boot-created.tar.gz"
action=status
[ "$#" -eq 0 ] || action=$1
verify_owned() {
    [ -f "$STATE" ] && [ -f "$OVERRIDE" ] && [ ! -L "$OVERRIDE" ] &&
        cmp -s "$OVERRIDE" "$STATE"
}
case "$action" in
enable)
    [ "$(id -u)" = 0 ] && [ -f "$BASE/VERSION" ] || exit 1
    if [ -f "$STATE" ]; then
        verify_owned || { echo "Boot bundle changed; refusing to overwrite it" >&2; exit 1; }
        tar -zxf "$OVERRIDE" -C /Plugin/
        chmod 755 /Plugin/dlspeed_rt/dlspeed_monitor.sh 2>/dev/null || true
        touch "$BASE/autostart.enabled"
        echo "Autostart already configured"
        exit 0
    fi
    [ "$#" -eq 2 ] || { echo "Use scripts/router.py enable-autostart" >&2; exit 1; }
    source_dir=$2
    case "$source_dir" in /var/sr7410-setup-*) ;; *) exit 1;; esac
    [ ! -e "$OVERRIDE" ] && [ ! -L "$OVERRIDE" ] || {
        echo "An existing override belongs to another installation; left untouched" >&2; exit 1;
    }
    if [ ! -f "$STOCK" ] || [ -L "$STOCK" ] ||
        ! cmp -s "$STOCK" "$source_dir/stock.tar.gz"; then
        echo "Stock component changed after host validation" >&2
        exit 1
    fi
    (grep -Fq '/apps/apps/dlspeed_rt.tar.gz' /etc/init.norm 2>/dev/null ||
     grep -Fq '/apps/apps/dlspeed_rt.tar.gz' /etc/dlspeed_tar.sh 2>/dev/null) &&
        [ "$(readlink /apps)" = /Plugin ] || {
        echo "Expected vendor startup override was not found" >&2; exit 1;
    }
    [ -s "$source_dir/override.tar.gz" ] || exit 1
    available=$(df -k /Plugin | awk 'END {print $4}')
    case "$available" in ''|*[!0-9]*) exit 1;; esac
    [ "$available" -ge 4096 ] || { echo "At least 4 MiB free space is required" >&2; exit 1; }
    mkdir -p /Plugin/apps
    [ ! -e "$OVERRIDE.new" ] && [ ! -L "$OVERRIDE.new" ] || exit 1
    cp -p "$STOCK" "$BASE/stock-dlspeed_rt.tar.gz"
    cp "$source_dir/override.tar.gz" "$STATE"
    cp "$STATE" "$OVERRIDE.new"
    chmod 600 "$OVERRIDE.new"
    mv "$OVERRIDE.new" "$OVERRIDE"
    # In V1.0.0.8B1.8000, /etc/dlspeed_tar.sh skips unpacking if /Plugin/dlspeed_rt exists.
    # Unpack immediately into persistent /Plugin flash so dlspeed_monitor.sh has the hook right away.
    tar -zxf "$OVERRIDE" -C /Plugin/
    chmod 755 /Plugin/dlspeed_rt/dlspeed_monitor.sh 2>/dev/null || true
    touch "$BASE/autostart.enabled"
    echo "Autostart enabled; reboot separately when ready"
    ;;
disable)
    rm -f "$BASE/autostart.enabled"
    if [ -f "$STATE" ]; then
        if [ ! -e "$OVERRIDE" ] && [ ! -L "$OVERRIDE" ]; then
            rm -f "$STATE"
        else
            verify_owned || { echo "Override changed; flag disabled, archive left untouched" >&2; exit 1; }
            rm -f "$OVERRIDE" "$STATE"
        fi
    fi
    # Restore stock files to persistent flash
    if [ -f "$STOCK" ]; then
        tar -zxf "$STOCK" -C /Plugin/
        chmod 755 /Plugin/dlspeed_rt/dlspeed_monitor.sh 2>/dev/null || true
    fi
    echo "Autostart disabled"
    ;;
status)
    if [ -f "$BASE/autostart.enabled" ] && verify_owned; then
        echo "Autostart enabled (reboot verification still required)"
    else
        echo "Autostart disabled or bundle changed"
    fi
    ;;
*) echo "Usage: boot.sh enable|disable|status" >&2; exit 2;;
esac
