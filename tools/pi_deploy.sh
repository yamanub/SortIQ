#!/usr/bin/env bash
# Deploy SortIQ to a Raspberry Pi from your workstation, over SSH.
# The repo is private, so nothing is cloned on the Pi — the code is rsynced
# from this machine and the Pi never holds any credentials.
#
#   Usage:   tools/pi_deploy.sh [user@host]      (default: pisortiq@pisortiq.local)
#
# Safe to re-run any time: code updates are synced, but the Pi's own
# config.json (machine settings, serial port) and collected images under
# calibers/ are never overwritten or deleted.
#
# Pis installed under the old ShellSorter name are migrated automatically:
# ~/ShellSorter moves to ~/SortIQ (dataset, config, and models ride along),
# the shellsorter service is retired, and the venv is rebuilt (its scripts
# hard-code the old path).
set -euo pipefail
TARGET="${1:-pisortiq@pisortiq.local}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

# ---- pre-flight: the script needs passwordless SSH and passwordless sudo ----
# (field-reported #4/#5: a Pi user created with a password, or by adduser,
# has neither — and the failures mid-script are cryptic. Check both up
# front and say exactly what to do.)
echo ">> checking SSH access to $TARGET"
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$TARGET" true 2>/dev/null; then
  HOST="${TARGET#*@}"
  cat >&2 <<EOF

Cannot log in to $TARGET without a password. To fix (one time):

  1. Make sure the Pi is booted and reachable:
         ping $HOST
     ".local" names need a minute after boot; if the name never resolves,
     find the Pi's IP address (your router's device list) and re-run:
         tools/pi_deploy.sh pisortiq@<ip-address>

  2. Install your SSH key on the Pi (asks for the Pi password you set
     in the Raspberry Pi Imager):
         ssh-copy-id $TARGET
     (If the Imager was given an SSH key instead of a password, log in
     from the machine that key belongs to, or add this machine's key.)

  3. Re-run this script.
EOF
  exit 1
fi

if ! ssh "$TARGET" 'sudo -n true' 2>/dev/null; then
  echo ">> sudo on the Pi asks for a password (user not set up by the Imager)."
  echo "   Granting passwordless sudo so installs and updates can run"
  echo "   unattended — you'll be asked for the Pi user's password ONCE."
  echo "   (Undo later with: sudo rm /etc/sudoers.d/010-sortiq-nopasswd)"
  ssh -t "$TARGET" 'echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010-sortiq-nopasswd >/dev/null && sudo chmod 440 /etc/sudoers.d/010-sortiq-nopasswd'
  ssh "$TARGET" 'sudo -n true' 2>/dev/null || {
    echo "!! passwordless sudo still not working — aborting" >&2; exit 1; }
fi

echo ">> migrating any old ShellSorter install"
ssh "$TARGET" 'bash -s' <<'MIGRATE'
set -euo pipefail
if [ -d ~/ShellSorter ] && [ ! -d ~/SortIQ ]; then
  sudo systemctl stop shellsorter 2>/dev/null || true
  mv ~/ShellSorter ~/SortIQ
  rm -rf ~/SortIQ/.venv          # venv scripts hard-code the old path
  echo "   migrated ~/ShellSorter -> ~/SortIQ"
fi
if [ -f /etc/systemd/system/shellsorter.service ]; then
  # (file test, not `systemctl | grep -q` — grep's early exit SIGPIPEs
  # systemctl under pipefail and this branch silently never runs)
  sudo systemctl disable --now shellsorter 2>/dev/null || true
  sudo rm -f /etc/systemd/system/shellsorter.service
  sudo systemctl daemon-reload
  echo "   retired shellsorter.service"
fi
MIGRATE

echo ">> syncing code to $TARGET:~/SortIQ"
rsync -az --delete \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude runs --exclude .claude --exclude 'data/' \
  --exclude config.json --exclude 'calibers/' \
  --exclude 'gpu_jobs/' --exclude '*.log' \
  "$SRC/" "$TARGET:SortIQ/"

# seed config + model templates ONLY if the Pi doesn't have them yet.
# Templates are model.json only — never this machine's local images or
# trained models, so a fresh Pi truly starts blank.
rsync -az --ignore-existing "$SRC/config.json" "$TARGET:SortIQ/config.json"
rsync -az --ignore-existing \
  --exclude 'data/' --exclude 'models/' --exclude 'runs/' \
  "$SRC/calibers/" "$TARGET:SortIQ/calibers/"

echo ">> installing on the Pi (first run takes a few minutes)"
ssh "$TARGET" 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/SortIQ
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv libglib2.0-0 v4l-utils >/dev/null
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q opencv-python-headless flask pyserial
# TFLite interpreter: tflite-runtime has wheels up to Python 3.11 (needs
# numpy<2); newer Pi OS (trixie, Python 3.12+) uses its successor
if ! .venv/bin/pip install -q ai-edge-litert 2>/dev/null; then
  .venv/bin/pip install -q "numpy<2" tflite-runtime
fi

# the dev-box config ships with a Windows COM port; point it at the Uno
.venv/bin/python - <<'PY'
import json, pathlib
p = pathlib.Path("config.json")
c = json.loads(p.read_text())
if not str(c.get("serial", {}).get("port", "")).startswith("/dev/"):
    c.setdefault("serial", {})["port"] = "/dev/ttyUSB0"
    p.write_text(json.dumps(c, indent=2))
    print("   config: serial.port -> /dev/ttyUSB0")
PY

# run at boot, restart on crash
sudo tee /etc/systemd/system/sortiq.service >/dev/null <<UNIT
[Unit]
Description=SortIQ web UI
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/SortIQ
ExecStart=$HOME/SortIQ/.venv/bin/python webui/server.py --port 5000
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now sortiq >/dev/null 2>&1
sudo systemctl restart sortiq
sleep 3
systemctl is-active sortiq | sed 's/^/   service: /'
echo "   board:  $(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | tr '\n' ' ' || true)$( [ -e /dev/ttyUSB0 ] || [ -e /dev/ttyACM0 ] || echo 'not detected (plug in the CS7.2 USB)')"
echo "   camera: $( [ -e /dev/video0 ] && echo /dev/video0 || echo 'not detected (plug in the camera USB)')"
REMOTE

echo ">> done — open http://${TARGET#*@}:5000"
