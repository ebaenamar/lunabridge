"""
traffic_gen/traffic_model.py

Single source of truth for the LunaBridge realistic-traffic emulation model.
This is the INPUT side of the Option-B (trace-driven) pipeline decided this
session:

    UE flows (this model)  ->  gNB -> Open5GS UPF -> N6 NFQUEUE
        -> n6_interceptor writes bundles.jsonl (real ingress_ts/size/class)
        -> [time-remap step, next]  ->  Scheduler.run()  (existing DES)

What this module is and is NOT
------------------------------
IS   : a declarative, reproducible per-class flow model (bitrate, packet
       size, arrival pattern, DSCP mark, on/off schedule) plus a self-check
       that every DSCP we emit classifies back to the class we intend, using
       the repo's OWN classifier (gateway/priority_classifier.py) -- so the
       marks can never silently drift from the gateway taxonomy.
IS NOT: a packet sender. Rendering to an MGEN script or an iperf3 launcher
        lives in gen_mgen.py / run_traffic.sh. Keeping the model tool-agnostic
        means the same parameters drive MGEN today and anything else later.

Design decisions locked this session
------------------------------------
  1. DSCP marks: the PRIMARY codepoint per class, chosen from the classifier's
     own DSCP_TO_CLASS table (priority_classifier.py:33) so no mark is ever an
     "unknown codepoint" that would be demoted to best-effort SCIENCE_BULK.
       EMERGENCY    -> 46  (EF,   RFC 4594 Telephony, policy-relabelled)
       TELEMETRY    -> 26  (AF31, policy relabel)
       SCIENCE_BULK -> 10  (AF11)
       MEDIA        -> 34  (AF41, RFC 4594 Multimedia Conferencing)
     verify_marks() asserts this against the live classifier at import-test
     time; a mismatch is a loud failure, not a warning.

  2. Direction: UPLINK only (rover Moon -> Earth). That is the direction the
     N6 interceptor sees on ogstun and the direction the relay's 10 Mbps
     contact has to carry. Downlink is out of scope for the trace.

  3. Bitrates are chosen RELATIVE TO THE RELAY, not the 5G access. The relay
     contact is 10 Mbps (lcrns_relay_contact_plan_1sv.csv rate_bps=1e7). The
     model is deliberately sized so that at peak (a science dump overlapping an
     EVA-media window) the AGGREGATE uplink EXCEEDS 10 Mbps -- otherwise the
     buffer never fills and the scheduler-policy comparison has nothing to
     compare. The 5G access (20 MHz) is not the bottleneck and is not modelled
     as one.

  4. Packet size == bundle size. The interceptor makes one BundleRecord per IP
     packet (n6_interceptor.py). So msg_size here IS the bundle size the
     scheduler will see. A 10 MB science dump therefore becomes ~7000 MTU-sized
     bundles, not one ADU -- a real modelling artifact of the packet==bundle
     prototype, flagged for the paper (application-layer bundling is future
     work, not emulated here). We keep msg_size at a realistic MTU (1400 B) so
     the per-bundle byte sizes are at least plausible.

  5. All numbers live HERE, nowhere else, and every flow carries a one-line
     rationale. A run can scale duration / rates via TrafficScenario without
     editing any sender.

Sources for the class semantics (bitrate/burstiness shape, not exact values):
  - TrafficClass taxonomy + TTLs: gateway/traffic.py CLASS_SPECS.
  - DSCP codepoints: RFC 2474 / RFC 4594, via gateway/priority_classifier.py.
  - Housekeeping-telemetry being a low-rate continuous heartbeat, science being
    episodic high-volume dumps, EVA video being on/off CBR, and emergency being
    rare small event-driven alerts: standard spacecraft-ops traffic shape. The
    exact kbps/Mbps are LunaBridge modelling choices, sized per decision 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


# Kept as a local mirror of gateway.traffic.TrafficClass VALUES so this module
# is importable without the gateway package on sys.path (the sender host may
# not have it). verify_marks() reconciles against the real classifier when it
# IS importable -- that is where drift is caught, loudly.
class ClassName(str, Enum):
    EMERGENCY = "emergency"
    TELEMETRY = "telemetry"
    SCIENCE_BULK = "sci_bulk"
    MEDIA = "media"


class Pattern(str, Enum):
    """Arrival pattern shapes, matching MGEN's own pattern keywords so the
    renderer is a direct translation, not a reinterpretation."""
    PERIODIC = "PERIODIC"   # constant bit rate: [msgs_per_sec, msg_size]
    POISSON = "POISSON"     # Poisson arrivals at an average rate: [ave_mps, size]


# Primary DSCP per class -- see decision 1. These MUST be keys in
# gateway/priority_classifier.py's DSCP_TO_CLASS and map to the intended class.
PRIMARY_DSCP = {
    ClassName.EMERGENCY: 46,     # EF
    ClassName.TELEMETRY: 26,     # AF31
    ClassName.SCIENCE_BULK: 10,  # AF11
    ClassName.MEDIA: 34,         # AF41
}


def dscp_to_tos(dscp: int) -> int:
    """DS field byte from a 6-bit DSCP: DSCP occupies the top 6 bits, ECN the
    bottom 2 (RFC 2474). ECN left at 0. This is the value MGEN's `TOS` option
    and iperf3's `--tos` both expect (the full 8-bit byte, not the 6-bit
    codepoint) -- getting this wrong is the classic silent-misclassification
    bug, so the renderer takes TOS from here and nowhere else."""
    return (dscp & 0x3F) << 2


@dataclass(frozen=True)
class FlowSpec:
    """One emulated uplink flow.

    name         : stable id, used as the MGEN flow id label / iperf title.
    cls          : which TrafficClass this flow represents.
    pattern      : PERIODIC (CBR) or POISSON (bursty average-rate).
    msgs_per_sec : messages/second (PERIODIC) or average messages/second
                   (POISSON).
    msg_size     : IP payload bytes per message == resulting bundle size
                   (decision 4). Keep <= path MTU to avoid fragmentation.
    start_s      : seconds after scenario t0 to turn this flow ON.
    stop_s       : seconds after t0 to turn it OFF. None == runs to scenario end.
    rationale    : one line, why these numbers -- carried into generated
                   artifacts so a reader never has to come back here.
    """
    name: str
    cls: ClassName
    pattern: Pattern
    msgs_per_sec: float
    msg_size: int
    start_s: float
    stop_s: float | None
    rationale: str

    @property
    def dscp(self) -> int:
        return PRIMARY_DSCP[self.cls]

    @property
    def tos(self) -> int:
        return dscp_to_tos(self.dscp)

    @property
    def bits_per_sec(self) -> float:
        """Nominal offered load of this flow while ON. For POISSON this is the
        AVERAGE rate (bursts exceed it instantaneously)."""
        return self.msgs_per_sec * self.msg_size * 8


@dataclass
class TrafficScenario:
    """A full set of concurrent flows over a fixed wall-clock duration.

    duration_s scales the whole run; individual flow start/stop are clamped to
    it by the renderer. Everything is reproducible: no randomness lives in this
    object -- POISSON's randomness is the sender's, seeded there so a rerun with
    the same seed reproduces the same trace.
    """
    duration_s: float
    flows: List[FlowSpec] = field(default_factory=list)

    def peak_offered_bps(self, sample_step_s: float = 1.0) -> float:
        """Worst-case instantaneous aggregate offered load across the run,
        sampling the on/off schedule. This is the number to compare against the
        relay's 10 Mbps: if it never exceeds 10 Mbps the buffer never fills and
        the policy comparison is moot (decision 3)."""
        peak = 0.0
        t = 0.0
        while t <= self.duration_s:
            agg = sum(
                f.bits_per_sec for f in self.flows
                if f.start_s <= t and (f.stop_s is None or t < f.stop_s)
            )
            peak = max(peak, agg)
            t += sample_step_s
        return peak


# ---------------------------------------------------------------------------
# The reference scenario -- "EVA + science dump over a degraded relay".
# Sized (decision 3) so science(8 Mbps) + media(2 Mbps) overlap ~t=120..150s
# to push aggregate PAST the 10 Mbps relay, while telemetry runs continuously
# and emergencies arrive sporadically throughout.
# ---------------------------------------------------------------------------
def reference_scenario(duration_s: float = 600.0) -> TrafficScenario:
    return TrafficScenario(
        duration_s=duration_s,
        flows=[
            # Continuous housekeeping heartbeat. Small packets, low rate, never
            # off -- this is the class whose starvation vs deadline-loss under
            # each policy is the paper's headline, so it must always be present.
            FlowSpec(
                name="tlm_housekeeping", cls=ClassName.TELEMETRY,
                pattern=Pattern.PERIODIC, msgs_per_sec=4, msg_size=512,
                start_s=0.0, stop_s=None,
                rationale="~16 kbps CBR housekeeping heartbeat, always on.",
            ),
            # Sporadic distress/abort alerts. Poisson, tiny, rare, always armed.
            # Very low volume but each one is rank-0; the whole point is that a
            # good policy delivers these even while the buffer is jammed with
            # science.
            FlowSpec(
                name="emg_alerts", cls=ClassName.EMERGENCY,
                pattern=Pattern.POISSON, msgs_per_sec=1.0 / 30.0, msg_size=128,
                start_s=0.0, stop_s=None,
                rationale="~1 alert/30s Poisson, 128 B; rare rank-0 events.",
            ),
            # Episodic science dump #1: 8 Mbps for 40 s. On its own it is under
            # the 10 Mbps relay; it fills the buffer only in aggregate / across
            # a blackout.
            FlowSpec(
                name="sci_dump_1", cls=ClassName.SCIENCE_BULK,
                pattern=Pattern.PERIODIC, msgs_per_sec=714, msg_size=1400,
                start_s=60.0, stop_s=100.0,
                rationale="~8 Mbps 40 s bulk dump (1400 B MTU-sized bundles).",
            ),
            # Science dump #2, deliberately overlapping the EVA media window to
            # create the >10 Mbps peak (decision 3).
            FlowSpec(
                name="sci_dump_2", cls=ClassName.SCIENCE_BULK,
                pattern=Pattern.PERIODIC, msgs_per_sec=714, msg_size=1400,
                start_s=120.0, stop_s=160.0,
                rationale="~8 Mbps 40 s dump, overlaps EVA media -> >10 Mbps.",
            ),
            # EVA video: 2 Mbps CBR for a 120 s "spacewalk", expendable/short
            # TTL. Overlaps sci_dump_2 so aggregate peaks at ~10.2 Mbps.
            FlowSpec(
                name="eva_video", cls=ClassName.MEDIA,
                pattern=Pattern.PERIODIC, msgs_per_sec=268, msg_size=1400,
                start_s=110.0, stop_s=230.0,
                rationale="~3 Mbps CBR EVA video (HD), 120 s window; expendable. "
                          "Overlaps sci_dump_2 -> ~11 Mbps peak, clearly > relay.",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Smoke scenario -- all four classes fire immediately at low rate, for a quick
# on-the-wire proof (tcpdump) that MGEN emits real IP packets carrying the
# right DS field per class. NOT a mission profile; just a mark/traversal test.
# ---------------------------------------------------------------------------
def smoke_scenario(duration_s: float = 5.0) -> TrafficScenario:
    return TrafficScenario(
        duration_s=duration_s,
        flows=[
            FlowSpec("smoke_tlm", ClassName.TELEMETRY, Pattern.PERIODIC,
                     5, 512, 0.0, None, "smoke: telemetry mark on the wire"),
            FlowSpec("smoke_emg", ClassName.EMERGENCY, Pattern.PERIODIC,
                     5, 128, 0.0, None, "smoke: emergency mark on the wire"),
            FlowSpec("smoke_sci", ClassName.SCIENCE_BULK, Pattern.PERIODIC,
                     5, 1400, 0.0, None, "smoke: sci_bulk mark on the wire"),
            FlowSpec("smoke_media", ClassName.MEDIA, Pattern.PERIODIC,
                     5, 1400, 0.0, None, "smoke: media mark on the wire"),
        ],
    )


# ---------------------------------------------------------------------------
# Trace scenario -- realistic class MIX but bounded volume (~30s, a few
# thousand packets), sized so a small contact plan + bounded queue force real
# contention (overflow + starvation) when replayed through the scheduler. Used
# to check that per-class OUTCOMES respect the priority labels.
# ---------------------------------------------------------------------------
def trace_scenario(duration_s: float = 30.0) -> TrafficScenario:
    return TrafficScenario(
        duration_s=duration_s,
        flows=[
            FlowSpec("tlm", ClassName.TELEMETRY, Pattern.PERIODIC,
                     4, 512, 0.0, None, "~16 kbps housekeeping, always on"),
            FlowSpec("emg", ClassName.EMERGENCY, Pattern.POISSON,
                     0.5, 128, 0.0, None, "~1 alert/2s Poisson; rank-0 events "
                     "spread across the run, incl. during buffer pressure"),
            FlowSpec("sci", ClassName.SCIENCE_BULK, Pattern.PERIODIC,
                     179, 1400, 5.0, 13.0, "~2 Mbps 8s bulk burst (~1430 pkts)"),
            FlowSpec("media", ClassName.MEDIA, Pattern.PERIODIC,
                     90, 1400, 8.0, 20.0, "~1 Mbps 12s EVA video (~1080 pkts)"),
        ],
    )


# ---------------------------------------------------------------------------
# Self-check: every mark we emit must classify back to its intended class,
# using the gateway's OWN classifier. Run: python -m traffic_gen.traffic_model
# (from the repo root, so `gateway` is importable).
# ---------------------------------------------------------------------------
def verify_marks() -> None:
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from gateway.priority_classifier import classify_packet, DSCP_TO_CLASS  # noqa: E402

    for cls, dscp in PRIMARY_DSCP.items():
        assert dscp in DSCP_TO_CLASS, (
            f"{cls.value}: DSCP {dscp} is NOT in the classifier's table -- it "
            f"would be demoted to best-effort. Pick a codepoint from "
            f"priority_classifier.DSCP_TO_CLASS."
        )
        got = classify_packet(dscp)
        assert got.value == cls.value, (
            f"{cls.value}: DSCP {dscp} classifies to {got.value}, not "
            f"{cls.value}. Mark and taxonomy have drifted."
        )
    print("verify_marks: OK -- all DSCP marks classify to their intended class")


if __name__ == "__main__":
    verify_marks()
    sc = reference_scenario()
    peak = sc.peak_offered_bps()
    print(f"\nreference_scenario: {len(sc.flows)} flows, "
          f"duration={sc.duration_s:.0f}s")
    print(f"peak aggregate offered load = {peak/1e6:.2f} Mbps "
          f"(relay = 10.00 Mbps) -> "
          f"{'BUFFER WILL FILL' if peak > 1e7 else 'WARNING: under relay, buffer will NOT fill'}")
    print("\nper-flow:")
    for f in sc.flows:
        span = f"{f.start_s:.0f}..{'end' if f.stop_s is None else f'{f.stop_s:.0f}'}s"
        print(f"  {f.name:18s} {f.cls.value:9s} dscp={f.dscp:>2} tos=0x{f.tos:02x} "
              f"{f.bits_per_sec/1e3:8.1f} kbps  {span:>12}  {f.pattern.value}")
