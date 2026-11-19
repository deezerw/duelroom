#!/bin/sh
# Who turned this PC on? (user, 2026-11-19: "if it is booted by the network it boots into
# linux and if i hit the power button it does windows".) The firmware does not say, so the
# server does: it notes every Wake-on-LAN it sends (skills/remote.py `note_wake`) and answers
# /duelpc/boot with "linux" for a boot it asked for, "windows" otherwise. Anything but
# "linux" -- the user's power button, or no server -- reboots straight into Windows.
SERVER=${DUELPC_SERVER:-https://10.0.0.96:8446}
WINDOWS="Windows Boot Manager (on /dev/nvme0n1p1)"
answer=""
i=0
while [ $i -lt 12 ]; do                   # up to a minute for the network to come up
  answer=$(curl -sk --max-time 4 "$SERVER/duelpc/boot" 2>/dev/null)
  [ -n "$answer" ] && break
  i=$((i + 1)); sleep 5
done
logger -t boot-choice "server says: ${answer:-nothing}"
[ "$answer" = "linux" ] && exit 0
grub-reboot "$WINDOWS" && systemctl reboot
