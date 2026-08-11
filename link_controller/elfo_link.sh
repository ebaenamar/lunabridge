#!/usr/bin/env bash
# link_controller/elfo_link.sh
#
# ELFO relay-link emulator: applies the physical characteristics of the
# surface<->ELFO-satellite contact to a real network interface using Linux
# `tc netem`, and toggles blackout on command. This is the "channel physics"
# half of the online (Option A) path -- the LunaBridge scheduler is the
# "policy" half that decides WHAT to send; this decides HOW the link behaves.
#
# What it emulates, and where each number comes from (gateway CSV
# lcrns_relay_contact_plan_1sv.csv columns):
#   - one-way propagation delay = owlt_s  (~0.058 s == ~58 ms; ~116 ms RTT
#     when applied to both directions). This is the surface<->relay-satellite
#     range light-time, NOT the 1.28 s Earth-Moon leg (that is a SECOND netem
#     on the deep-space network, out of scope here).
#   - link rate = rate_bps (10 Mbps) via netem's own `rate` option.
#   - blackout = 100% loss (link dark: the satellite is below the horizon /
#     outside the contact window). Restore returns to delay+rate.
#
# DELIBERATE: the delay is REAL wall-clock (58 ms stays 58 ms) even when the
# CONTACT SCHEDULE is time-compressed by a driver. Compressing the delay too
# would distort DTN round-trip dynamics -- see README "real delay vs
# compressed time". This script never compresses delay; only a driver
# compresses the up/down schedule.
#
# Needs root (tc). Operates on ONE interface; apply to both veth ends (or both
# container veths on the lunar-space docker network) for a symmetric link.
#
# Usage:
#   elfo_link.sh apply   <dev> <owlt_s> <rate_bps>
#   elfo_link.sh blackout <dev>
#   elfo_link.sh restore <dev> <owlt_s> <rate_bps>   # alias of apply
#   elfo_link.sh clear   <dev>
#   elfo_link.sh show    <dev>
set -euo pipefail

cmd="${1:-}"; dev="${2:-}"

need_dev() { [ -n "$dev" ] || { echo "need <dev>" >&2; exit 2; }; }

# owlt_s (float seconds) -> integer microseconds for tc precision.
owlt_to_us() { awk -v s="$1" 'BEGIN{printf "%d", s*1000000 + 0.5}'; }
# rate_bps -> tc-friendly "Nmbit" string (keeps one decimal).
bps_to_mbit() { awk -v b="$1" 'BEGIN{printf "%.3gmbit", b/1000000}'; }

apply() {
  need_dev
  local owlt_s="${3:?need owlt_s}" rate_bps="${4:?need rate_bps}"
  local us mbit; us="$(owlt_to_us "$owlt_s")"; mbit="$(bps_to_mbit "$rate_bps")"
  # `replace` is idempotent: creates if absent, updates if present.
  tc qdisc replace dev "$dev" root netem delay "${us}us" rate "$mbit"
  echo ">> $dev: CONTACT  delay=${us}us (${owlt_s}s one-way)  rate=$mbit"
}

blackout() {
  need_dev
  tc qdisc replace dev "$dev" root netem loss 100%
  echo ">> $dev: BLACKOUT (100% loss)"
}

clear_q() { need_dev; tc qdisc del dev "$dev" root 2>/dev/null || true; echo ">> $dev: cleared"; }
show()    { need_dev; tc qdisc show dev "$dev"; }

case "$cmd" in
  apply|restore) apply "$@" ;;
  blackout)      blackout ;;
  clear)         clear_q ;;
  show)          show ;;
  *) echo "usage: $0 {apply|restore <dev> <owlt_s> <rate_bps>|blackout <dev>|clear <dev>|show <dev>}" >&2; exit 2 ;;
esac
