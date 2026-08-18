# Sound engine comparison

Written 2026-08-18 to decide what the rebuilt Pi synth runs.

Requirements it is judged against, from [IDEA.md](IDEA.md): reproducibility,
boot time and reliability, sonic range, and performance features. One patch
active at a time, no splits or layers.

## The constraint that shrinks the problem

**One patch at a time means you never need two engines making sound at once.**

That removes the usual reason to run a plugin host. A host like Carla exists to
mix, chain and route many plugins simultaneously. If exactly one instrument is
ever audible, all you need is several engines that can each be handed the
keyboard, and something that decides which one currently has it.

That is a much smaller, more reliable system than a plugin host, and it is the
main reason the recommendation below is not "install Carla".

There is a second-order choice inside it:

| | Engines resident, MIDI routed to the active one | Start and stop engines per patch |
|---|---|---|
| Patch switch | Instant | 1-3s while the engine loads |
| Idle RAM | All engines loaded, roughly 300-600MB | Only the active one |
| Idle CPU | Low, idle engines process silence | Lower |
| Feels like hardware | Yes | No |

**Resident wins.** A gap when changing patch is exactly the thing that stops
something feeling like an instrument, and a Pi 4 has RAM to spare for this.

## The options

### A. FluidSynth only, rebuilt properly

Keep what you have, but reproducible, fast-booting and power-cut-safe.

- **Sonic range:** SF2/SF3 only. Good GM sets exist, but sampled pianos and
  organs are noticeably weaker than dedicated engines. This is the driver it
  fails.
- **CPU:** negligible, low single-digit percent of one core.
- **Control:** command socket or MIDI, already working via `bridge.py`.
- **Risk:** lowest of everything here. It already works.

### B. Roll your own supervisor, resident JACK engines

JACK (or PipeWire's JACK API) for audio. FluidSynth, sfizz, setBfree, dexed and
friends run as standalone JACK clients, all resident. A supervisor routes
keyboard MIDI to whichever engine the current patch names, and owns the patch
list the panel navigates.

A patch becomes `(engine, preset, level)` rather than `(bank, program)`.

- **Sonic range:** grows one engine at a time. sfizz alone is a big jump, since
  SFZ opens up libraries like Salamander that SF2 cannot touch.
- **CPU:** engine dependent. FluidSynth and setBfree are cheap; ZynAddSubFX pads
  can be expensive. Budget per engine and measure on your HAT.
- **Control:** you design it, so the panel is a first-class citizen rather than
  something bolted on.
- **Risk:** moderate. More moving parts than A, far fewer than a plugin host.

### C. Zynthian

The [open synth platform](https://zynthian.org/) that is, more or less, this
project already built. Over 60 engines including FluidSynth, LinuxSampler,
sfizz, setBfree, aeolus, Dexed, ZynAddSubFX, Surge, amsynth and obxd, plus
snapshot management and MIDI learn.

- **Sonic range:** by far the largest, immediately.
- **Reproducibility:** they ship images, so this is largely solved.
- **The catch:** Zynthian has its own UI and its own idea of how the instrument
  is driven. Your CYD panel would be duplicating or fighting that, and pulling
  state back out to display means integrating against Zynthian internals or its
  OSC surface, on their release cycle rather than yours.
- **Verdict:** the right answer if sonic range dominates and you would rather
  not build. Wrong answer if the panel you have already built is meant to be
  the interface.

### D. Carla or another plugin host

Hosts LV2/VST2/VST3/SF2/SFZ, with patch switching via its rack mode.

- Buys you plugin format breadth, mainly VST, that standalone engines do not
  cover.
- Costs a large dependency, an extra abstraction layer between you and the
  sound, and control through OSC rather than something you designed.
- **With one patch at a time, its central feature goes unused.** Hard to justify.

## Against your four drivers

| Driver | A: FluidSynth | B: Own supervisor | C: Zynthian | D: Carla |
|--------|--------------|-------------------|-------------|----------|
| Reproducibility | Solved by the rebuild | Solved by the rebuild | Solved, their images | Solved by the rebuild |
| Boot / reliability | Best, tiny surface | Good | Heavier stack, slower | Heavier |
| Sonic range | **Fails** | Good, grows incrementally | Best | Good |
| Performance features | You build them | You build them | Built in | You build them |
| Panel fit | Perfect | Perfect | **Poor, competes** | Adapter needed |

## Recommendation

**Option B, phased, starting from A.**

FluidSynth stays as engine one, so you always have a working instrument. Add
sfizz next, because SFZ is the single largest jump in sample quality available
and it is one more JACK client, not an architectural change. Add character
engines like setBfree and Dexed after that, when you want them.

This keeps the panel central, keeps every step shippable, and never has a
"rewrite everything" moment. Option A is the first milestone of option B rather
than a competing path.

Reconsider Zynthian if, after living with two or three engines, curating them
turns out to be the part you dislike. That is a real possibility and it is worth
naming now, because switching later costs you the supervisor and nothing else.

## The other three drivers

Engine choice does not address these. They are worth solving in the same
rebuild.

### Reliability under power cuts

This is a battery instrument that gets switched off, not shut down. The fix is a
**read-only root filesystem with overlayfs**, which Raspberry Pi OS supports
directly through `raspi-config`. Writes go to a RAM overlay, the SD card is
never written during normal running, and yanking power cannot corrupt it.

Patches and settings live on a small separate partition mounted read-write only
while saving, then remounted read-only.

This alone probably retires the reliability driver.

### Boot time

Raspberry Pi OS Lite reaches userspace on a Pi 4 in roughly 10-15 seconds.
Measure first with `systemd-analyze blame` and `systemd-analyze critical-chain`,
then cut what a headless instrument does not need: avahi, bluetooth, triggerhappy,
swap, and dhcpcd if it never touches a network.

Around 8-10 seconds is a realistic target without exotic measures. Going much
below that means a lighter base than Raspberry Pi OS, which costs more than it
returns here.

### Reproducibility

**Ansible against stock Raspberry Pi OS Lite.** A playbook is readable, diffable
and re-runnable, and it keeps you on a base that receives security updates.

A custom image boots faster to a known state but is harder to evolve. Containers
are a poor fit: audio, realtime scheduling and USB MIDI passthrough all fight
containerisation on a Pi, for no benefit on a single-purpose device.

## Latency notes

With a proper HAT rather than the onboard audio, a Pi 4 handles JACK at 64
frames with 2 periods without persistent xruns, and 256 frames at 48kHz is
5.33ms. Onboard audio cannot usefully go below 256 frames because of its
intermediate buffer, which does not apply to you.

`PREEMPT_RT` only helps if the audio threads are actually prioritised, meaning
`SCHED_FIFO` and something like `jackd -P 90`. Try a stock kernel with correct
priorities first and only chase a realtime kernel if xruns show up under real
playing.

Start at 128 or 256 frames, get it stable, then tighten.

## Sources

- [Zynthian engine list](https://zynthian.org/engines)
- [Raspberry Pi low-latency audio, linuxaudio.org](https://wiki.linuxaudio.org/wiki/raspberrypi)
