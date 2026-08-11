# analysis

## Headline result — onboard buffer sizing for safety-of-life delivery (`buffer_sizing.py`)

The engineering number the paper leads with. Question a lunar comms designer
actually has and can't answer without flying it: **how much onboard DTN buffer
guarantees delivery of safety-of-life traffic across the worst ELFO relay
blackout?** Buffer is a hardware spec line (mass/power/cost); too little = drop
EVA-safety traffic during a gap, too much = wasted mass.

On the real LCRNS max gap (**5.56 h**) with an illustrative lunar-surface
mission profile (science ~2 Mbps dominates; emergency+telemetry are the
protected safety classes):

| admission mode | buffer to GUARANTEE zero protected-class loss |
|---|---|
| native BPv7 (class-blind — RFC 9171 has no in-band priority) | **5.05 GB** (= total_rate × gap) |
| priority-aware admission (this work, DSCP-driven) | **40 MB** (= critical_rate × gap) |
| **reduction at equal safety guarantee** | **~126×** |

The mechanism: native BPv7 must buffer the WHOLE worst-case backlog to avoid
overflowing a late-arriving emergency; priority-aware admission evicts
low-priority science to admit critical traffic, so the buffer only needs to hold
the CRITICAL backlog → shrinks by ~ total_rate / critical_rate. A DES buffer
sweep validates it empirically: min buffer for zero protected-class loss is
503 MB (native) vs 4.0 MB (priority-aware) over a 2000 s gap — **125×**, matching
the analytical 126×. `priority_aware_admission=False` on `Scheduler` runs the
native-BPv7 baseline on the same plan+trace.

That is the useful, decision-changing output: our mechanism meets the same EVA
safety guarantee with ~2 orders of magnitude less onboard memory.

# trace → scheduler label-respect run

`run_trace_scheduler.py` replays a captured N6 trace (`runs/<run>/bundles.jsonl`,
produced by `traffic_gen/pcap_to_trace.py` from real DSCP-marked packets) through
the scheduler DES under each policy, with a **bounded** queue and a small contact
plan sized to force contention, and checks that per-class outcomes respect the
priority labels.

## Verified result (nuwinsrack2, real 2653-packet trace)

Label fidelity capture→trace: each MGEN flow's DSCP classified **purely** to its
intended class (telemetry 121, emergency 18, sci_bulk 1433, media 1081 — no
mixing), via the repo's own `gateway/priority_classifier.py`.

Trace 3.66 MB (29.3 Mbit) vs plan budget 9 Mbit, queue cap 0.8 MB (heavy
contention). Delivery ratio by class:

| policy | emergency | telemetry | sci_bulk | media | utility |
|--------|-----------|-----------|----------|-------|---------|
| FIFO (naive) | 0.39 | 0.31 | 0.43 | 0.03 | 1690 |
| STRICT_PRIORITY | **0.89** | 0.64 | 0.44 | 0.00 | **3000** |
| WFQ | **1.00** | 0.57 | 0.04 | 0.03 | 2544 |

Reads correctly:
- **STRICT_PRIORITY** delivery ratio is monotonic by rank (0.89 ≥ 0.64 ≥ 0.44 ≥
  0.00) — media starved to zero, emergencies protected. Highest utility.
- **WFQ** hard-preempts EMERGENCY (1.00) then fair-shares the remainder between
  sci_bulk/media (both ~0.04) — trades utility for low-class fairness.
- **FIFO** spreads loss across all classes *including emergencies* (0.39) — the
  floor baseline that ignores labels. Lowest utility. Labels demonstrably matter.

## Honest finding: admission is priority-blind

Under the tight 0.8 MB queue, **2 EMERGENCY bundles were lost to QUEUE_OVERFLOW**
even under STRICT_PRIORITY. Cause: `gateway/scheduler.py` `_try_admit` /
`_admit_arrivals` admit in **ingress order** and mark `QUEUE_OVERFLOW` when the
buffer is full — priority is enforced only at **drain** time, not at admission.
So when the buffer is full of science/media, a later-arriving rank-0 emergency
is dropped at the door.

Confirmed it is an admission (not drain) issue: with a queue that fits the whole
trace (4 MB), EMERGENCY returns to **18/18, overflow 0**, and every label-respect
check passes.

**FIXED** — `gateway/scheduler.py` now does priority-aware admission for
class-aware policies (evicts strictly-lower-priority queued bundles to admit a
higher-priority arrival; FIFO stays priority-blind as the floor baseline). With
the fix, the tight 0.8 MB run above delivers EMERGENCY **18/18, overflow 0**
under STRICT_PRIORITY and the label-respect check passes. See
`TestPriorityAwareAdmission` in `gateway/test_scheduler.py`.

## Run

```bash
# 1. capture real marked packets + convert to a trace
python3 -m traffic_gen.gen_mgen --trace --dst 127.0.0.1 --duration 30 --out trace.mgn
sudo tcpdump -i lo -n -w trace.pcap 'udp portrange 5001-5010' &   # then run mgen
python3 -m traffic_gen.pcap_to_trace trace.pcap runs/real_trace

# 2. replay through the scheduler + verify labels
python3 -m analysis.run_trace_scheduler runs/real_trace --queue-bytes 800000 \
    --win-rate 1000000 --win-dur 1.5 --win-period 6 --win-count 6
```
