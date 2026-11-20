#!/bin/sh
# Monitors off in Linux (OLED burn-in). A one-shot blank at boot did not hold
# (2026-11-20: both screens came back on), so this keeps checking: any connected
# output whose DPMS reads On is powered down again.
while :; do
  for c in /sys/class/drm/card*-*; do
    [ "$(cat "$c/status" 2>/dev/null)" = connected ] || continue
    if [ "$(cat "$c/dpms" 2>/dev/null)" = On ]; then
      echo 4 > /sys/class/graphics/fb0/blank 2>/dev/null
      TERM=linux setterm --blank force --powersave powerdown --powerdown 1 </dev/tty1 >/dev/tty1 2>/dev/null
      break
    fi
  done
  sleep 5
done
