# VirtualHardSynth

A software synthesizer that behaves like hardware.

Plug in a MIDI keyboard, hit power, play. No screen to log into, no laptop, no
DAW, no boot sequence to babysit. A Raspberry Pi with an audio HAT does the
sound, and a physical touchscreen panel does the front panel, the way a rack
synth would.

## Components

| Directory | What it is | State |
|-----------|-----------|-------|
| [`midi-patch-display/`](midi-patch-display/) | ESP32 CYD touchscreen front panel plus the host-side bridge daemon | Firmware working, bridge untested against a live synth |
| [`synth/`](synth/) | The Pi MIDI host and sound engine | Being rebuilt from scratch, see [`synth/IDEA.md`](synth/IDEA.md) |

```
MIDI keyboard --USB/DIN--> Pi --> sound engine --> audio HAT --> speakers
                            |
                        bridge
                            |
                     USB serial (JSON)
                            |
                     CYD touch panel
```

## Why the two halves are separate

The panel knows nothing about the synth. It renders whatever `state` frames it
receives and emits abstract commands like `patch_next`. All meaning lives in the
host, which means:

- The synth engine can be rebuilt, replaced or swapped without reflashing the
  panel.
- Any host that speaks the protocol can drive the panel. It has already been
  driven from Windows for testing.
- The transport is swappable too. USB serial today, WiFi later, no change to
  the UI or the state format.

Protocol is documented in [`midi-patch-display/README.md`](midi-patch-display/README.md).

## Hardware

- Raspberry Pi 4 with a battery module and an audio HAT
- ESP32-2432S028R "CYD", 2.8" 320x240 ILI9341 with XPT2046 touch
- One USB cable between them, carrying power and data
- A MIDI keyboard

## Status

The panel half works: firmware builds, flashes, renders, and its touch buttons
emit the right commands over a verified serial link. The synth half is a
rebuild in progress. See each component's `IDEA.md` for detail.
