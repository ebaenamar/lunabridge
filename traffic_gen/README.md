# traffic_gen — realistic uplink emulation for LunaBridge

Generates **realistic per-class uplink traffic from the UERANSIM UE** so that
real packets traverse the live 5G stack and are captured at N6 into
`bundles.jsonl`. That capture is the arrival **trace** the existing batch
scheduler replays — this is the input side of the **Option B (trace-driven)**
pipeline:

```
UE flows (traffic_gen)
   → gNB → Open5GS UPF → ogstun → N6 NFQUEUE
   → gateway/n6_interceptor.py  →  runs/<run>/bundles.jsonl   (real ingress_ts / size / class)
   → [time-remap step — NEXT, not built yet]
   → gateway/scheduler.py  Scheduler.run()  →  terminal states + telemetry
```

Before this, the scheduler ran on *synthetic* bundles. Here the **input becomes
real** while the clean, reproducible discrete-event scheduler stays untouched.

## Why these flows

Bitrates are sized **relative to the 10 Mbps relay** (`lcrns_relay_contact_plan_1sv.csv`),
not the 20 MHz 5G access — the relay is the bottleneck that makes scheduling and
buffer sizing matter. At peak (a science dump overlapping the EVA video window)
the aggregate offered load is **~11 Mbps > 10 Mbps**, so the buffer fills even
during a live contact; across a blackout it fills regardless of instantaneous
rate.

| Flow | Class | DSCP | Rate | Window | Shape |
|------|-------|------|------|--------|-------|
| `tlm_housekeeping` | TELEMETRY | 26 (AF31) | ~16 kbps | always | PERIODIC (CBR) |
| `emg_alerts` | EMERGENCY | 46 (EF) | ~1/30 s, 128 B | always | POISSON |
| `sci_dump_1` | SCIENCE_BULK | 10 (AF11) | ~8 Mbps | 60–100 s | PERIODIC |
| `sci_dump_2` | SCIENCE_BULK | 10 (AF11) | ~8 Mbps | 120–160 s | PERIODIC |
| `eva_video` | MEDIA | 34 (AF41) | ~3 Mbps | 110–230 s | PERIODIC |

Every DSCP is a codepoint from the gateway's own `DSCP_TO_CLASS` table, and
`traffic_model.verify_marks()` asserts each one classifies back to its intended
class using the **real** `gateway/priority_classifier.py`. A drift is a loud
failure, never a silent demotion to best-effort.

## Files

- `traffic_model.py` — **single source of truth**: `FlowSpec`, `TrafficScenario`,
  `reference_scenario()`, DSCP marks, and `verify_marks()`. All numbers live here.
- `gen_mgen.py` — renders a scenario into an MGEN `.mgn` script (TOS taken from
  the model, never recomputed).
- `run_traffic.sh` — enters the UE netns/container and runs MGEN (or an iperf3
  fallback). The one environment-specific piece; all knobs are env vars.

## Run

```bash
# 1. sanity: verify marks + see the offered-load profile
python3 -m traffic_gen.traffic_model

# 2. render the MGEN script
python3 -m traffic_gen.gen_mgen --dst <EARTH_SINK_IP> --duration 600 \
    --out scenarios/eva_dump.mgn

# 3. drive it from the UE (packets hit N6, interceptor writes bundles.jsonl)
DST=<EARTH_SINK_IP> UE_CONTAINER=ueransim-ue bash traffic_gen/run_traffic.sh
#   iperf3 fallback (no MGEN in the UE image): SENDER=iperf3 ...
```

Prereq (once per session, UPF side — see `gateway/n6_interceptor.py` header):
```bash
docker exec upf iptables -I FORWARD -i ogstun -j NFQUEUE --queue-num 0
```
and run the interceptor in the UPF netns.

## Verified on the testbed (nuwinsrack2, mgen 5.02b)

A loopback smoke capture confirmed the generator emits **real IP packets** and
each class carries its **correct DS field** on the wire, which the gateway's own
classifier maps back to the intended class:

| dst port | flow | DS byte | DSCP | `classify_packet` |
|----------|------|---------|------|-------------------|
| 5001 | telemetry | 0x68 | 26 | `telemetry` |
| 5002 | emergency | 0xb8 | 46 | `emergency` |
| 5003 | sci_bulk  | 0x28 | 10 | `sci_bulk` |
| 5004 | media     | 0x88 | 34 | `media` |

Finding that shaped `gen_mgen.py`: MGEN shares one transmit socket among flows
to the same destination and TOS is a socket option, so **without a distinct SRC
port per flow the last flow's TOS overwrites all others** (all four came out
with a single DS field). The renderer now emits `SRC <base_src_port+i>` per flow
to force separate sockets. It also emits a closing `OFF` per flow so the script
is self-terminating (MGEN with only run-length flows never exits).

## Known modelling artifacts (flag in the paper, do not silently absorb)

1. **packet == bundle.** `n6_interceptor.py` makes one `BundleRecord` per IP
   packet, so an 8 Mbps 40 s dump becomes ~28 k MTU-sized bundles, not one ADU.
   `msg_size` is kept at a realistic 1400 B MTU so per-bundle sizes are
   plausible; application-layer bundling is future work, not emulated.
2. **ToS survives the tunnel?** The DSCP must reach `ogstun` intact for the
   classifier to see it. Verify Open5GS/UERANSIM don't reset the DS field on the
   uplink DRB before trusting the marks — check a captured `bundles.jsonl` shows
   the four classes, not all `sci_bulk` (which is what a wiped DS field would
   look like, since 0 → best-effort → SCIENCE_BULK).

## Next step in the pipeline (not built here)

The captured `ingress_ts` is wall-clock "now"; the contact plan is in 2027 UTC
with hour-long gaps. A **time-remap / compression layer** must map capture time
onto the plan timeline before `Scheduler.run()`. That is the next module — this
generator produces the realistic arrivals it will consume.
