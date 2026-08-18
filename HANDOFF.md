# Handoff

Session state as of 2026-08-18. Written so work can continue from another
machine without re-deriving anything.

## Where this stands in one paragraph

The panel half is built, flashed and verified. The ESP32 CYD is running firmware
that renders patch state and emits commands over USB serial, and the link has
been tested hard. The synth half has a design and no code. The next real step is
plugging in the Pi and running `bridge.py` against a live FluidSynth for the
first time, which has never been done.

## Bring with you

- The **CYD** itself. Firmware is already flashed, so no PlatformIO needed
  unless you want to change it.
- A **USB data cable**. The 1ft micro-USB cable is **charge-only** and will not
  work. This was confirmed the hard way: the screen lights up and looks perfectly
  healthy while no serial port ever appears.
- The Pi, keyboard, audio HAT, battery.

## Hardware facts, all verified

### CYD panel

| Property | Value |
|----------|-------|
| Chip | ESP32-D0WD-V3, revision v3.1 |
| Features | WiFi, BT, Dual Core, 240MHz |
| Flash | 4MB |
| PSRAM | **None.** This is why no audio path was viable on it |
| MAC | `d4:e9:f4:6a:fd:9c` |
| Display | ILI9341 320x240, MOSI 13, SCLK 14, CS 15, DC 2, MISO 12, BL 21 |
| Touch | XPT2046, CLK 25, MOSI 32, MISO 39, CS 33, IRQ 36 |
| USB serial | CH340, `VID_1A86 PID_7523` |

Both the micro-USB and USB-C connectors are confirmed data-capable. Either
works; the plan is micro-USB with a short cable.

On Windows the board appears as a COM port whose number depends on the physical
USB port used, because the CH340 has no unique serial number. It was COM6 on one
port and COM7 on another. On Linux, install
[`pi/99-midi-panel.rules`](midi-patch-display/pi/99-midi-panel.rules) to get a
stable `/dev/midi-panel`.

If a CH340 shows Code 28 on a fresh Windows machine, the driver is on Windows
Update as `wch.cn - Ports`. Do not bother with wch-ic.com, whose download
endpoint is JS-gated.

### Pi rig, as it exists today

Headless Pi 4, battery module, audio HAT, FluidSynth with SoundFonts. Plug in a
keyboard, power on, play. Sounds good. Not reproducible, no visibility.

## What is verified

- Firmware **builds**: 7.1% RAM, 10.7% of a 3MB app partition. The stock 1MB
  CYD partition is too small, hence `huge_app.csv`.
- Firmware **flashed and running** on the board.
- **Serial link**, four tests all passing: boot handshake, link held after state
  frames, a 200-frame burst in 1.82s (110fps, no corruption), and watchdog
  recovery after silence. That is roughly 55x the 2Hz the bridge actually uses.
- **SF2 parser** against real SoundFonts found in an FL Studio install.
- **Patch and bank navigation**, 11 assertions covering wraparound, sparse bank
  jumps (0 to 8 to 128 to wrap) and external patch matching.
- Touch buttons **emit the correct commands**, confirmed by capturing serial
  traffic while the board was handled.

## What is NOT verified

1. **How the screen actually looks.** Never seen by a human. Layout, colours and
   legibility are unconfirmed.
2. **Touch calibration.** Raw XPT2046 ranges vary per panel. Constants are in
   [`src/config.h`](midi-patch-display/src/config.h) with a serial `cal` mode
   and `TOUCH_SWAP_XY` / `TOUCH_INVERT_X` / `TOUCH_INVERT_Y` flags. Use
   [`pi/touchtest.py`](midi-patch-display/pi/touchtest.py) if anything lands
   wrong.
3. **`bridge.py` against a live FluidSynth.** Syntax-checked, logic unit-tested,
   never run against a real synth. **This is the next step.**

### One trap that already cost time

The panel **only updates when a host is feeding it state frames**. It never
updates its own display optimistically, because the host owns which patches
exist. Press buttons with no bridge running and nothing visibly happens, which
looks exactly like broken firmware. The red `LINK DOWN` in the status bar is the
tell.

## Resuming at the Pi

```bash
git clone git@github.com:sethlyon/VirtualHardSynth.git
cd VirtualHardSynth

# stable device name for the panel
sudo cp midi-patch-display/pi/99-midi-panel.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

sudo apt install python3-rtmidi
pip install -r midi-patch-display/pi/requirements.txt

# first run - verbose matters, see below
python3 midi-patch-display/pi/bridge.py --port /dev/midi-panel --midi-in "" -v
```

`-v` on the first run tells you immediately which control path it took:

- **FluidSynth command socket found** - good, it can enumerate soundfonts and
  auto-detect the `.sf2` path.
- **Fell back to MIDI control** - FluidSynth was not started with `-s`. Still
  works, but it cannot query fonts, so pass `--sf2 /path/to/your.sf2`.

To enable the command socket, FluidSynth needs `-s`:

```bash
fluidsynth -s -a alsa -o audio.alsa.device=hw:0 /usr/share/sounds/sf2/FluidR3_GM.sf2
```

Find the keyboard's MIDI port name for `--midi-in` with:

```bash
python3 -c "import mido; print(mido.get_input_names())"
```

That flag is what makes the screen follow patch changes made from the keyboard
rather than the panel.

If the panel does not appear at all: check the cable first. See above.

## Decisions already taken

Full reasoning in [`synth/ENGINES.md`](synth/ENGINES.md).

- **One patch at a time.** No splits or layers. This is the decision that
  removes the need for a plugin host: if only one engine is ever audible, a
  supervisor routing MIDI to resident engines beats Carla or similar.
- **Engine architecture:** own supervisor, resident JACK engines, phased from
  today's FluidSynth. Add sfizz next for SFZ sample libraries, then character
  engines like setBfree and Dexed.
- **Engines stay resident** rather than starting per patch, because a 1-3s gap
  when changing patch is exactly what stops it feeling like an instrument.
- **Zynthian assessed and set aside.** Largest sonic head start by far, but its
  own UI competes with the panel already built. Revisit if curating engines
  yourself becomes the annoying part.
- **Reproducibility:** Ansible against stock Raspberry Pi OS Lite. Not
  containers; audio, realtime scheduling and USB MIDI all fight containerisation
  on a Pi.
- **Power-cut safety:** read-only root with overlayfs via `raspi-config`,
  patches on a small writable partition. This is a battery instrument that gets
  switched off, not shut down.
- **Panel transport:** USB serial, one cable for power and data. The protocol is
  transport-agnostic, so WiFi later is contained.

## Questions to answer once the Pi is in front of you

These gate the synth rebuild and are all one command away:

1. **Which audio HAT?** Sets achievable buffer size and whether `PREEMPT_RT` is
   worth chasing. `aplay -l` and `cat /proc/asound/cards`.
2. **How much RAM is the Pi 4?** Decides how many engines stay resident.
   `free -h`.
3. **Which battery module?** Needed for the panel battery readout and for
   whether low-battery shutdown is possible. The bridge ships a PiSugar reader
   and a `none` default.
4. **Is FluidSynth started with `-s`?** Check how it is launched today, likely
   `systemctl cat` on whatever unit runs it, or `/etc/rc.local`.
5. **Boot time today.** `systemd-analyze` and `systemd-analyze blame` before
   changing anything, so improvement is measurable.

## Suggested order of work

1. Run `bridge.py` against the live FluidSynth. Confirm the screen shows real
   patch names and the buttons change patches. This validates everything built
   so far.
2. Look at the screen and fix layout or touch calibration if needed.
3. Answer the five questions above and record them in
   [`synth/IDEA.md`](synth/IDEA.md).
4. Capture the current rig's configuration before rebuilding anything, so the
   working state is recoverable.
5. Start the Ansible playbook, with FluidSynth as engine one.

## Repo layout

```
VirtualHardSynth/
  IDEA.md                    project essence, requirements, architecture notes
  README.md                  overview and component table
  HANDOFF.md                 this file
  midi-patch-display/        the panel: ESP32 firmware + host bridge
    IDEA.md                  design notes, viability, observability
    README.md                protocol, flashing, calibration, cabling
    platformio.ini           board config, 3MB partition
    src/                     firmware: config.h, main.cpp, ui.cpp/h
    pi/                      bridge.py, sf2.py, demo.py, touchtest.py,
                             systemd unit, udev rule
  synth/
    IDEA.md                  requirements, decisions, open questions
    ENGINES.md               engine comparison and the reasoning behind it
```

Testing the panel with no Pi involved:
`python midi-patch-display/pi/demo.py --port <PORT>`
