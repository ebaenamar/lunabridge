"""
traffic_gen/pcap_to_trace.py

Convert a pcap of REAL marked packets into the N6 arrival trace (bundles.jsonl)
that the scheduler replays -- the honest realization of the Option-B pipeline
without needing the full 5G core: the packets really crossed a socket carrying
the right DS field, and the SAME gateway classifier + flow-id logic turn them
into BundleRecords, exactly as gateway/n6_interceptor.py would.

    real packets (MGEN, DSCP-marked)  ->  tcpdump pcap
        ->  [this]  classify_packet(dscp) + derive_flow_id + size + ts
        ->  runs/<run>/bundles.jsonl   (PENDING BundleRecords, TTL set)

Label fidelity is the whole point: traffic_class comes ONLY from the packet's
real DS field via gateway/priority_classifier.py. If a mark was wiped anywhere
upstream, it shows up here as best-effort SCIENCE_BULK, not a silent guess.

ingress_ts is seconds-since-first-packet (relative), so a contact plan can be
built on the same relative timeline (minimal time-remap). Absolute epoch is not
meaningful across the capture/plan boundary.

Needs scapy. Usage:
    python -m traffic_gen.pcap_to_trace capture.pcap runs/real_trace
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gateway.priority_classifier import classify_packet          # noqa: E402
from gateway.n6_interceptor import derive_flow_id                # noqa: E402
from gateway.telemetry import BundleRecord, TelemetryWriter      # noqa: E402


def convert(pcap_path: str, run_dir: str) -> dict:
    from scapy.all import PcapReader, IP  # imported here so --help works w/o scapy

    writer = TelemetryWriter(run_dir, config={
        "source": "pcap_to_trace", "pcap": os.path.abspath(pcap_path),
        "note": "ingress_ts is seconds-since-first-packet (relative)",
    })

    seq = 0
    t0 = None
    per_class: dict[str, int] = {}
    per_port_class: dict[int, dict[str, int]] = {}

    with PcapReader(pcap_path) as pr:
        for pkt in pr:
            if IP not in pkt:
                continue
            ip = pkt[IP]
            raw = bytes(ip)
            dscp = (ip.tos >> 2) & 0x3F          # same bit math as _extract_dscp
            tc = classify_packet(dscp)
            if t0 is None:
                t0 = float(pkt.time)
            seq += 1
            rec = BundleRecord(
                bundle_id=str(seq),
                flow_id=derive_flow_id(raw),
                traffic_class=tc,
                size_bytes=len(ip),
                ingress_ts=float(pkt.time) - t0,
            )
            rec.set_ttl()
            writer.write_bundle(rec)

            per_class[tc.value] = per_class.get(tc.value, 0) + 1
            dport = ip.dport if hasattr(ip, "dport") else -1
            per_port_class.setdefault(dport, {})
            per_port_class[dport][tc.value] = per_port_class[dport].get(tc.value, 0) + 1

    return {"total": seq, "per_class": per_class, "per_port_class": per_port_class,
            "run_dir": run_dir}


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    pcap_path, run_dir = sys.argv[1], sys.argv[2]
    summary = convert(pcap_path, run_dir)
    print(f"wrote {summary['total']} bundles -> {run_dir}/bundles.jsonl")
    print("per-class counts:", summary["per_class"])
    print("per dst-port -> class (each MGEN flow is one class; must be pure):")
    for port in sorted(summary["per_port_class"]):
        classes = summary["per_port_class"][port]
        pure = "OK" if len(classes) == 1 else "MIXED!"
        print(f"  port {port}: {classes}  {pure}")


if __name__ == "__main__":
    main()
