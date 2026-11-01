# VirtualHardSynth

A software synthesizer that behaves like hardware.

Plug in a MIDI keyboard, hit power, play. No screen to log into, no laptop, no
DAW, no boot sequence to babysit. A Raspberry Pi with an audio HAT does the
sound, and a physical touchscreen panel does the front panel, the way a rack
synth would.

**Picking this up on a new machine? Start with [HANDOFF.md](HANDOFF.md)** - it
carries verified hardware facts, what is and is not proven, resume commands, and
the decisions already taken.

## Components

| Directory | What it is | State |
|-----------|-----------|-------|
| [`midi-patch-display/`](midi-patch-display/) | ESP32 CYD touchscreen front panel plus the host-side bridge daemon | Working end to end: patches, volume, battery, MIDI-present indicator all live |
| [`synth/`](synth/) | The Pi MIDI host, FluidSynth engine, and the patch-table web UI | Running as systemd services; web UI (`webui.py`) built and tested, not yet deployed to the Pi |

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

The rig works: a Pi 4 with a Waveshare WM8960 HAT runs FluidSynth and the
panel bridge as systemd services that survive a reboot, the CYD shows real
patch data and a working volume slider, and a MIDI keyboard plays through it
at real volume. Bank/program slots are no longer tied to whatever order one
`.sf2` file happens to list its presets in - a patch table
(`synth/patches.json`) maps each slot to any preset in any loaded soundfont,
editable through a browser UI that can also pull new soundfonts in from a
URL. See [HANDOFF.md](HANDOFF.md) for exactly what's verified versus still
rough (touch input lag, and the panel not reconnecting on its own after a
physical unplug, are the two known gaps).
