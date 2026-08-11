#!/usr/bin/env bash
# traffic_gen/run_traffic.sh
#
# Launch the emulated LunaBridge uplink FROM the UERANSIM UE, so packets
# traverse gNB -> Open5GS UPF -> ogstun -> N6 NFQUEUE and are captured by
# n6_interceptor into bundles.jsonl (the trace the scheduler replays).
#
# This is the ONE environment-specific piece: how you enter the UE's network
# namespace depends on your deployment. The two common cases are handled and
# both are overridable by env var -- nothing here is hardcoded to a machine.
#
#   UE_MODE=container : UE runs in a docker container (default).
#                       Requires mgen (or iperf3) INSIDE that container.
#   UE_MODE=netns     : UE is a host process; enter its netns by name.
#
# Env vars (all overridable):
#   UE_MODE       container | netns          (default: container)
#   UE_CONTAINER  docker container name       (default: ueransim-ue)
#   UE_NETNS      netns name for UE_MODE=netns(default: ue1)
#   UE_IFACE      tunnel iface to source from (default: uesimtun0)
#   DST           Earth-side sink IP          (REQUIRED)
#   BASE_PORT     first UDP dest port         (default: 5000)
#   DURATION      run seconds                 (default: 600)
#   SENDER        mgen | iperf3               (default: mgen)
#   MGEN_SCRIPT   path to .mgn (auto-rendered if unset)
#
# Prereq (once per session, on the UPF side -- see n6_interceptor.py header):
#   docker exec upf iptables -I FORWARD -i ogstun -j NFQUEUE --queue-num 0
#   and run the interceptor in the UPF netns.
set -euo pipefail

: "${DST:?set DST to the Earth-side sink IP the UE routes to}"
UE_MODE="${UE_MODE:-container}"
UE_CONTAINER="${UE_CONTAINER:-ueransim-ue}"
UE_NETNS="${UE_NETNS:-ue1}"
UE_IFACE="${UE_IFACE:-uesimtun0}"
BASE_PORT="${BASE_PORT:-5000}"
DURATION="${DURATION:-600}"
SENDER="${SENDER:-mgen}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

# --- exec wrapper: run "$@" inside the UE's network namespace --------------
ue_exec() {
  case "$UE_MODE" in
    container) docker exec -i "$UE_CONTAINER" "$@" ;;
    netns)     sudo ip netns exec "$UE_NETNS" "$@" ;;
    *) echo "unknown UE_MODE=$UE_MODE (want container|netns)" >&2; exit 2 ;;
  esac
}

echo ">> UE_MODE=$UE_MODE iface=$UE_IFACE dst=$DST duration=${DURATION}s sender=$SENDER"

if [[ "$SENDER" == "mgen" ]]; then
  # Render the script on the HOST (needs python + traffic_gen), then feed it
  # to mgen inside the UE. Rendering here keeps the model as the single source
  # of truth even if the UE image has no python.
  MGEN_SCRIPT="${MGEN_SCRIPT:-$REPO_ROOT/scenarios/eva_dump.mgn}"
  if [[ ! -f "$MGEN_SCRIPT" ]]; then
    echo ">> rendering $MGEN_SCRIPT from traffic_model.py"
    ( cd "$REPO_ROOT" && python3 -m traffic_gen.gen_mgen \
        --dst "$DST" --base-port "$BASE_PORT" --duration "$DURATION" \
        --out "$MGEN_SCRIPT" )
  fi
  echo ">> starting mgen in UE (script: $MGEN_SCRIPT)"
  # -t caps the run; MGEN reads the script from stdin so we don't need it
  # copied into the container.
  ue_exec mgen input - <"$MGEN_SCRIPT"

elif [[ "$SENDER" == "iperf3" ]]; then
  # Fallback: no MGEN available. iperf3 gives CBR + --tos per flow but NOT
  # Poisson and NOT a single-process schedule, so we approximate: launch the
  # steady flows as backgrounded iperf3 clients with the right --tos, and the
  # bursty science/emergency flows as timed one-shots. Sizes/rates come from
  # the model so they stay consistent; only the scheduling fidelity drops.
  #
  # NOTE: requires an iperf3 server reachable at $DST (iperf3 -s on the sink).
  # --tos takes the full DS byte, same value as MGEN's TOS (DSCP<<2).
  echo ">> iperf3 fallback -- reduced scheduling fidelity (no Poisson)"
  # telemetry ~16 kbps, EF-less; AF31 tos=0x68
  ue_exec iperf3 -c "$DST" -u -b 16K -l 512  --tos 0x68 -t "$DURATION" -p "$((BASE_PORT+1))" &
  # emergency approx: low-rate CBR stand-in for Poisson; EF tos=0xb8
  ue_exec iperf3 -c "$DST" -u -b 4K  -l 128  --tos 0xb8 -t "$DURATION" -p "$((BASE_PORT+2))" &
  # science dump 1: 8 Mbps for 40 s starting at 60 s
  ( sleep 60;  ue_exec iperf3 -c "$DST" -u -b 8M -l 1400 --tos 0x28 -t 40 -p "$((BASE_PORT+3))" ) &
  # science dump 2: 8 Mbps for 40 s starting at 120 s
  ( sleep 120; ue_exec iperf3 -c "$DST" -u -b 8M -l 1400 --tos 0x28 -t 40 -p "$((BASE_PORT+4))" ) &
  # EVA media: 3 Mbps for 120 s starting at 110 s; AF41 tos=0x88
  ( sleep 110; ue_exec iperf3 -c "$DST" -u -b 3M -l 1400 --tos 0x88 -t 120 -p "$((BASE_PORT+5))" ) &
  wait
else
  echo "unknown SENDER=$SENDER (want mgen|iperf3)" >&2; exit 2
fi

echo ">> traffic run complete. Capture is in the interceptor's bundles.jsonl."
