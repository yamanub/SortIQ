#!/usr/bin/env bash
# One-time Pi setup for every SortIQ machine, run on the Pi by
# tools/pi_deploy.sh. Safe to re-run: it changes only what is missing, and
# prints REBOOT_NEEDED when a boot setting changed.
#
#   tools/pi_host_setup.sh           apply
#   tools/pi_host_setup.sh --check   report what would change, change nothing
#
# What it sets up (harmless on a CS7.2, which uses none of these pins):
#   - the Pi's header UART for the SKR Pico board link (/dev/serial0),
#     with the serial console moved off it (and Bluetooth off it on a Pi 3/4)
#   - GPIO 18 hardware PWM for the LED ring, re-armed at every boot
#   - I2C for the carousel's PCA9685 door board
#   - a persistent system log, and no USB power saving on the Suyin camera
#   - the 7" touchscreen, only when a screen is plugged into the Pi's HDMI
#     (or was set up before): a kiosk that opens the machine's /screen page
set -euo pipefail
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1
BOOT=/boot/firmware; [ -f $BOOT/config.txt ] || BOOT=/boot
CFG=$BOOT/config.txt; CMD=$BOOT/cmdline.txt
MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
REBOOT=0

say() { echo "   $*"; }
do_() { if [ $CHECK = 1 ]; then say "would: $*"; else eval "$@"; fi; }
active_line() { grep -qE "^[[:space:]]*$1[[:space:]]*$" "$CFG"; }

# config.txt lines go under [all] so no model-specific section swallows them
cfg_line() {
  active_line "$1" && return 0
  say "config.txt: + $1"
  if [ $CHECK = 0 ]; then
    [ "$(grep -E '^\[' "$CFG" | tail -1)" = "[all]" ] || echo "[all]" | sudo tee -a "$CFG" >/dev/null
    echo "$1" | sudo tee -a "$CFG" >/dev/null
  fi
  REBOOT=1
}

echo ">> Pi host setup ($MODEL)"

# board link: the header UART, console off it
cfg_line "enable_uart=1"
case "$MODEL" in
  *"Pi 3"*|*"Pi 4"*)
    cfg_line "dtoverlay=disable-bt"                        # frees the PL011 for GPIO 14/15
    if systemctl is-enabled hciuart.service >/dev/null 2>&1; then
      say "hciuart.service: disable (Bluetooth no longer owns the UART)"
      do_ "sudo systemctl disable hciuart.service >/dev/null 2>&1"
    fi ;;
esac
if grep -qE 'console=(serial0|ttyAMA0|ttyS0),[0-9]+' "$CMD"; then
  say "cmdline.txt: - serial console"
  do_ "sudo sed -i -E 's/console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?//g' '$CMD'"
  REBOOT=1
fi

# LED ring PWM on GPIO 18
cfg_line "dtoverlay=pwm,pin=18,func=2"
UNIT=/etc/systemd/system/ring-pwm.service
WANT_UNIT='[Unit]
Description=SortIQ LED ring PWM (GPIO18, 20kHz)
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '"'"'CH=$(pinctrl get 18 | grep -o "CHAN[0-9]" | tr -dc "0-9"); CH=${CH:-0}; P=/sys/class/pwm/pwmchip0; echo $CH > $P/export 2>/dev/null || true; sleep 0.5; echo 50000 > $P/pwm$CH/period; echo 13750 > $P/pwm$CH/duty_cycle; echo 1 > $P/pwm$CH/enable'"'"'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target'
if [ "$(cat $UNIT 2>/dev/null)" != "$WANT_UNIT" ]; then
  say "ring-pwm.service: install"
  if [ $CHECK = 0 ]; then
    printf '%s\n' "$WANT_UNIT" | sudo tee $UNIT >/dev/null
    sudo systemctl daemon-reload
  fi
fi
systemctl is-enabled ring-pwm.service >/dev/null 2>&1 || {
  say "ring-pwm.service: enable"; do_ "sudo systemctl enable ring-pwm.service >/dev/null 2>&1"; }

# I2C for the carousel's door board (written to config.txt, survives reboots)
if ! active_line "dtparam=i2c_arm=on"; then
  say "I2C: enable"
  if [ $CHECK = 0 ]; then
    if command -v raspi-config >/dev/null; then
      sudo raspi-config nonint do_i2c 0
    else
      cfg_line "dtparam=i2c_arm=on"
      grep -qx i2c-dev /etc/modules || echo i2c-dev | sudo tee -a /etc/modules >/dev/null
    fi
  fi
  REBOOT=1
fi
[ -x /usr/sbin/i2cdetect ] || { say "apt: i2c-tools"; do_ "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq i2c-tools >/dev/null 2>&1"; }

# the app's user needs the serial port, I2C and GPIO
for g in dialout i2c gpio; do
  if getent group $g >/dev/null && ! id -nG "$USER" | tr ' ' '\n' | grep -qx $g; then
    say "groups: + $g"; do_ "sudo usermod -aG $g '$USER'"; REBOOT=1
  fi
done

# keep the system log across reboots
JCONF=/etc/systemd/journald.conf.d/50-sortiq-persistent.conf
if [ ! -f $JCONF ]; then
  say "journald: persistent"
  do_ "sudo mkdir -p /etc/systemd/journald.conf.d /var/log/journal && printf '[Journal]\nStorage=persistent\n' | sudo tee $JCONF >/dev/null && sudo systemctl restart systemd-journald"
fi

# never power-manage the Suyin HD camera
RULE=/etc/udev/rules.d/50-sortiq-camera.rules
if [ ! -f $RULE ]; then
  say "udev: Suyin camera power saving off"
  do_ "printf '%s\n' 'ACTION==\"add\", SUBSYSTEM==\"usb\", ATTR{idVendor}==\"1e45\", ATTR{idProduct}==\"8022\", TEST==\"power/control\", ATTR{power/control}=\"on\"' | sudo tee $RULE >/dev/null && sudo udevadm control --reload"
fi

# the 7" touchscreen: a kiosk (cage + Chromium) on the Pi's HDMI showing
# the app's /screen page. Set up when a screen is plugged in, kept up to
# date once it has been
KUNIT=/etc/systemd/system/sortiq-kiosk.service
SCREEN=0
[ -f $KUNIT ] && SCREEN=1
for s in /sys/class/drm/card*-HDMI-A-*/status; do
  if [ "$(cat "$s" 2>/dev/null)" = connected ]; then SCREEN=1; fi
done
if [ $SCREEN = 1 ]; then
  for p in cage wlr-randr; do
    command -v $p >/dev/null || { say "apt: $p"; do_ "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $p >/dev/null 2>&1"; }
  done
  if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
    say "apt: chromium"
    do_ "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium >/dev/null 2>&1 || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium-browser >/dev/null 2>&1"
  fi
  CHROME="$(command -v chromium || command -v chromium-browser || echo /usr/bin/chromium)"
  # the screen's own mode, and HDMI held on even if its hotplug wire flickers
  for t in "video=HDMI-A-1:1024x600M@60D" "vc4.force_hotplug=1"; do
    if ! grep -qF "$t" "$CMD"; then
      say "cmdline.txt: + $t"; do_ "sudo sed -i '1 s/\$/ $t/' '$CMD'"; REBOOT=1
    fi
  done
  # the app reads touches straight from the input devices to wake the screen
  if getent group input >/dev/null && ! id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
    say "groups: + input"; do_ "sudo usermod -aG input '$USER'"; REBOOT=1
  fi
  if [ ! -f "$HOME/.icons/default/index.theme" ]; then
    say "cursor: hide the pointer"; do_ "python3 '$HOME/SortIQ/tools/kiosk_cursor.py'"
  fi
  WANT_KIOSK='[Unit]
Description=SortIQ touchscreen (cage + Chromium kiosk)
After=systemd-user-sessions.service sortiq.service network.target
Wants=sortiq.service
Conflicts=getty@tty1.service

[Service]
User=@USER@
PAMName=login
TTYPath=/dev/tty1
StandardInput=tty-fail
StandardOutput=journal
StandardError=journal
UtmpIdentifier=tty1
UtmpMode=user
Environment=XCURSOR_THEME=invisible XCURSOR_SIZE=24
# wait for the app to answer before opening the page
ExecStartPre=/bin/sh -c '"'"'for i in $(seq 1 60); do curl -s -o /dev/null http://127.0.0.1:5000/ && exit 0; sleep 1; done; exit 0'"'"'
ExecStart=/usr/bin/cage -s -- @CHROME@ --kiosk --ozone-platform=wayland \
  --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \
  --disable-features=TranslateUI --overscroll-history-navigation=0 --disable-pinch \
  --password-store=basic --check-for-update-interval=31536000 --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows \
  --user-data-dir=@HOME@/.config/sortiq-kiosk \
  http://127.0.0.1:5000/screen?kiosk
Restart=always
RestartSec=3

[Install]
WantedBy=graphical.target multi-user.target'
  WANT_KIOSK="${WANT_KIOSK//@USER@/$USER}"
  WANT_KIOSK="${WANT_KIOSK//@HOME@/$HOME}"
  WANT_KIOSK="${WANT_KIOSK//@CHROME@/$CHROME}"
  if [ "$(cat $KUNIT 2>/dev/null)" != "$WANT_KIOSK" ]; then
    say "sortiq-kiosk.service: install"
    if [ $CHECK = 0 ]; then
      printf '%s\n' "$WANT_KIOSK" | sudo tee $KUNIT >/dev/null
      sudo systemctl daemon-reload
      sudo systemctl enable sortiq-kiosk.service >/dev/null 2>&1
      [ $REBOOT = 1 ] || sudo systemctl restart --no-block sortiq-kiosk.service
    fi
  fi
  systemctl is-enabled sortiq-kiosk.service >/dev/null 2>&1 || {
    say "sortiq-kiosk.service: enable"; do_ "sudo systemctl enable --now sortiq-kiosk.service >/dev/null 2>&1"; }
fi

if [ $REBOOT = 1 ]; then echo "REBOOT_NEEDED"; else say "nothing to change"; fi
