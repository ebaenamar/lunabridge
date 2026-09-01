"""
analysis/buffer_sizing.py

THE headline engineering result: how much onboard DTN buffer ("memory") a lunar
surface asset needs to GUARANTEE delivery of safety-of-life traffic across the
worst ELFO relay blackout -- native BPv7 vs. priority-aware admission.

Why this is the useful number: buffer size is a hardware spec line (mass, power,
cost on a lunar asset). Too little -> drop EVA-safety traffic during a relay gap
(mission risk). Too much -> wasted mass. Today a comms engineer can't size it
without flying it. This gives the tradeoff, and the saving our mechanism buys.

The mechanism (proved, not asserted):
  - NATIVE BPv7 (no in-band priority field, RFC 9171 -> admission is class-blind):
    to never overflow an EMERGENCY, the buffer must hold the WHOLE worst-case
    backlog accumulated during the max gap = total_rate * max_gap.
  - PRIORITY-AWARE admission (DSCP-driven, this project): low-priority bundles
    are evicted to admit critical ones, so the buffer only needs to hold the
    CRITICAL backlog = critical_rate * max_gap.
  => safety buffer shrinks by ~ total_rate / critical_rate, at the SAME guarantee.

Analytical thresholds use the REAL LCRNS max gap; a DES sweep on a shorter,
manageable gap VALIDATES that the empirical min-buffer-for-zero-EMERGENCY-loss
matches those thresholds under each admission mode.

Usage:  python -m analysis.buffer_sizing
"""

from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gateway.contact_plan import ContactPlan, ContactWindow           # noqa: E402
from gateway.scheduler import Scheduler, SchedulingPolicy             # noqa: E402
from gateway.telemetry import BundleRecord, TerminalState             # noqa: E402
from gateway.traffic import TrafficClass, CLASS_SPECS                 # noqa: E402

CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "gateway", "lcrns_relay_contact_plan_1sv.csv")


@dataclass
class ClassLoad:
    """Sustained mission-profile generation for one class."""
    cls: TrafficClass
    byte_rate: float   # bytes/s generated on average
    bundle_size: int   # bytes per bundle (ADU granularity)


# Illustrative lunar-surface mission profile (documented choices, not flown data).
# Science dominates volume; emergency is tiny but rank-0 safety-of-life.
MISSION_PROFILE = [
    ClassLoad(TrafficClass.EMERGENCY,    byte_rate=128 / 60.0, bundle_size=128),    # ~1 alert/min
    ClassLoad(TrafficClass.TELEMETRY,    byte_rate=2000.0,     bundle_size=8192),   # ~16 kbps housekeeping
    ClassLoad(TrafficClass.SCIENCE_BULK, byte_rate=250000.0,   bundle_size=65536),  # ~2 Mbps instrument
]
# Classes that MUST NOT be dropped (the safety guarantee). rank<=1 => EMERGENCY+TELEMETRY.
PROTECTED_MAX_RANK = 1


def real_max_gap(csv_path: str = CSV_PATH) -> float:
    """Largest blackout (seconds) between consecutive contacts in the real plan."""
    rows = list(csv.DictReader(open(csv_path)))
    starts = [float(r["start_sec"]) for r in rows]
    ends = [float(r["end_sec"]) for r in rows]
    gaps = [starts[i + 1] - ends[i] for i in range(len(rows) - 1)]
    return max(gaps)


def analytical(profile, gap_s):
    total_rate = sum(c.byte_rate for c in profile)
    crit_rate = sum(c.byte_rate for c in profile
                    if CLASS_SPECS[c.cls].rank <= PROTECTED_MAX_RANK)
    return {
        "total_rate": total_rate, "crit_rate": crit_rate,
        "B_native": total_rate * gap_s, "B_critical": crit_rate * gap_s,
        "ratio": total_rate / crit_rate,
    }


def make_trace(profile, gap_s):
    """Deterministic per-class periodic arrivals over [0, gap_s]."""
    bundles, seq = [], 0
    for c in profile:
        interval = c.bundle_size / c.byte_rate
        t = 0.0
        while t < gap_s:
            seq += 1
            b = BundleRecord(str(seq), c.cls.value, c.cls, c.bundle_size, ingress_ts=t)
            b.set_ttl()
            bundles.append(b)
            t += interval
    return bundles


def all_protected_delivered(records):
    """True iff EVERY protected-class (rank<=PROTECTED_MAX_RANK) bundle was
    delivered -- the safety guarantee. Matches the analytical critical_rate,
    which sums the same ranks."""
    prot = [r for r in records
            if CLASS_SPECS[r.traffic_class].rank <= PROTECTED_MAX_RANK]
    if not prot:
        return True
    return all(r.terminal_state is TerminalState.DELIVERED for r in prot)


def min_buffer_for_zero_protected_loss(profile, gap_s, priority_aware):
    """Binary-search the smallest buffer (bytes) that delivers EVERY protected
    bundle, with a post-gap contact large enough that admission is the only
    loss path."""
    bundles = make_trace(profile, gap_s)
    total_bytes = sum(b.size_bytes for b in bundles)
    # one huge contact after the gap: drain is never the constraint here
    plan = ContactPlan([ContactWindow("drain", start_ts=gap_s, end_ts=gap_s + 10.0,
                                       link_rate_bps=total_bytes * 8 * 10)])
    import copy
    lo, hi = 1.0, float(total_bytes)
    # ensure hi delivers all (sanity)
    def safe(cap):
        s = Scheduler(plan, max_queue_bytes=cap,
                      policy=SchedulingPolicy.STRICT_PRIORITY,
                      priority_aware_admission=priority_aware)
        recs = copy.deepcopy(bundles)
        s.run(recs)
        return all_protected_delivered(recs)
    for _ in range(40):
        mid = (lo + hi) / 2
        if safe(mid):
            hi = mid
        else:
            lo = mid
    return hi, total_bytes


def main():
    gap = real_max_gap()
    a = analytical(MISSION_PROFILE, gap)

    print("=" * 68)
    print("ONBOARD DTN BUFFER SIZING FOR SAFETY-OF-LIFE DELIVERY")
    print("=" * 68)
    print(f"real LCRNS max blackout gap      : {gap:.0f} s ({gap/3600:.2f} h)")
    print(f"mission profile (bytes/s)        : "
          + ", ".join(f"{c.cls.value}={c.byte_rate:.1f}" for c in MISSION_PROFILE))
    print(f"total generation rate            : {a['total_rate']/1e3:.1f} kB/s")
    print(f"critical rate (rank<={PROTECTED_MAX_RANK}, protected): {a['crit_rate']/1e3:.2f} kB/s")
    print()
    print("ANALYTICAL buffer to GUARANTEE zero EMERGENCY loss across the gap:")
    print(f"  native BPv7 (class-blind admission)  B = total_rate * gap "
          f"= {a['B_native']/1e9:.2f} GB")
    print(f"  priority-aware admission (this work) B = crit_rate  * gap "
          f"= {a['B_critical']/1e6:.1f} MB")
    print(f"  >>> BUFFER REDUCTION AT EQUAL SAFETY GUARANTEE: {a['ratio']:.0f}x <<<")

    # DES validation on a shorter, manageable gap (ratio is gap-independent)
    val_gap = 2000.0
    print()
    print(f"DES validation (gap={val_gap:.0f}s, post-gap drain unconstrained):")
    b_native, tot = min_buffer_for_zero_protected_loss(MISSION_PROFILE, val_gap, False)
    b_prio, _ = min_buffer_for_zero_protected_loss(MISSION_PROFILE, val_gap, True)
    va = analytical(MISSION_PROFILE, val_gap)
    print(f"  trace over gap: {tot/1e6:.2f} MB total backlog")
    print(f"  min buffer for zero PROTECTED-class loss (rank<={PROTECTED_MAX_RANK}):")
    print(f"    native BPv7      : {b_native/1e6:7.3f} MB  "
          f"(~total backlog {va['B_native']/1e6:.2f} MB)")
    print(f"    priority-aware   : {b_prio/1e6:7.3f} MB  "
          f"(~critical backlog {va['B_critical']/1e6:.3f} MB)")
    print(f"    empirical reduction: {b_native/b_prio:.0f}x  "
          f"(analytical predicts {va['ratio']:.0f}x)")


if __name__ == "__main__":
    main()
