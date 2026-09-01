# link_controller — ELFO relay-link emulator + live DTN slice

Makes the DTN relay hop **real**: emulates the surface↔ELFO-satellite link as an
actual network interface with `tc netem` (propagation delay, 10 Mbps rate,
blackout), and runs two real **µD3TN** (BPv7) nodes across it to demonstrate
**store-carry-forward** over a blackout — the live (Option A) counterpart to the
offline scheduler DES.

All channel numbers come from the real contact plan
`gateway/lcrns_relay_contact_plan_1sv.csv` (`owlt_s`, `rate_bps`).

## Files

- `elfo_link.sh` — the channel emulator (`tc netem`): `apply/blackout/restore/clear/show`.
  Delay = `owlt_s` (~58 ms one-way → ~116 ms RTT applied both directions),
  rate = `rate_bps` (10 Mbps), blackout = 100% loss. **Never compresses the
  delay** — only a driver compresses the contact/gap schedule (real delay vs
  compressed time, as discussed).
- `elfo_demo.sh` — pure-channel proof: veth across two netns, one compressed
  contact→blackout→contact cycle, measures RTT (ping) and rate (iperf3). No DTN.
- `dtn_elfo_demo.sh` — **the realistic slice**: two µD3TN nodes over the ELFO
  veth with **scheduled contacts**, proving DTN store-carry-forward.

## Measured results (nuwinsrack2)

`elfo_demo.sh` (channel only):

| phase | metric | expected | measured |
|-------|--------|----------|----------|
| contact | RTT | ~116 ms (2×owlt) | **116.3 ms** min |
| contact | throughput | ~10 Mbps | **9.56 Mbps** (TCP, minus overhead) |
| blackout | ping | 100% loss | **100% loss** |
| restored | RTT | ~116 ms | **116.30 ms** (mdev 0.003) |

`dtn_elfo_demo.sh` (µD3TN store-carry-forward): bundle **A** delivered in
contact 1; bundle **B** injected during the gap is **held in moon's µD3TN
storage** (not delivered); **B flushes in contact 2**. Proven honest (not a TCP
retransmit) by the moon log showing the contact/CLA **torn down and re-made** —
two separate `MTCP: Connected successfully` with a `Scheduled contact ... ended`
between them.

### Why scheduled contacts (not "leave the contact up + netem blackout")

If the µD3TN contact stays active and only netem drops packets, a bundle can
ride a **kernel TCP retransmit** when the link returns — that is TCP reliability,
not DTN storage. `dtn_elfo_demo.sh` ends the contact during the gap so µD3TN
must hold the bundle in its own storage until the next scheduled contact. This
is also the operationally correct model for deep-space DTN (contacts known from
the orbit ahead of time).

## Where this maps in the stack

The veth `moon ↔ relay` is the `lunar-space` (10.20.0.0/24) network of
`docs/architecture.md`. A second netem on `deep-space` (10.30.0.0/24) would add
the ~1.28 s Earth-Moon leg to `dtn://earth.dsn/`. The µD3TN buffer here is the
"memory" the LunaBridge scheduler reasons about — real, not simulated.

## Reproduce (Ubuntu 20.04 testbed)

Host packages: `mgen` (traffic_gen), `libjansson-dev` + `libsqlite3-dev`
(µD3TN build), `iperf3`, `tcpdump`, `iproute2`.

```bash
# build µD3TN from source (docker.io registry was blocked; gitlab reachable)
git clone --recurse-submodules --depth 1 https://gitlab.com/d3tn/ud3tn.git
cd ud3tn && make posix                       # -> build/posix/ud3tn
pip3 install ./pyd3tn ./python-ud3tn-utils   # aap-config / aap-send / aap-receive

# run (needs root for ip netns + tc)
sudo UD3TN_BIN=/path/to/ud3tn/build/posix/ud3tn PATH="$HOME/.local/bin:$PATH" \
    bash link_controller/dtn_elfo_demo.sh
```

## Not covered yet (next increments)

- Multi-bundle backlog + a **bounded** µD3TN storage to exercise overflow
  (connect the DES's `max_queue_bytes` sizing to a real buffer cap).
- Feeding the **real N6 trace** (traffic_gen → bundles.jsonl) into moon's µD3TN
  instead of hand-injected A/B bundles.
- The `deep-space` second hop (~1.28 s) to a third `earth` node.
