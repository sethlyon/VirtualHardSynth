# synth - the Pi MIDI host and sound engine

**Status:** Exploring
**Created:** 2026-08-18
**Last Updated:** 2026-08-18

## Essence

The half of [VirtualHardSynth](../IDEA.md) that makes noise. A Raspberry Pi 4
with an audio HAT that boots straight into being an instrument: MIDI in from a
USB or DIN keyboard, audio out to the HAT, controlled from the
[panel](../midi-patch-display/IDEA.md).

Being rebuilt from scratch. The existing rig works and sounds good, but it is a
hand-tended pet rather than something reproducible.

## What Exists Today

A headless Pi 4 running FluidSynth with SoundFonts, audio HAT to speakers,
battery module. Plug in a keyboard, power on, play. Known good sonically.

Its limits:
- No visibility into what is loaded (the reason the panel exists)
- Hand-configured, not reproducible from a clean card
- Single engine, SoundFonts only
- Unknown boot-to-playable time
- Unknown behaviour on power loss

## Design Requirements

**Must have:**
- Boot to playable with no interaction, no login, no network
- Deterministic MIDI device binding, so device order never matters
- Survive an abrupt power cut without corruption, since it is battery powered
  and gets switched off rather than shut down
- Rebuildable from a clean SD card by running something, not by remembering
- A control interface the panel bridge can drive

**Nice to have:**
- Multiple engines, not SoundFonts alone
- Splits and layers
- Effects, reverb at minimum
- Set lists, patches walked in performance order
- Recording
- Low enough latency to feel like hardware

## Open Questions

These decide the whole design and are not yet answered:

1. **Why rebuild?** Which failure hurts most: boot time, reliability,
   reproducibility, patch management, or sonic range?
2. **Engine direction.** Stay with FluidSynth for its simplicity, or move to a
   host that can run several engines and plugins?
3. **Multi-timbral?** One patch at a time, or splits and layers, which changes
   the data model and the panel UI substantially.
4. **Reproducibility mechanism.** A flashable image, Ansible against a stock
   Raspberry Pi OS, or containers?
5. **Latency target.** What buffer size does the HAT manage cleanly, and does
   the kernel need `PREEMPT_RT`?
6. **Is the Pi 4 staying?** A rebuild is the moment to reconsider compute, given
   the whole thing runs on a battery.

## Viability Notes

Nothing built yet. The current rig proves the audio path and the battery
approach work, which de-risks the sonic side entirely. The rebuild is about
operations and interface, not about whether a Pi can make sound.

The panel already exists and its protocol is documented, so the engine has a
known control surface to design against rather than a moving target.

## Interfaces

Whatever this becomes must expose a way for the panel bridge to:

- Enumerate available patches with names, ordered
- Select a patch
- Report the current patch when it changes from anywhere, including the keyboard
- Trigger all-notes-off
- Report health, so the panel can show when the engine is down

See [`../midi-patch-display/README.md`](../midi-patch-display/README.md) for the
current protocol, which is a reasonable starting shape.
