# setup

Builds a VHS-1 SD card from scratch, so a dead card costs ten minutes
instead of a rebuild.

1. Write **Raspberry Pi OS Lite (64-bit)** to the card with Raspberry Pi
   Imager, no OS customisation. (`rpi-imager --cli <image> <drive>` works too.)
2. Commit anything you want on the Pi, then run, with the card's boot
   partition mounted:

   ```powershell
   pwsh setup/prepare-card.ps1 -Boot G:\
   ```

3. Put the card in the Pi and power on. After a minute or two the panel
   opens its Wi-Fi picker; choose a network and type the password on the
   touchscreen. (Or plug in Ethernet and skip that.)
4. The panel shows install progress, then the synth comes up on its own.
   The web UI is at `http://vhs.local:8080`; `ssh vhs@vhs.local` with the
   key from step 2.

| File | Runs | Does |
|------|------|------|
| `prepare-card.ps1` | on your PC | config.txt (WM8960, UART), cmdline.txt, userconf.txt + SSH key, cloud-init user-data, copies the repo |
| `user-data` | cloud-init, first boot | hostname `vhs`, US keyboard, calls firstboot.sh |
| `firstboot.sh` | first boot, offline | SSH key + sudo for `vhs`, repo into `/home/vhs`, Wi-Fi country, starts the two units below |
| `vhs-netsetup.service` | until provisioned | `netsetup.py`: drives the panel, Wi-Fi picker (stdlib only) |
| `vhs-provision.service` | once, when online | `provision.sh`: apt packages, WM8960 mixer, installs + starts the synth services |
| `50-vhs-networkmanager.rules` | always | lets the bridge (user `vhs`) scan/join Wi-Fi for the panel later |

To change networks later, tap the soundfont name in the panel's status bar.

Re-run the install by hand with `sudo bash ~/VirtualHardSynth/setup/provision.sh`.
