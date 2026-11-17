#!/usr/bin/env bash
# Find duelpc on the LAN after it installs (it takes a DHCP address): tries
# the server's key against every host with SSH open. Prints IP and MAC
# (the MAC is what Wake-on-LAN needs). Host keys are not checked here: the
# live installer answers SSH on the same IP with a different key.
set -uo pipefail
NET=${1:-10.0.0}
for i in $(seq 2 254); do
    ip=$NET.$i
    timeout 0.3 bash -c "echo >/dev/tcp/$ip/22" 2>/dev/null || continue
    name=$(ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
           deezer@$ip hostname 2>/dev/null) || continue
    if [ "$name" = duelpc ]; then
        mac=$(ip neigh show "$ip" | awk '{print $5}')
        echo "duelpc  ip $ip  mac $mac"
        exit 0
    fi
done
echo "duelpc not found on $NET.0/24 (is it on, and booted into Linux?)"; exit 1
