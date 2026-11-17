#!/usr/bin/env bash
# Build the duelpc installers from the stock Ubuntu Server ISO:
#   USB:     $SHARE/duelpc-ubuntu-$REL.iso  (flash with Rufus or balenaEtcher)
#   network: $NET/tftp + $NET/http, served by netinstall.sh
# Re-runnable; rebuild after editing autoinstall.yaml.in.
set -euo pipefail

REL=24.04.5
SERVER_IP=10.0.0.96
HTTP_PORT=8099
SHARE=/mnt/ssdshare/duelpc-install
NET=/srv/duelpc-netboot
STOCK=$SHARE/ubuntu-$REL-live-server-amd64.iso
OUT=$SHARE/duelpc-ubuntu-$REL.iso
HASH_FILE=$HOME/.duelpc-password.hash
KEY_FILE=$HOME/.ssh/id_ed25519.pub
HERE=$(cd "$(dirname "$0")" && pwd)

[ -f "$STOCK" ] || { echo "missing $STOCK (download it from releases.ubuntu.com)"; exit 1; }
[ -f "$HASH_FILE" ] || { echo "missing $HASH_FILE (openssl passwd -6 > it)"; exit 1; }
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT

# 1. the answers file, secrets filled in here only
mkdir -p "$NET/http"
sed -e "s|@PWHASH@|$(cat "$HASH_FILE")|" \
    -e "s|@SSHKEY@|$(cat "$KEY_FILE")|" \
    -e "s|@RELEASE@|duelpc-ubuntu-$REL|" \
    "$HERE/autoinstall.yaml.in" > "$work/autoinstall.yaml"
python3 -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$work/autoinstall.yaml"
install -m 644 "$work/autoinstall.yaml" "$NET/http/autoinstall.yaml"

# 2. USB image: the stock ISO plus /autoinstall.yaml (24.04's installer reads
#    it from the install media) and a boot menu that starts it on the HWE kernel
cat > "$work/grub.cfg" <<'CFG'
set timeout=30
loadfont unicode
set menu_color_normal=white/black
set menu_color_highlight=black/light-gray

menuentry "Install duelpc (Ubuntu Server, asks which disk)" {
	set gfxpayload=keep
	linux	/casper/hwe-vmlinuz autoinstall ---
	initrd	/casper/hwe-initrd
}
menuentry 'Boot from next volume' {
	exit 1
}
menuentry 'UEFI Firmware Settings' {
	fwsetup
}
CFG
rm -f "$OUT"
xorriso -indev "$STOCK" -outdev "$OUT" \
    -map "$work/autoinstall.yaml" /autoinstall.yaml \
    -map "$work/grub.cfg" /boot/grub/grub.cfg \
    -boot_image any replay 2>&1 | grep -E 'Writing to|FAILURE|SORRY' || true
[ -s "$OUT" ] || { echo "ISO build failed"; exit 1; }

# 3. network boot: signed shim + net-capable GRUB from Ubuntu's netboot
#    tarball, but the ISO's OWN kernel + initrd (the tarball's kernel is a
#    different build from the ISO's modules)
tar -xzf "$NET/ubuntu-$REL-netboot-amd64.tar.gz" -C "$NET/tftp"
xorriso -osirrox on -indev "$STOCK" \
    -extract /casper/hwe-vmlinuz "$NET/tftp/amd64/hwe-vmlinuz" \
    -extract /casper/hwe-initrd "$NET/tftp/amd64/hwe-initrd" 2>/dev/null
chmod 644 "$NET/tftp/amd64/hwe-vmlinuz" "$NET/tftp/amd64/hwe-initrd"
ln -sf "$OUT" "$NET/http/duelpc.iso"
# Ubuntu's signed network GRUB reads (tftp)/grub/grub.cfg, at the TFTP
# ROOT (measured in a VM: amd64/grub/ is never looked at), so absolute paths.
mkdir -p "$NET/tftp/grub"
cat > "$NET/tftp/grub/grub.cfg" <<CFG
# Served to ANY machine that network-boots while netinstall.sh is on, so the
# default is to leave: nothing installs unless someone picks it.
set timeout=20
set default=0
menuentry "Boot from local disk" {
	exit 1
}
menuentry "Install duelpc (Ubuntu Server, asks which disk)" {
	set gfxpayload=keep
	linux	/amd64/hwe-vmlinuz ip=dhcp iso-url=http://$SERVER_IP:$HTTP_PORT/duelpc.iso cloud-config-url=/dev/null autoinstall ---
	initrd	/amd64/hwe-initrd
}
CFG
cat > "$NET/dnsmasq.conf" <<CFG
# proxy DHCP: the router still hands out addresses; this only adds the
# network-boot answer. No DNS (port=0) -- Pi-hole owns 53.
port=0
interface=enp1s0
bind-interfaces
dhcp-range=${SERVER_IP%.*}.0,proxy
pxe-service=X86-64_EFI,"duelpc install",amd64/bootx64.efi
pxe-service=BC_EFI,"duelpc install",amd64/bootx64.efi
enable-tftp
tftp-root=$NET/tftp
log-dhcp
CFG
echo "USB image : $OUT ($(du -h "$OUT" | cut -f1))"
echo "network   : $NET (start with netinstall.sh start)"
