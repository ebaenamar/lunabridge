# Sionna RT — lunar surface access channel (POC)

First-principles ray tracing of the **lunar surface** 5G access channel, as the
physically correct, reproducible alternative to:

- the in-loop GNU Radio `channels.channel_model` broker (needs the fragile live
  srsRAN ZMQ testbed), and
- Earth-NTN models (3GPP TR 38.811 / OpenNTN), which model atmospheric
  absorption, iono/tropo scintillation and land-mobile-satellite clutter that
  **do not exist on the Moon**.

On the lunar surface the channel is governed by the **terrain**: two-ray ground
bounce, and diffraction/shadowing at crater rims. Sionna RT traces exactly that.

## What the POC shows

`lunar_rt_poc.py` builds a synthetic crater-rim scene, places a fixed gNB, sweeps
a rover from line-of-sight across the rim into geometric shadow, and ray-traces
the channel (specular ground reflection + edge diffraction, regolith material
`eps_r=3.0`, loss tangent `0.008`) at S-band (2.5 GHz, matching the paper).

Result (`figs/lunar_rt_poc.png`): LOS path loss ≈66–79 dB with two-ray lobes,
then a **~50–59 dB cliff** at the rim into a 113–156 dB diffracted shadow floor —
the paper's "coverage boundaries are cliffs, not fades," from physics, not a
heuristic.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install sionna-rt trimesh matplotlib numpy
python lunar_rt_poc.py --out figs            # ~1 min on an M3 (Metal) / any GPU
```

Sionna RT 2.2 / Mitsuba 3.9; auto-selects the Metal (Apple) or CUDA backend, runs
on CPU (LLVM) otherwise. No TensorFlow or live testbed needed.

## LOLA-ready

`crater_rim_heightfield()` returns a `(verts, faces)` heightfield mesh. To ray-
trace **real** south-pole terrain, replace its `Z` with a PGDA LOLA 5 m/px DEM
patch (`Z = dem[j, i]`) — same mesh → Sionna pipeline, same `lunaremu` Connecting
Ridge site as `analysis/fig_channel.py` (paper Fig. 2). The rover path then
follows the real traverse and the cliffs fall at the real crater rims.

## Next

- Swap synthetic ridge → LOLA DEM patch (Connecting Ridge).
- Feed per-position CIR into a 5G-NR link-level chain → BLER / throughput vs
  traverse (the "access-realism" result, reproducibly, with no live stack).
