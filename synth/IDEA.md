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

**Decided 2026-08-18:**
- One patch active at a time. No splits or layers. This is what removes the
  need for a plugin host - see [ENGINES.md](ENGINES.md).

**Nice to have:**
- Multiple engines, not SoundFonts alone
- Effects, reverb at minimum
- Set lists, patches walked in performance order
- Recording
- Low enough latency to feel like hardware

## Decisions

Taken 2026-08-18. Full reasoning in [ENGINES.md](ENGINES.md).

- **Why rebuild:** all four drivers apply - reproducibility, boot and
  reliability, sonic range, and performance features.
- **One patch at a time.** No splits or layers.
- **Engine architecture:** roll our own supervisor with resident JACK engines,
  phased, starting from the FluidSynth we already have. Not a plugin host,
  because one-patch-at-a-time makes its central feature dead weight.
- **Reproducibility:** Ansible against stock Raspberry Pi OS Lite.
- **Power-cut safety:** read-only root with overlayfs, patches on a small
  writable partition.

## Open Questions

1. **What audio HAT is it?** Decides achievable buffer size and whether
   `PREEMPT_RT` is needed at all.
2. **Which battery module?** Needed for panel battery reporting and for knowing
   whether clean shutdown on low battery is possible.
3. **How much RAM is the Pi 4?** Decides how many engines can stay resident.
4. **Which engines after sfizz?** setBfree and Dexed are the obvious character
   additions, but that is taste, not architecture.
5. **Where does the bridge live?** Stays a separate daemon, or folds into the
   supervisor as its panel driver.
6. **Is the Pi 4 staying?** A rebuild is the moment to reconsider compute.

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
