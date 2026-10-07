# Live srsRAN ZMQ testbed — recovery runbook & lunar-channel deployment

This documents the exact, verified procedure to bring the live srsRAN + Open5GS +
jrtc (JBPF) ZMQ testbed back up on the `ran` namespace (k3s: control-plane
`nuwins-server1`, agent `nuwinsrack2`), and to run the lunar channel **in the
loop** via `grc_lunar_standalone.py`. It also records the one blocker that is
currently open, so the next session does not have to re-derive the topology.

## Topology (must hold — getting this wrong is the #1 failure mode)

| Component            | Node (nodeSelector)        | Why it is pinned there                              |
|----------------------|----------------------------|----------------------------------------------------|
| `jrtc-0`             | `nuwins-server1`           | jrtc controller (JBPF IPC primary `jrt_controller`)|
| `srs-gnb-du1-0`      | `jrtc-role: ran` (server1) | mounts `/tmp/jbpf` **hostPath** → must share jrtc's node |
| `srs-grc-du1-0` (broker) | `jrtc-role: ue` (rack2) | GRC ZMQ hub                                        |
| `srs-ue{1,2}-du1-0`  | `jrtc-role: ue` (rack2)    | UEs                                                 |

**The gNB↔broker ZMQ is cross-node by design** (gNB on server1, broker on
rack2) and that is fine — it carried the 81,662-bundle capture. Do **not** pin
the gNB to `jrtc-role: ue`: that co-locates it with the broker but breaks its
JBPF IPC to `jrt-controller` (`Error connecting to primary (path=/tmp/jbpf)`),
and the `gnb` container exits 255. The correct pin is `jrtc-role: ran`.

## ZMQ wiring (srsRAN lockstep, sequential baseband)

```
gNB  :2000 (rep, DL)  <--- req  broker gnb_dl_source
gNB  (req, UL)        ---> :2001 (rep)  broker gnb_ul_sink
broker :2100/2200 (rep, DL) <--- req  UE rx_port
broker (req, UL)      ---> UE :2101/2201 (rep)  UE tx_port
broker UL = add_vcc(UE1_ul + UE2_ul)  -> served to gNB on :2001
```

Every link is REQ/REP in strict lockstep: **no sample moves unless all of
gNB, broker, and every configured UE are connected and clocking.** The UE's RF
device produces UL (zeros) as soon as it opens — before attach — so a connected-
but-not-yet-attached UE is enough to clock the loop.

## Trigger protocol (Helm `values.yaml` → `.zmq.triggerFiles`)

- hostPath on the node: `/var/tmp/zmq-triggers`; pod mountPath: `/zmq-triggers`.
- Files (exact names): `zmq-trigger-grc`, `zmq-trigger-connections`,
  `zmq-trigger-traffic`.
- Order: touch `zmq-trigger-grc` (broker starts flowgraph) → wait for broker log
  `Starting flowgraph` → touch `zmq-trigger-connections` (UEs open ZMQ + attach)
  → wait for UE `PDU Session Establishment successful` → touch
  `zmq-trigger-traffic`.
- `kubectl exec srs-grc-du1-0 -c grc -- touch /zmq-triggers/<file>` works (the
  hostPath is shared by all rack2 pods); the canonical script does `sudo touch`
  on the host at `/var/tmp/zmq-triggers`.

## Recovery sequence (verified healthy up to the open blocker)

```bash
NS=ran
K(){ sudo k3s kubectl "$@"; }
# 1. gNB must be on the jrtc node
K -n $NS patch sts srs-gnb-du1 --type=merge \
  -p '{"spec":{"template":{"spec":{"nodeSelector":{"jrtc-role":"ran"}}}}}'
# 2. clear triggers, restart gNB FIRST, then broker + UEs
K -n $NS exec srs-grc-du1-0 -c grc -- rm -f /zmq-triggers/* || true
K -n $NS delete pod srs-gnb-du1-0 --wait=false
# wait until gNB log shows:  "N2: Connection to AMF ... completed" and "gNB started"
K -n $NS delete pod srs-grc-du1-0 srs-ue1-du1-0 srs-ue2-du1-0 --wait=false
# wait grc Running ("Starting flowgraph") and UEs Running
# 3. fire triggers in order (see protocol above)
```

Health checks that should all pass (they did on 2026-10-07):
- gNB: `N2: Connection to AMF ... completed`, `gNB started`, JBPF `Started LCM IPC server`.
- Service endpoint fresh: `kubectl -n ran get endpoints srs-gnb-du1-zmq` == gNB podIP.
- Links ESTABLISHED: broker→gNB:2000 reachable; gNB→broker:2001 `ESTAB` (check
  `/proc/net/tcp` on the broker for `:07D1` in state `01`).

## OPEN BLOCKER (as of 2026-10-07)

With **all** of the above verified healthy — correct node placement, JBPF IPC up,
AMF connected, fresh endpoints, both cross-node ZMQ links ESTABLISHED, correct
trigger order, and even a **single-UE** config to remove the UL-adder dependency —
the UE still hangs at `Attaching UE...`, the gNB logs **no PRACH/RNTI**, and the
UEs eventually time out and CrashLoopBackOff (srsUE exits 1 on RF ZMQ timeout).

This reproduces with the **stock** `GRC_run.sh` broker, so it is **not** a
`grc_lunar_standalone.py` bug — it is a sample-flow stall in the live ZMQ radio
loop that appeared after many restart cycles (the same stack previously carried
the 81,662-bundle capture that is in the paper). Connections are up but DL IQ is
not clocking end-to-end.

Hypotheses not yet eliminated (next session):
1. `gr::vmcircbuf ... createfilemapping is not available` with only **64 MB**
   `/dev/shm` on the broker pod — try raising the broker's `/dev/shm` (emptyDir
   `medium: Memory` sizeLimit) and re-test; large DL buffers may be failing to
   allocate.
2. srsUE RF ZMQ `zmq_timeout` too tight for the cross-node round trip under load
   — the flowgraph connects but a slow slot trips the UE's RF timeout before
   first SSB decode. Try a larger timeout / `slowdown`.
3. A stale AMF/core (Open5GS docker `jrtc_open5gs`) session state — full core
   bounce before the RAN restart.

## Running the lunar channel in the loop (once the stack attaches)

`grc_lunar_standalone.py` is a drop-in broker that copies the stock flowgraph
**verbatim** and swaps each UE downlink `multiply_const` for a GNU Radio
`channels.channel_model` (path-loss tap + AWGN noise floor), so attenuation
produces a real SNR the UE AGC cannot undo (graceful MCS degradation; outage in
deep shadow). It is validated to build and to accept `set_taps` /
`set_noise_voltage` in the deployed GRC image.

Deploy (configmap `grc-lunar` already holds the three files):
```bash
# broker args -> /app/grc_lunar_std_run.sh ; mount grc_lunar_standalone.py,
# grc_lunar_std_run.sh, ridge.pathloss.json from configmap grc-lunar.
# Bring up with noise-voltage 0.0 first (pure passthrough == stock) to confirm
# attach, THEN calibrate the floor with --probe (noise_voltage = sig_rms *
# 10^(-SNR_dB/20)) and drive terrain with --pathloss-trace ridge.pathloss.json.
```
The `ridge.pathloss.json` trace comes from `lunaremu_to_pathloss.py` over the
real Connecting Ridge LOLA 5 m/px DEM (same channel characterized in
`analysis/fig_channel.py` / paper Fig. 2).
