# midi-patch-display

Front panel for a headless FluidSynth rig. A CYD (ESP32-2432S028R) hangs off one
USB port of the Raspberry Pi and shows what is playing.

```
MIDI keyboard --USB/DIN--> Pi 4 --> FluidSynth --> audio HAT --> speakers
                            |
                       bridge.py
                            |
                     USB serial (JSON lines)
                            |
                          CYD
```

```
+----------------------------------------------+
| FluidR3_GM                     * MIDI  [87%] |
+----------------------------------------------+
|                                              |
|                Rhodes MkI                    |
|                                              |
|          BANK 000      PROG 004              |
|                                              |
|      tap here for panic / all notes off      |
+-----------+-----------+----------+-----------+
|   BANK-   |   PREV    |   NEXT   |   BANK+   |
+-----------+-----------+----------+-----------+
```

## Layout

| Path | What it is |
|------|-----------|
| `platformio.ini` | Board, TFT_eSPI pin config, 3MB partition scheme |
| `src/config.h` | Pin map, touch calibration, backlight and link timeouts |
| `src/main.cpp` | Serial protocol, touch handling, link watchdog |
| `src/ui.cpp` | Screen drawing with dirty-region tracking |
| `pi/bridge.py` | FluidSynth control + serial link daemon |
| `pi/sf2.py` | SoundFont preset reader (no dependencies) |
| `pi/demo.py` | Fake driver for testing the CYD with no Pi |
| `pi/patchbridge.service` | systemd unit |

## Flashing the CYD

```bash
pio run --target upload --upload-port COM6      # or /dev/ttyUSB0
```

The build uses `huge_app.csv`, because the 1MB partition the CYD ships with is
too small once TFT_eSPI is linked in.

## Testing without the Pi

`demo.py` speaks the same protocol with a hardcoded preset list. Run it from any
machine with the CYD plugged in:

```bash
pip install pyserial
python pi/demo.py --port COM6
```

Touch the buttons; the patch name should change and each command prints in the
terminal. This is the fastest way to confirm the display, touch mapping and
serial link before FluidSynth is in the picture.

## Running on the Pi

```bash
sudo apt install python3-rtmidi        # or let pip build it
pip install -r pi/requirements.txt

python3 pi/bridge.py --port /dev/ttyUSB0 --midi-in "" --battery none -v
```

Useful flags:

| Flag | Purpose |
|------|---------|
| `--sf2 PATH` | Soundfont path. Auto-detected when the command socket is up |
| `--midi-in SUBSTR` | Substring of the keyboard MIDI port, for patch-change sync |
| `--channel N` | MIDI channel to drive, default 0 |
| `--battery pisugar` | Enable battery reporting |
| `--fluid-port N` | FluidSynth command socket, default 9800 |

### Control paths

The bridge prefers FluidSynth's command socket, which needs FluidSynth started
with `-s`:

```bash
fluidsynth -s -a alsa -o audio.alsa.device=hw:0 /usr/share/sounds/sf2/FluidR3_GM.sf2
```

Without it, the bridge falls back to sending Bank Select + Program Change to
FluidSynth's MIDI input. That works against any running instance with no
reconfiguration, but it cannot query which soundfonts are loaded, so pass
`--sf2` explicitly in that case.

### Stable device name

The CH340 has no unique serial number, so Linux assigns `/dev/ttyUSB*` in plug
order and Windows assigns a COM number per physical USB port. Install the udev
rule so the bridge always finds the panel:

```bash
sudo cp pi/99-midi-panel.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

That gives you `/dev/midi-panel`, which the systemd unit already uses.

### Install as a service

```bash
sudo cp pi/patchbridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now patchbridge
journalctl -u patchbridge -f
```

Edit `WorkingDirectory`, `ExecStart` and `User` in the unit first if your paths
differ.

## Cabling

The 2-USB revision of this board exposes both micro-USB and USB-C, wired to the
same CH340. **Both are verified working for data on this unit**, so either is
fine and the Pi sees the same device either way.

This rig uses micro-USB with a 1ft cable. Micro-USB connectors on these boards
are surface-mount and can tear off the PCB, but a cable this short carries
little weight and has nothing to snag on, which is where that failure usually
comes from. Worth anchoring the cable to the board or enclosure with a zip tie
or a dab of hot glue so any yank lands on the anchor rather than the solder
joints.

Two things that waste time:

- **Use a data cable.** Plenty of short USB-A to USB-C cables are charge-only.
  The symptom is a screen that lights up and looks perfectly healthy while no
  serial port ever appears. Suspect the cable first.
- **One port at a time.** Both connectors feed the same 5V rail.

USB-A on the host side is the safer pairing anyway, since the host supplies 5V
unconditionally and no USB-C CC resistor negotiation is involved.

## Protocol

Newline-delimited JSON, both directions. Lines beginning with `#` are CYD
diagnostics and the bridge ignores them.

Pi to CYD:

```json
{"t":"state","bank":0,"prog":4,"name":"Rhodes MkI","sf":"FluidR3_GM","batt":78,"midi":true}
{"t":"act"}
{"t":"toast","msg":"PANIC"}
```

CYD to Pi:

```json
{"t":"hello","fw":"1.0.0"}
{"t":"cmd","action":"patch_next"}
```

Actions: `patch_next`, `patch_prev`, `bank_next`, `bank_prev`, `panic`.

The CYD repeats `hello` every 2s until it receives a `state` frame, so the
bridge can restart at any time and the screen resyncs on its own. If no frame
arrives for 6s the status bar shows `LINK DOWN`.

## Touch calibration

Raw XPT2046 ranges vary between panels. If touches land in the wrong place:

1. Open a serial monitor (`pio device monitor -p COM6`) and type `cal`.
2. Touch each corner. Raw and mapped coordinates print for every touch.
3. Put your corner values into `TS_RAW_*` in `src/config.h`.
4. If the axes are swapped or mirrored, set `TOUCH_SWAP_XY`, `TOUCH_INVERT_X` or
   `TOUCH_INVERT_Y` rather than editing code.
5. Type `cal` again to exit, then reflash.

If the whole screen is upside down, change `SCREEN_ROTATION` from 1 to 3.

## Power

The CYD draws roughly 100-150mA at full backlight from the Pi USB port, which
means from the same battery. The backlight drops to a dim level after two
minutes without a touch and wakes on touch or on any patch change, including one
made from the keyboard. Tune `BL_IDLE_MS`, `BL_LEVEL_DIM` and `BL_LEVEL_FULL` in
`src/config.h`; set `BL_IDLE_MS` to 0 to disable dimming.
