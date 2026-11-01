#!/usr/bin/env bash
# First boot, before there's any network: finish setting up the vhs user,
# copy the repo off the boot partition, then start the panel's Wi-Fi picker
# (vhs-netsetup) and the one-time installer (vhs-provision). Run once by
# cloud-init - see user-data. Everything that needs the network is in
# provision.sh.
set -euo pipefail

VHS_USER=vhs
BOOT_VHS=/boot/firmware/vhs
SRC=$BOOT_VHS/VirtualHardSynth
REPO=/home/$VHS_USER/VirtualHardSynth

# userconf.txt has the OS rename its placeholder first user to vhs; that
# runs alongside this, so wait for it.
for _ in $(seq 1 120); do
    id "$VHS_USER" >/dev/null 2>&1 && break
    sleep 1
done
id "$VHS_USER"
usermod -aG adm,dialout,audio,netdev,video,plugdev,input,gpio,spi,i2c,render,sudo "$VHS_USER"
install -d -m 700 -o "$VHS_USER" -g "$VHS_USER" /home/$VHS_USER/.ssh
install -m 600 -o "$VHS_USER" -g "$VHS_USER" "$BOOT_VHS"/authorized_keys \
    /home/$VHS_USER/.ssh/authorized_keys
echo "$VHS_USER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/010-vhs
chmod 440 /etc/sudoers.d/010-vhs

if [ ! -d "$REPO" ]; then
    cp -r "$SRC" "$REPO"
    # FAT has no file modes; give everything sane ones. git ignores modes in
    # this checkout (core.fileMode=false) and every script is run through
    # its interpreter, so nothing needs +x.
    find "$REPO" -type d -exec chmod 755 {} +
    find "$REPO" -type f -exec chmod 644 {} +
    chown -R "$VHS_USER:$VHS_USER" "$REPO"
fi

# No RTC: the clock is wrong until NTP syncs, and apt would refuse the
# archive's signatures as "not yet valid".
echo 'Acquire::Check-Date "false";' > /etc/apt/apt.conf.d/99vhs-no-check-date

# The image defaults to a UK layout, which moves " | @ ~ around.
raspi-config nonint do_configure_keyboard us || true

# Wi-Fi stays soft-blocked until a regulatory country is set.
raspi-config nonint do_wifi_country US || true
rfkill unblock wifi || true

# Bluetooth gave up its UART to the panel (dtoverlay=disable-bt).
systemctl disable --now hciuart.service 2>/dev/null || true
systemctl enable --now ssh.service

install -m 644 "$REPO"/setup/vhs-netsetup.service "$REPO"/setup/vhs-provision.service \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable vhs-netsetup.service vhs-provision.service
systemctl start --no-block vhs-netsetup.service vhs-provision.service
