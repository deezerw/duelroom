#!/usr/bin/env bash
# Network-install server for duelpc, OFF unless started:
#   netinstall.sh start   proxy DHCP + TFTP + HTTP for 3 hours, then auto-off
#   netinstall.sh stop
#   netinstall.sh status  (and the boot log: journalctl -u duelpc-pxe)
# Build the files first with build.sh. LAN only (ufw rules from 10.0.0.0/24,
# removed again on stop).
set -euo pipefail
NET=/srv/duelpc-netboot
PORT=8099
LAN=10.0.0.0/24
HOURS=3h
rules=("67/udp" "69/udp" "4011/udp" "$PORT/tcp")

ufw_rules() {  # $1 = allow | delete allow
    # A booting PC asks for DHCP from 0.0.0.0, outside $LAN: measured
    # 2026-11-17, the LAN-only rule dropped every request ("no valid offer").
    sudo ufw $1 in on enp1s0 to any port 67 proto udp comment 'duelpc netinstall' >/dev/null 2>&1 || true
    for r in "${rules[@]}"; do
        sudo ufw $1 from $LAN to any port "${r%/*}" proto "${r#*/}" comment 'duelpc netinstall' >/dev/null 2>&1 || true
    done
}

case "${1:-status}" in
start)
    [ -f "$NET/dnsmasq.conf" ] && [ -e "$NET/http/duelpc.iso" ] || { echo "run build.sh first"; exit 1; }
    ufw_rules allow
    sudo systemd-run --unit duelpc-pxe -p RuntimeMaxSec=$HOURS \
        -p ExecStopPost="$(realpath "$0") _cleanup" \
        dnsmasq --keep-in-foreground --conf-file="$NET/dnsmasq.conf" --log-facility=-
    sudo systemd-run --unit duelpc-http -p RuntimeMaxSec=$HOURS -p User="$USER" \
        -p WorkingDirectory="$NET/http" python3 -m http.server $PORT --bind 0.0.0.0
    sleep 1; "$0" status
    echo "on for $HOURS. On the PC: boot menu -> network/PXE (UEFI) boot,"
    echo "then pick 'Install duelpc'. Watch: journalctl -fu duelpc-pxe"
    ;;
stop)
    sudo systemctl stop duelpc-pxe duelpc-http 2>/dev/null || true
    ufw_rules "delete allow"
    echo "network install off"
    ;;
_cleanup)  # runs as root when the 3 h timer (or stop) ends the PXE unit
    systemctl stop duelpc-http 2>/dev/null || true
    ufw delete allow in on enp1s0 to any port 67 proto udp >/dev/null 2>&1 || true
    for r in "${rules[@]}"; do
        ufw delete allow from $LAN to any port "${r%/*}" proto "${r#*/}" >/dev/null 2>&1 || true
    done
    ;;
status)
    for u in duelpc-pxe duelpc-http; do printf '%-12s %s\n' "$u" "$(systemctl is-active $u 2>/dev/null)"; done
    ;;
*) echo "usage: $0 start|stop|status"; exit 2 ;;
esac
