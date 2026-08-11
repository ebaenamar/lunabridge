#!/usr/bin/env bash
# link_controller/elfo_demo.sh
#
# Self-contained, measurable proof that the ELFO relay link can be emulated as
# a real interface with netem: builds a veth pair across two network
# namespaces (moon <-> relay, on the lunar-space 10.20.0.0/24 subnet), applies
# the ELFO channel from the REAL contact plan, and runs one compressed
# contact -> blackout -> contact cycle while measuring RTT and throughput.
#
# This uses plain ping/iperf3 as the load. It proves the CHANNEL behaves like
# ELFO (delay, rate, blackout). It does NOT yet show DTN store-carry-forward
# (that is uD3TN's job, layered on next): here, traffic during blackout is
# simply lost, which is exactly why a real deployment needs the gateway buffer
# + uD3TN on top of this link.
#
# Needs root (ip netns, tc). Cleans up its namespaces on exit.
#
# Env:
#   CSV       path to lcrns_relay_contact_plan_1sv.csv (default: ../gateway/...)
#   ROW       1-based data row of the window to emulate (default: 1)
#   COMPRESS  schedule compression factor (default: 10000; delay is NEVER
#             compressed, only the contact/gap durations)
#   CONTACT_CAP / GAP_CAP  max seconds for each phase after compression
#                          (default 12 / 6) so the demo stays short.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSV="${CSV:-$HERE/../gateway/lcrns_relay_contact_plan_1sv.csv}"
ROW="${ROW:-1}"
COMPRESS="${COMPRESS:-10000}"
CONTACT_CAP="${CONTACT_CAP:-12}"
GAP_CAP="${GAP_CAP:-6}"

NS_M=elfo_moon; NS_R=elfo_relay
VE_M=veth_m; VE_R=veth_r
IP_M=10.20.0.1; IP_R=10.20.0.2
ELFO="$HERE/elfo_link.sh"

cleanup() {
  ip netns pids "$NS_R" 2>/dev/null | xargs -r kill 2>/dev/null || true
  ip netns del "$NS_M" 2>/dev/null || true
  ip netns del "$NS_R" 2>/dev/null || true
}
trap cleanup EXIT

# --- read the window from the real contact plan --------------------------
read_csv_field() { # $1 = column name
  python3 - "$CSV" "$ROW" "$1" <<'PY'
import csv, sys
path, row, col = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(path) as f:
    rows = list(csv.DictReader(f))
print(rows[row-1][col])
PY
}
OWLT_S="$(read_csv_field owlt_s)"
RATE_BPS="$(read_csv_field rate_bps)"
DURATION_S="$(read_csv_field duration_s)"
GAP_S="$(read_csv_field gap_before_s)"

contact_s=$(awk -v d="$DURATION_S" -v c="$COMPRESS" -v cap="$CONTACT_CAP" 'BEGIN{v=d/c; print (v>cap)?cap:(v<2?2:v)}')
gap_s=$(awk -v g="$GAP_S" -v c="$COMPRESS" -v cap="$GAP_CAP" 'BEGIN{v=g/c; print (v>cap)?cap:(v<2?2:v)}')
rtt_ms=$(awk -v s="$OWLT_S" 'BEGIN{printf "%.1f", s*2*1000}')

echo "=== ELFO window (row $ROW of $(basename "$CSV")) ==="
echo "  owlt=${OWLT_S}s -> RTT target ~${rtt_ms} ms | rate=${RATE_BPS} bps | "
echo "  real contact=${DURATION_S}s gap=${GAP_S}s -> compressed x${COMPRESS} -> contact=${contact_s}s gap=${gap_s}s"

# --- build the veth link across two namespaces ---------------------------
cleanup
ip netns add "$NS_M"; ip netns add "$NS_R"
ip link add "$VE_M" type veth peer name "$VE_R"
ip link set "$VE_M" netns "$NS_M"; ip link set "$VE_R" netns "$NS_R"
ip -n "$NS_M" addr add "$IP_M/24" dev "$VE_M"; ip -n "$NS_M" link set "$VE_M" up; ip -n "$NS_M" link set lo up
ip -n "$NS_R" addr add "$IP_R/24" dev "$VE_R"; ip -n "$NS_R" link set "$VE_R" up; ip -n "$NS_R" link set lo up

# apply ELFO channel to BOTH egress directions -> symmetric delay+rate
ip netns exec "$NS_M" bash "$ELFO" apply "$VE_M" "$OWLT_S" "$RATE_BPS"
ip netns exec "$NS_R" bash "$ELFO" apply "$VE_R" "$OWLT_S" "$RATE_BPS"

echo; echo "=== PHASE 1: CONTACT -- measure RTT and throughput ==="
echo "-- ping ($IP_M -> $IP_R), expect avg ~${rtt_ms} ms --"
ip netns exec "$NS_M" ping -c 5 -q "$IP_R" | tail -2
echo "-- iperf3 TCP over the shaped link (expect ~$(awk -v b=$RATE_BPS 'BEGIN{printf "%.1f", b/1e6}') Mbit/s) --"
ip netns exec "$NS_R" iperf3 -s -1 -D >/dev/null 2>&1; sleep 0.5
ip netns exec "$NS_M" iperf3 -c "$IP_R" -t 8 -O 1 2>/dev/null | grep -E 'sender|receiver' || echo "  (iperf3 unavailable)"

echo; echo "=== PHASE 2: BLACKOUT for ${gap_s}s (satellite below horizon) ==="
ip netns exec "$NS_M" bash "$ELFO" blackout "$VE_M"
ip netns exec "$NS_R" bash "$ELFO" blackout "$VE_R"
echo "-- ping during blackout, expect 100% loss --"
ip netns exec "$NS_M" ping -c 4 -W 1 -q "$IP_R" | tail -2 || true
sleep "$gap_s"

echo; echo "=== PHASE 3: CONTACT RESTORED -- link should recover ==="
ip netns exec "$NS_M" bash "$ELFO" restore "$VE_M" "$OWLT_S" "$RATE_BPS"
ip netns exec "$NS_R" bash "$ELFO" restore "$VE_R" "$OWLT_S" "$RATE_BPS"
ip netns exec "$NS_M" ping -c 4 -q "$IP_R" | tail -2

echo; echo "=== done (namespaces cleaned up on exit) ==="
