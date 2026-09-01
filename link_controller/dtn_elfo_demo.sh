#!/usr/bin/env bash
# link_controller/dtn_elfo_demo.sh
#
# THE realistic slice: two REAL uD3TN (BPv7) nodes over the ELFO relay link,
# demonstrating DTN store-carry-forward across a blackout using SCHEDULED
# CONTACTS -- the operationally correct model for deep-space DTN (nodes know
# the orbit ahead of time; the router only forwards during a contact).
#
#   moon uD3TN ──[ veth: ELFO netem = owlt delay + 10 Mbps + blackout ]── relay uD3TN
#   dtn://moon.dtn/        (lunar-space 10.20.0.0/24)         dtn://relay.dtn/
#
# WHY scheduled contacts and not just "leave the contact up + netem blackout":
# if the uD3TN contact stays active and only netem drops packets, a bundle can
# ride a kernel TCP RETRANSMIT when the link returns -- that is TCP reliability,
# NOT DTN storage, and would make the claim dishonest. Here the contact truly
# ENDS during the gap: uD3TN tears the CLA down and must hold the bundle in its
# OWN storage until the NEXT scheduled contact. netem enforces the physics
# (delay/rate, and a dark link during the gap) in lockstep with the schedule.
#
#   contact 1 [t+C1S .. t+C1S+CDUR]   gap (blackout)   contact 2 [t+C2S .. ]
#   send A during contact 1           send B here      B flushes from storage
#
# Channel (owlt_s, rate_bps) comes from one real contact-plan window.
# Needs root (ip netns, tc) + the uD3TN binary and aap tools. Cleans up on exit.
#
# Env: UD3TN_BIN, CSV, ROW  (see defaults below)
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UD="${UD3TN_BIN:-/tmp/lunabridge_test/ud3tn/build/posix/ud3tn}"
ELFO="$HERE/elfo_link.sh"
CSV="${CSV:-$HERE/../gateway/lcrns_relay_contact_plan_1sv.csv}"; ROW="${ROW:-1}"

NS_M=elfo_moon; NS_R=elfo_relay; VE_M=veth_m; VE_R=veth_r
IP_M=10.20.0.1; IP_R=10.20.0.2; MTCP=4224; AAP=4242; AAP2=4243
OUT=/tmp/relay_dtn.out; MLOG=/tmp/moon_dtn.log; RLOG=/tmp/relay_dtn.log

# schedule (seconds, relative to t0): contact1, gap, contact2
C1S=2; CDUR=8; GAP=7; C2S=17; C2DUR=12

read_csv() { python3 - "$CSV" "$ROW" "$1" <<'PY'
import csv,sys
print(list(csv.DictReader(open(sys.argv[1])))[int(sys.argv[2])-1][sys.argv[3]])
PY
}
OWLT_S="$(read_csv owlt_s)"; RATE_BPS="$(read_csv rate_bps)"
RTT_MS="$(awk -v s="$OWLT_S" 'BEGIN{printf "%.0f", s*2*1000}')"

cleanup() {
  for ns in "$NS_M" "$NS_R"; do ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null || true; done
  sleep 0.3; ip netns del "$NS_M" 2>/dev/null || true; ip netns del "$NS_R" 2>/dev/null || true
}
trap cleanup EXIT
cleanup; rm -f "$OUT" "$MLOG" "$RLOG"

echo "=== ELFO channel from $(basename "$CSV") row $ROW: owlt=${OWLT_S}s (RTT ~${RTT_MS}ms) rate=${RATE_BPS}bps ==="
echo "=== schedule: contact1 [${C1S}..$((C1S+CDUR))]s  gap [$((C1S+CDUR))..${C2S}]s  contact2 [${C2S}..$((C2S+C2DUR))]s ==="

# --- ELFO-shaped veth across the two node namespaces -----------------------
ip netns add "$NS_M"; ip netns add "$NS_R"
ip link add "$VE_M" type veth peer name "$VE_R"
ip link set "$VE_M" netns "$NS_M"; ip link set "$VE_R" netns "$NS_R"
ip -n "$NS_M" addr add "$IP_M/24" dev "$VE_M"; ip -n "$NS_M" link set "$VE_M" up; ip -n "$NS_M" link set lo up
ip -n "$NS_R" addr add "$IP_R/24" dev "$VE_R"; ip -n "$NS_R" link set "$VE_R" up; ip -n "$NS_R" link set lo up
ip netns exec "$NS_M" bash "$ELFO" apply "$VE_M" "$OWLT_S" "$RATE_BPS" >/dev/null
ip netns exec "$NS_R" bash "$ELFO" apply "$VE_R" "$OWLT_S" "$RATE_BPS" >/dev/null
elfo() { ip netns exec "$1" bash "$ELFO" "$2" "$3" "${4:-}" "${5:-}" >/dev/null 2>&1 || true; }
blackout_link() { elfo "$NS_M" blackout "$VE_M"; elfo "$NS_R" blackout "$VE_R"; }
restore_link()  { elfo "$NS_M" restore "$VE_M" "$OWLT_S" "$RATE_BPS"; elfo "$NS_R" restore "$VE_R" "$OWLT_S" "$RATE_BPS"; }

# --- start the two uD3TN nodes ---------------------------------------------
ip netns exec "$NS_M" "$UD" -e dtn://moon.dtn/  -l 3600 -a 127.0.0.1 -p $AAP -A 127.0.0.1 -P $AAP2 -c "mtcp:$IP_M,$MTCP" -L 3 >"$MLOG" 2>&1 &
ip netns exec "$NS_R" "$UD" -e dtn://relay.dtn/ -l 3600 -a 127.0.0.1 -p $AAP -A 127.0.0.1 -P $AAP2 -c "mtcp:$IP_R,$MTCP" -L 3 >"$RLOG" 2>&1 &
sleep 2

# --- two SCHEDULED contacts moon -> relay (t0 = now) -----------------------
T0=$(date +%s.%N)
ip netns exec "$NS_M" aap-config --tcp 127.0.0.1 $AAP --schedule $C1S $CDUR  1200000 -r 'dtn://relay.dtn/' 'dtn://relay.dtn/' "mtcp:$IP_R:$MTCP" 2>&1 | grep -v DEPRECATED | tail -1 || true
ip netns exec "$NS_M" aap-config --tcp 127.0.0.1 $AAP --schedule $C2S $C2DUR 1200000 -r 'dtn://relay.dtn/' 'dtn://relay.dtn/' "mtcp:$IP_R:$MTCP" 2>&1 | grep -v DEPRECATED | tail -1 || true

ip netns exec "$NS_R" aap-receive --tcp 127.0.0.1 $AAP -a bundlesink -c 2 --newline >"$OUT" 2>&1 &
sleep 0.5

wait_until() { local target="$1" now; now=$(date +%s.%N); awk -v a="$T0" -v n="$now" -v t="$target" 'BEGIN{d=t-(n-a); if(d>0) system("sleep "d)}'; }
send() { ip netns exec "$NS_M" bash -c "printf '%s' '$1' | aap-send --tcp 127.0.0.1 $AAP -a '$2' 'dtn://relay.dtn/bundlesink'" 2>&1 | grep -v DEPRECATED | tail -1 || true; }
has()  { grep -aq "$1" "$OUT"; }

echo; echo "=== PHASE 1: CONTACT 1 -- send BUNDLE-A ==="
wait_until $((C1S+2)); send "BUNDLE-A-CONTACT1" srcA
wait_until $((C1S+5))
has "BUNDLE-A-CONTACT1" && echo "  [OK] A delivered during contact 1 (over the ${RTT_MS}ms link)" || echo "  [FAIL] A not received"

echo; echo "=== PHASE 2: GAP -- contact ends, link goes dark, send BUNDLE-B ==="
wait_until $((C1S+CDUR)); blackout_link      # contact1 just ended; kill the link too
wait_until $((C1S+CDUR+2)); send "BUNDLE-B-STORED" srcB
wait_until $((C1S+CDUR+4))
if has "BUNDLE-B-STORED"; then echo "  [UNEXPECTED] B arrived during the gap"; else
  echo "  [OK] B NOT delivered -- held in moon's uD3TN storage (no active contact)"; fi
ended=$(grep -c 'contact.*ended\|Contact.*ended' "$MLOG" 2>/dev/null || true)
echo "  moon log: scheduled-contact-ended events so far: ${ended:-0}"

echo; echo "=== PHASE 3: CONTACT 2 -- link restored, B flushes from storage ==="
wait_until $C2S; restore_link
for i in $(seq 1 15); do has "BUNDLE-B-STORED" && break; sleep 1; done
has "BUNDLE-B-STORED" && echo "  [OK] B delivered in contact 2 -- DTN store-carry-forward (not TCP retransmit)" || echo "  [FAIL] B never delivered"

echo; echo "=== evidence from moon log (2 separate mtcp connections = contact torn down & re-made) ==="
grep -nE 'Connected successfully|contact with .* (started|ended)|Contact' "$MLOG" | tail -8
echo; echo "=== relay received, in order ==="; grep -aE 'BUNDLE' "$OUT" || cat "$OUT"
echo "=== done ==="
