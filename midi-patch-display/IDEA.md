# midi-patch-display

**Status:** Active
**Created:** 2026-08-17
**Last Updated:** 2026-08-17

## Essence

A CYD (ESP32-2432S028R, 2.8" 320x240 touch display) acting as the front panel
for an otherwise headless FluidSynth MIDI rig running on a battery-powered
Raspberry Pi 4 with an audio HAT. It shows the current bank, program and patch
name, and its touchscreen cycles patches and banks.

The Pi and the CYD are joined by a single USB cable that carries power and data
both. The ESP32 radio is never started.

## Problem It Solves

The Pi rig sounds good and is genuinely portable: plug in a MIDI keyboard, power
on, play. Its one flaw is that it is blind. There is no way to see which patch
is loaded or to change it without SSH-ing in, which defeats the point of a
self-contained instrument.

## Design Requirements

**Must have:**
- Show current bank, program number and patch name
- Cycle patch forward/back and bank forward/back from the touchscreen
- Stay in sync when the patch is changed from the keyboard instead of the screen
- Work with no network present (busking, rehearsal rooms, anywhere)
- Survive the bridge or the Pi restarting without needing the CYD reflashed

**Nice to have:**
- Battery percentage from the Pi power module
- MIDI activity indicator, to answer "is the keyboard even connected"
- Panic / all-notes-off
- Idle backlight dimming, since the screen shares the Pi battery

## Viability Notes

**Why USB serial and not WiFi.** For a portable rig the cable wins outright: one
connector for power and data, nothing to configure, no association delay at
power-on, works with no infrastructure, and no radio drain on either end. A WiFi
variant would need the Pi running hostapd. The wire protocol is transport
agnostic, so switching later costs nothing on the CYD side.

**Why this project fits the CYD when the audio ideas did not.** This board is an
ESP32-D0WD-V3 with ~212KB RAM and no PSRAM. That blocks essentially every
"decode an audio stream" path, including cspot for Spotify Connect and the
ESP32-audioI2S library, both of which require PSRAM. This project moves no
audio, so none of that applies. Measured footprint after the first build was
7.1% RAM and 10.7% of a 3MB app partition.

**Stock partition table is too small.** The CYD ships a 1MB app partition.
`board_build.partitions = huge_app.csv` gives 3MB with no OTA slot, which is
correct for a device that flashes over USB.

**Touch calibration varies per panel.** Raw XPT2046 corner values and axis
orientation are exposed as constants and flags in `src/config.h`, with a serial
`cal` command that prints raw coordinates so a new panel can be dialled in
without code changes.

**Patch names come from the .sf2 directly.** `pi/sf2.py` parses the RIFF `phdr`
chunk. This means correct names even when the FluidSynth command socket is
disabled, and it gives an ordered preset list to navigate.

## Shared Functionality

- Uses: nothing from `shared/` yet.
- Could contribute: `pi/sf2.py` is a self-contained SoundFont preset reader with
  no dependencies. If another AI-Dear ever touches SoundFonts it should move to
  `shared/`. Related in spirit to [ai-daw](../ai-daw/IDEA.md), though that is
  paused and shares no code.

## Open Questions

- Which battery module is on the Pi? The bridge ships a PiSugar reader and a
  `none` default; anything else needs a small reader function adding.
- Is FluidSynth started with `-s` (command socket)? If not the bridge silently
  falls back to driving it over MIDI, which works but cannot query soundfonts.
- Worth adding a scrollable preset list screen, or are four buttons enough in
  practice?
- Should favourite patches be pinnable, so a set list can be walked in order?

## Observability

This is embedded firmware plus a foreground daemon on a battery-powered,
frequently-offline device. It runs no containers and serves no HTTP, so the
AON service obligations (health endpoints, Grafana dashboards, burn-rate alerts,
`service-registry.yaml` entry) do not apply. It is deliberately out of the
observability estate rather than an omission from it.

The equivalent guarantees are handled in-band, on the screen itself, which is
the only place the musician can see them:

### Critical User Journeys

| ID | Actor | Action | Expected outcome | Latency budget | Signal |
|----|-------|--------|-----------------|----------------|--------|
| CUJ-1 | Musician | Taps NEXT/PREV | Patch changes and the new name is on screen | 150ms | screen updates |
| CUJ-2 | Musician | Changes patch from the keyboard | Screen follows within one heartbeat | 2s | screen updates |
| CUJ-3 | Musician | Glances at the panel | Knows patch, keyboard status and battery | immediate | status bar |
| CUJ-4 | Musician | Taps centre during a stuck note | All notes off | 150ms | audio stops |

### In-band health signals

| Signal | Meaning | Failure display |
|--------|---------|-----------------|
| Link watchdog | Bridge heartbeat every 2s; 6s silence means the link is down | Status bar reads `LINK DOWN` in red |
| MIDI presence | Bridge reports whether the keyboard port is open | Activity dot turns red |
| Battery | Polled every 15s from the Pi power module | Icon turns amber below 35%, red below 15% |

### Logging

The bridge logs structured lines to stdout at INFO, captured by journald under
the `patchbridge` systemd unit. `journalctl -u patchbridge` is the debug entry
point. The CYD emits diagnostics as `#`-prefixed serial lines, which the bridge
ignores by design so a serial monitor stays usable while it runs.
