#!/usr/bin/env bash
# Flash the SKR Pico from the Pi over its USB cable.
#
#   tools/flash_pico.sh               the build this machine already runs
#   tools/flash_pico.sh carousel      SortIQ-PicoServo (servo carousel)
#   tools/flash_pico.sh arm           SortIQ-Pico (sort arm)
#   tools/flash_pico.sh file.uf2      any image
#
# A board already running SortIQ firmware is asked to reboot into its
# bootloader over the app's link, so no jumper is needed. A blank board
# must be put in the bootloader by hand once: fit the BOOT jumper, power
# the board, run this, then REMOVE the jumper before the next power cycle.
# The board's logic runs off 24 V, not USB: it must be powered.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
pick() { ls "$HERE/firmware/$1"/*.uf2 2>/dev/null | sort -V | tail -1; }

case "${1:-}" in
  carousel) UF2="$(pick SortIQ_PicoServo)" ;;
  arm)      UF2="$(pick SortIQ_Pico)" ;;
  "")
    KIND="$(python3 -c 'import json;print(json.load(open("'"$HERE"'/config.json")).get("machine",{}).get("firmware",""))' 2>/dev/null || true)"
    case "$KIND" in
      pico-servo) UF2="$(pick SortIQ_PicoServo)" ;;
      pico)       UF2="$(pick SortIQ_Pico)" ;;
      *) echo "Which build? Run: $0 carousel   or   $0 arm" >&2; exit 1 ;;
    esac ;;
  *) UF2="$1" ;;
esac
[ -n "$UF2" ] && [ -f "$UF2" ] || { echo "no firmware image found (${UF2:-none})" >&2; exit 1; }
echo "image: $UF2"

if [ ! -e /dev/disk/by-label/RPI-RP2 ]; then
  echo "asking the board to enter its bootloader..."
  curl -s -X POST localhost:5000/api/console/send -H "Content-Type: application/json" \
    -d '{"line":"bootloader"}' >/dev/null || true
fi
echo "waiting for the RPI-RP2 drive..."
for _ in $(seq 1 30); do
  [ -e /dev/disk/by-label/RPI-RP2 ] && break
  sleep 1
done
if [ ! -e /dev/disk/by-label/RPI-RP2 ]; then
  echo "The board never showed up as RPI-RP2." >&2
  echo "Check that it has 24 V power and its USB cable goes to the Pi. A blank" >&2
  echo "board needs the BOOT jumper fitted before power-on." >&2
  exit 2
fi
sudo mkdir -p /mnt/rp2
sudo mount /dev/disk/by-label/RPI-RP2 /mnt/rp2
sudo cp "$UF2" /mnt/rp2/firmware.uf2 || true   # the drive drops as the copy lands
sync || true
sudo umount /mnt/rp2 2>/dev/null || true
echo "flashed. If you used the BOOT jumper, remove it now."

# A flash can land while the app is still sending the board its settings
# (a flash right after power-on). Reconnecting once the new firmware is up
# makes the app send every setting again, whatever the timing was.
echo "waiting for the new firmware to start..."
for _ in $(seq 1 20); do
  [ -e /dev/disk/by-label/RPI-RP2 ] || break
  sleep 1
done
sleep 6
if curl -s -m 90 -X POST localhost:5000/api/console/connect \
     -H "Content-Type: application/json" -d '{"mode":"serial"}' | grep -q '"ok"'; then
  echo "the app reconnected and sent the board every setting."
else
  echo "the board is not answering yet; the app connects on its own once it does."
fi
