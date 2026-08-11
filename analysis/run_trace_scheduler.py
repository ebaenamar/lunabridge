"""
analysis/run_trace_scheduler.py

Replay a captured N6 trace (runs/<run>/bundles.jsonl) through the scheduler DES
with a BOUNDED queue and a small contact plan, under each policy, and CHECK that
the per-class outcomes respect the priority labels.

This is the "do the results make sense and respect the tags" step. It does NOT
tune numbers to get a pretty answer -- it prints the real per-class breakdown
and states plainly which invariants hold and which do not (e.g. admission is
ingress-order, so it can be priority-blind under overflow -- reported, not hidden).

Contact plan: built on the SAME relative timeline as the trace (ingress_ts is
seconds-since-first-packet), so no absolute-epoch remap is needed. Windows are
sized (via --win-*) to force contention; that is the point.

Usage:
    python -m analysis.run_trace_scheduler runs/real_trace \
        --queue-bytes 800000 --win-rate 1000000 --win-dur 1.5 \
        --win-period 6 --win-count 6
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gateway.contact_plan import ContactPlan, ContactWindow            # noqa: E402
from gateway.scheduler import Scheduler, SchedulingPolicy              # noqa: E402
from gateway.telemetry import (                                        # noqa: E402
    read_bundles, delivery_ratio_by_class, mission_utility, starvation_summary,
)
from gateway.traffic import TrafficClass, CLASS_SPECS                  # noqa: E402
import copy                                                            # noqa: E402


RANK_ORDER = sorted(TrafficClass, key=lambda c: CLASS_SPECS[c].rank)


def build_plan(rate, dur, period, count, start):
    return ContactPlan([
        ContactWindow(contact_id=f"c{i}", start_ts=start + i * period,
                      end_ts=start + i * period + dur, link_rate_bps=rate)
        for i in range(count)
    ])


def outcome_table(records):
    """Per-class: total, delivered, ttl_expired, never_scheduled, queue_overflow."""
    from gateway.telemetry import TerminalState
    rows = {}
    for r in records:
        k = r.traffic_class.value
        d = rows.setdefault(k, dict(total=0, delivered=0, ttl=0, never=0, overflow=0))
        d["total"] += 1
        st = r.terminal_state
        if st is TerminalState.DELIVERED: d["delivered"] += 1
        elif st is TerminalState.TTL_EXPIRED: d["ttl"] += 1
        elif st is TerminalState.NEVER_SCHEDULED: d["never"] += 1
        elif st is TerminalState.QUEUE_OVERFLOW: d["overflow"] += 1
    return rows


def run_policy(bundles, plan, queue_bytes, policy):
    b = copy.deepcopy(bundles)
    sched = Scheduler(plan, max_queue_bytes=queue_bytes, policy=policy)
    sched.run(b)
    return b


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--queue-bytes", type=float, default=800_000)
    ap.add_argument("--win-rate", type=float, default=1_000_000)
    ap.add_argument("--win-dur", type=float, default=1.5)
    ap.add_argument("--win-period", type=float, default=6.0)
    ap.add_argument("--win-count", type=int, default=6)
    ap.add_argument("--win-start", type=float, default=2.0)
    args = ap.parse_args()

    bundles = read_bundles(args.run_dir)
    total_bytes = sum(r.size_bytes for r in bundles)
    plan = build_plan(args.win_rate, args.win_dur, args.win_period,
                      args.win_count, args.win_start)
    plan_budget = args.win_rate * args.win_dur * args.win_count  # bits

    print(f"trace: {len(bundles)} bundles, {total_bytes/1e6:.2f} MB "
          f"({total_bytes*8/1e6:.2f} Mbit)")
    print(f"plan : {args.win_count} windows x {args.win_dur}s @ "
          f"{args.win_rate/1e6:.1f} Mbps = {plan_budget/1e6:.2f} Mbit budget")
    print(f"queue: {args.queue_bytes/1e6:.2f} MB cap  "
          f"(contention expected: budget<trace and cap<trace)")

    results = {}
    for pol in (SchedulingPolicy.FIFO, SchedulingPolicy.STRICT_PRIORITY,
                SchedulingPolicy.WFQ):
        recs = run_policy(bundles, plan, args.queue_bytes, pol)
        results[pol] = recs
        tab = outcome_table(recs)
        print(f"\n===== policy = {pol.value} =====")
        print(f"  mission_utility = {mission_utility(recs):.1f}")
        print(f"  {'class':<10} {'tot':>5} {'deliv':>6} {'ddr':>6} "
              f"{'ttl':>4} {'never':>6} {'ovfl':>5}")
        for cls in RANK_ORDER:
            k = cls.value
            d = tab.get(k)
            if not d:
                continue
            ddr = d["delivered"] / d["total"] if d["total"] else 0.0
            print(f"  {k:<10} {d['total']:>5} {d['delivered']:>6} {ddr:>6.2f} "
                  f"{d['ttl']:>4} {d['never']:>6} {d['overflow']:>5}")

    # ---- label-respect checks -------------------------------------------
    print("\n===== label-respect verification =====")
    sp = outcome_table(results[SchedulingPolicy.STRICT_PRIORITY])
    fi = outcome_table(results[SchedulingPolicy.FIFO])

    def ddr(tab, cls):
        d = tab.get(cls.value)
        return (d["delivered"] / d["total"]) if d and d["total"] else None

    checks = []
    # 1. strict-priority must not deliver a lower class at a BETTER ratio than a
    #    higher class when the higher class still has losses (monotonic-ish).
    prev = None
    mono = True
    for cls in RANK_ORDER:
        r = ddr(sp, cls)
        if r is None:
            continue
        if prev is not None and r > prev + 1e-9:
            mono = False
        prev = r
    checks.append(("strict-priority: delivery ratio non-increasing by rank",
                   mono))

    # 2. EMERGENCY should do NO WORSE under strict-priority than under FIFO.
    e_sp, e_fi = ddr(sp, TrafficClass.EMERGENCY), ddr(fi, TrafficClass.EMERGENCY)
    if e_sp is not None and e_fi is not None:
        checks.append((f"EMERGENCY ddr strict({e_sp:.2f}) >= FIFO({e_fi:.2f})",
                       e_sp + 1e-9 >= e_fi))

    # 3. utility: prioritized policy should beat naive FIFO.
    u_sp = mission_utility(results[SchedulingPolicy.STRICT_PRIORITY])
    u_fi = mission_utility(results[SchedulingPolicy.FIFO])
    checks.append((f"mission_utility strict({u_sp:.0f}) >= FIFO({u_fi:.0f})",
                   u_sp >= u_fi))

    # 4. HONEST admission check: does any EMERGENCY die to QUEUE_OVERFLOW under
    #    strict priority? Admission is ingress-order, so it CAN -- we report it.
    e_ovfl = sp.get("emergency", {}).get("overflow", 0)
    checks.append(("no EMERGENCY lost to admission overflow (strict)",
                   e_ovfl == 0))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print(f"\nOVERALL: {'labels respected' if ok else 'SEE FAILS ABOVE (finding)'}")


if __name__ == "__main__":
    main()
