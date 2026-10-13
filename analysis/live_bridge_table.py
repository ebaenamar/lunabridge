"""
analysis/live_bridge_table.py

Table of the live N6 -> BPv7 bridge runs (link_controller/run_bridge_class.sh):
one run per class, the UE's PDU session provisioned with the class's 5QI, the
class read by the bridge from TEID -> 5QI (NGAP), bundles carried by uD3TN over
the compressed LCRNS slice. Reads runs/live_5qi/<class>/results.json and writes
LaTeX rows: class & 5QI & L (s) & rho & bundles & measured & closed form.

Usage: python3 analysis/live_bridge_table.py [--runs runs/live_5qi] [--out rows.tex]
"""
import argparse, json, os

ORDER = ["emergency", "telemetry", "sci_bulk", "media"]
NAME = {"emergency": r"\textsc{Emergency}", "telemetry": r"\textsc{Telemetry}",
        "sci_bulk": r"\textsc{Science}", "media": r"\textsc{Media}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs/live_5qi")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    lines = []
    for cls in ORDER:
        p = os.path.join(a.runs, cls, "results.json")
        if not os.path.exists(p):
            print(f"missing {p}"); continue
        r = [x for x in json.load(open(p)) if x["cls"] == cls][0]
        sched = json.load(open(os.path.join(a.runs, cls, "schedule.json")))
        W = sched["windows_rel"]
        gmax = max(W[j + 1][0] - W[j][1] for j in range(len(W) - 1))
        print(f"{cls:10s} 5QI {r['fiveqi']} QFI {r['qfi']} L {r['L']} s (G_max {gmax} s) "
              f"rho {r['rho']:.2f} sent {r['sent']} delivered {r['delivered']} "
              f"measured {r['measured']:.4f} per-bundle {r['per_bundle']:.4f} "
              f"closed {r['closed_form']:.4f}")
        n = f"{r['sent']:,}".replace(",", "{,}")
        lines.append(f"{NAME[cls]} & {r['fiveqi']} & {r['L']} & {r['rho']:.2f} & {n} & "
                     f"{r['measured']:.3f} & {r['closed_form']:.3f} \\\\")
    if a.out:
        open(a.out, "w").write("\n".join(lines) + "\n")
        print("wrote", a.out)


if __name__ == "__main__":
    main()
