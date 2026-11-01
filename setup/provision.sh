#!/usr/bin/env bash
# One-time software install for the VHS-1. vhs-provision.service runs this
# on first boot and it waits for a network (Wi-Fi from the panel, or
# Ethernet). Safe to re-run by hand: sudo bash setup/provision.sh
set -euo pipefail

VHS_USER=vhs
REPO=/home/$VHS_USER/VirtualHardSynth
SETUP=$REPO/setup
CARD=wm8960soundcard

# netsetup.py shows this line on the panel.
status() { mkdir -p /run/vhs; echo "$*" > /run/vhs/status; echo "== $*"; }

status "waiting for network"
until getent ahosts deb.debian.org >/dev/null 2>&1; do sleep 5; done

status "installing software"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y fluidsynth fluid-soundfont-gm alsa-utils git i2c-tools \
    python3-serial python3-mido python3-rtmidi python3-flask
# INA219 battery gauge. Not packaged everywhere; fall back to pip.
apt-get install -y python3-smbus2 \
    || pip3 install --break-system-packages smbus2

status "configuring audio"
# The WM8960 output mixer switches default to off: no sound at all, and no
# error either, until they're on.
amixer -c "$CARD" sset 'Left Output Mixer PCM' on
amixer -c "$CARD" sset 'Right Output Mixer PCM' on
amixer -c "$CARD" sset Headphone 80%
amixer -c "$CARD" sset Speaker 80%
alsactl store

status "installing services"
install -m 644 "$REPO"/synth/fluidsynth.service \
    "$REPO"/midi-patch-display/pi/patchbridge.service \
    "$REPO"/synth/patchweb.service \
    /etc/systemd/system/
# Lets the bridge (user vhs) scan and join Wi-Fi for the panel.
install -m 644 "$SETUP"/50-vhs-networkmanager.rules /etc/polkit-1/rules.d/
sudo -u "$VHS_USER" mkdir -p "$REPO"/synth/soundfonts "$REPO"/synth/configs
systemctl daemon-reload
systemctl enable fluidsynth.service patchbridge.service patchweb.service

mkdir -p /var/lib/vhs
touch /var/lib/vhs/provisioned

status "starting synth"
systemctl disable vhs-netsetup.service vhs-provision.service
# patchbridge Conflicts= with netsetup, so this hands the panel over.
systemctl start fluidsynth.service patchbridge.service patchweb.service
echo "== done: http://$(hostname).local:8080"
