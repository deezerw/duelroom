# duelpc: the gaming PC's spare SSD as a solver box

Ubuntu Server 24.04 LTS (HWE kernel), headless, installed hands-off except
for ONE question: which disk. The server keeps everything that matters
(app, database, results); duelpc only runs solver jobs the server sends it.

## Why this distro

- **Same release as the server.** The server's solver build runs on it
  unchanged (same glibc/libstdc++), so the "same code on both machines"
  check is a version compare, not a rebuild.
- **Kernel 6.7+** keeps the solver's fast page tracker (patch 0003, 1.88x).
- **HWE kernel**: new boards' Realtek 5GbE needs it.
- **Nothing on it needs preserving.** SteamOS's installer re-images the
  whole drive, so no partition survives that anyway. Everything duelpc
  holds is rebuilt from this folder + what the server pushes, so moving to
  any other distro is "install, add the server's SSH key, done".

## Build (on the server)

    tools/duelpc/build.sh

Makes `/mnt/ssdshare/duelpc-install/duelpc-ubuntu-24.04.5.iso` (USB) and
the network-boot files in `/srv/duelpc-netboot`. The login password is in
`~/.duelpc-password` on the server (SSH is key-only; the password is for
the PC's own console and sudo).

## Install over the network

1. PC BIOS: enable **Network stack / PXE (UEFI)** and **Wake-on-LAN**
   ("Power on by PCI-E"; turn **ErP** off). Plug in Ethernet.
2. Server: `tools/duelpc/netinstall.sh start` (on for 3 h, then turns
   itself and its firewall rules off; `stop` to end early).
3. PC: open the boot menu (F8/F11/F12 depending on the board), choose
   the **UEFI PXE / network** entry, then **Install duelpc**. The menu
   defaults to "Boot from local disk" after 20 s, so a machine that
   network-boots by accident installs nothing.
4. Installer: it asks only for the disk ("Guided storage configuration").
   Keep "Use an entire disk", then OPEN THE DISK DROPDOWN under it and pick
   the **empty spare SSD**. It may preselect the Windows drive; check the size
   and model. Done, then confirm the "destructive action" prompt. It downloads the 4 GB image into RAM first (needs 12 GB+ of
   RAM; an 8 GB machine runs out), so allow a few minutes.
5. When it reboots: server `tools/duelpc/find_duelpc.sh` prints its IP
   and MAC. A DHCP reservation for that MAC on the router keeps the IP fixed.

## Install from USB instead

Copy `\\server\ssdshare\duelpc-install\duelpc-ubuntu-24.04.5.iso` to the
PC, flash it with Rufus (DD mode if asked) or balenaEtcher, boot the
stick, choose **Install duelpc**. Same disk question, same result.

## Tested

2026-11-17, in a Secure Boot UEFI VM: network boot -> unattended install
-> SSH in with the server's key: hostname duelpc, kernel 7.0 (HWE),
Wake-on-LAN set, sleep masked, Windows in the boot menu, password SSH
off, the server's solver binary runs unchanged. USB image: menu boots,
stops at the disk question.

## Living with two systems

- The PC now boots **duelpc by default**; the boot menu (5 s) lists
  Windows Boot Manager below it. Linux by default is what lets the server
  wake the PC with Wake-on-LAN and start a job while you're away.
- If you'd rather keep Windows the BIOS default, run
  `reboot-to-duelpc.ps1` (in the share) when you leave: it restarts into
  duelpc for that one boot only.
- Secure Boot can stay on (Ubuntu's boot chain is signed).
- Leave Windows' **Fast Startup** off, or Wake-on-LAN after a Windows
  shutdown may not work.
- **Monitors stay off in Linux** (OLED burn-in): `screen-off.service` powers the
  console down (`echo 4 > /sys/class/graphics/fb0/blank`, DPMS off) once the
  system is up, and `consoleblank=60` in GRUB re-blanks it within a minute if
  anything lights it again. Only the 5 s boot menu and boot messages show.
  Added on the PC by hand 2026-11-19 (not in autoinstall.yaml.in yet).
