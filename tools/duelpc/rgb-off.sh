#!/bin/sh
# All RGB black (user: the lights bother them most). OpenRGB 1.1 AppImage, extracted.
# OpenRGB exits 0 even when a mode is missing, so every device gets both modes:
# static (motherboard) and direct (mouse, keyboard, ARGB fan headers, RAM).
# Fan header lengths (ARGB_V2 zones, 120 LEDs) are kept in /root/.config/OpenRGB.
# RAM needs the SMBus: acpi_enforce_resources=lax on the kernel line.
modprobe i2c-dev 2>/dev/null; modprobe i2c-piix4 2>/dev/null
cd /home/deezer/openrgb/squashfs-root || exit 0
export QT_QPA_PLATFORM=offscreen
n=$(./AppRun --noautoconnect --list-devices 2>/dev/null | grep -cE "^[0-9]+:")
i=0
while [ "$i" -lt "$n" ]; do
  ./AppRun --noautoconnect -d $i --mode static --color 000000 >/dev/null 2>&1
  ./AppRun --noautoconnect -d $i --mode direct --color 000000 >/dev/null 2>&1
  i=$((i + 1))
done
echo "rgb-off: $n devices set black"
