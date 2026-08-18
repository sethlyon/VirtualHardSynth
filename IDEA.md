# VirtualHardSynth

**Status:** Active
**Created:** 2026-08-18
**Last Updated:** 2026-08-18
**Repo:** https://github.com/sethlyon/VirtualHardSynth

## Essence

A software synth that behaves like a hardware one. A Raspberry Pi 4 with an
audio HAT and a battery module is the sound engine and MIDI host; an ESP32
touchscreen panel is the front panel. Plug in a keyboard, power on, play.

The point is the *feel* of hardware: instant, self-contained, physical controls,
no general-purpose computer anywhere in the experience.

## Problem It Solves

Software instruments sound better than affordable hardware and cost nothing per
patch, but using them means a laptop, a DAW, an audio interface, a screen, and a
boot-and-load ritual. That kills them for practice, writing, and anywhere you
would rather just play.

The existing rig already proves the audio half: a headless Pi with FluidSynth
and an audio HAT sounds good and runs on battery. Its failures are all interface
and operational, not sonic. You cannot see what patch is loaded, cannot change
it without SSH, and the setup is not reproducible.

## Design Requirements

**Must have:**
- Power on to playable with no interaction and no network
- See the current patch at a glance
- Change patch from physical controls
- Run on battery
- Reproducible from a clean SD card, not a hand-tended pet

**Nice to have:**
- Splits and layers
- Effects (reverb at minimum)
- Set lists, so patches can be walked in performance order
- More engines than SoundFonts alone
- Recording

## Components

- **[midi-patch-display/](midi-patch-display/IDEA.md)** - the front panel. ESP32
  CYD firmware plus the host-side bridge. Working; see its IDEA for detail.
- **[synth/](synth/IDEA.md)** - the Pi MIDI host and sound engine. Complete
  rebuild in progress.

## Architecture Notes

**The panel is deliberately dumb.** It renders `state` frames and emits abstract
commands. No synth knowledge lives in the firmware. This is what lets the engine
be rebuilt or replaced without touching the ESP32, and what let the panel be
developed and tested from a Windows machine before the Pi was involved.

**Transport is swappable.** USB serial today because it is one cable for power
and data, needs no configuration, and works with no network. The wire format is
transport-agnostic, so WiFi is a contained change if the panel ever needs to be
detached.

**Where the bridge lives is an open question.** `midi-patch-display/pi/bridge.py`
currently drives FluidSynth directly. If the rebuilt engine exposes a proper
control surface, the bridge likely becomes a thin adapter, or folds into the
engine as its panel driver. To be decided once the engine design settles.

## Viability Notes

Proven so far:
- Panel firmware builds at 7.1% RAM and 10.7% of a 3MB partition
- Serial link verified: 200-frame burst at 110fps, no corruption, watchdog
  recovery works
- SoundFont preset parsing works against real `.sf2` files
- Patch and bank navigation logic unit-tested across sparse banks

Not yet proven:
- The bridge against a live FluidSynth
- Whether the panel *looks* right, which needs eyes on the hardware
- Everything about the rebuilt engine

## Open Questions

- What is wrong with the current rig, specifically? Boot time, reliability,
  patch management, sonic limits, or reproducibility?
- Does the engine stay FluidSynth, or move to a multi-engine host?
- Multi-timbral, or one patch at a time?
- Is the Pi 4 staying, or is this a chance to reconsider the compute?
- How is the build made reproducible: an image, Ansible, or a container?

## Shared Functionality

Lives outside the AI-Dears incubator as its own repo. `midi-patch-display/pi/sf2.py`
is a dependency-free SoundFont preset reader that would be reusable elsewhere.
