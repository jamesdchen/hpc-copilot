# Design: per-host SSH connection broker

Status: PROPOSED (2026-07-06). Not scheduled; the probe verdict cache
(`ops/preflight/probe_cache.py`) ships first and independently.

## Problem

Every ssh-family call (probe, staging, submit, poll, harvest) pays a full
cold TCP+SSH handshake: named-pipe ControlMaster multiplexing is broken on
native Windows OpenSSH (`_cluster_combined_probe` documents the empirical
case), so there is no connection reuse anywhere in the stack. On a healthy
login node that is ~1s per call; on a loaded one (hoffman2 evenings,
observed 31-60s+ across runs #7-#8) it is the dominant latency in every
funnel stage — and each retry/poll opens ANOTHER connection, which is
exactly the pattern the cluster intrusion filters count (the 2026-07-04
ban-hammer incident; `infra.ssh_circuit` exists because of it).

One handshake per session instead of per call attacks both problems at
once: latency AND ban exposure.

## Shape

A small broker daemon per host, owned by the journal home (cross-process,
like the breaker/throttle/slots state):

- **Library**: `asyncssh` (pure-python, agent-forwarding aware, channel
  multiplexing). One `SSHClientConnection` per host, commands run as
  channels — milliseconds each after the one-time handshake.
- **Process model**: a detached broker process per host, started on first
  demand by whichever caller needs it (the `wait-detached`/lease idiom:
  `<journal>/_broker/<host>.lease.json` + liveness pid check). Callers talk
  to it over a local named pipe / localhost socket with a tiny
  length-prefixed JSON protocol: `{cmd, stdin?, timeout_sec}` →
  `{rc, stdout, stderr}`.
- **Fallback is the current path**: broker absent/unresponsive/erroring →
  callers fall back to one-shot `ssh_run` exactly as today. The broker is
  an accelerator, never a correctness dependency (same fail-open posture as
  the breaker).

## Integration seams

- `infra/remote.ssh_run` grows a broker fast path: try the broker socket
  first (bounded ~250ms connect), else one-shot ssh. All existing callers
  inherit it; no call-site churn.
- `infra/transport` bulk transfers (rsync/tar/scp) keep their own
  connections initially — bulk data over the broker channel is phase 2
  (sftp subsystem), and transfers are rarer than command round-trips.

## Ban-safety invariants (must hold, enforcement-mapped when built)

1. The broker holds exactly ONE connection per host, with keepalives —
   strictly fewer connections than today, never more.
2. `ssh_circuit.check_circuit` gates broker RECONNECTS exactly like
   one-shot calls; a broker reconnect loop must not become the new
   all-night hammer. Broker connect failures feed
   `record_connection_failure`.
3. `ssh_slots` counts the broker's connection against the per-host cap.
4. Broker death mid-command surfaces as the command's failure (loud), and
   the tree-kill discipline (`run_capture_bounded`) applies to the broker
   process itself.
5. An idle broker self-terminates (default ~15 min) so a forgotten daemon
   doesn't hold a login-node session forever — clusters count those too.

## Why not alternatives

- **OpenSSH ControlMaster**: not supported by native Windows OpenSSH
  (no unix-socket/mux support); Git-Bash ssh supports it but is
  ssh-agent-blind (the known key-auth trap) — rejected.
- **paramiko**: workable but asyncssh's channel model and agent support
  are cleaner for the one-connection-many-channels shape.
- **Long-lived master `ssh -N` + `-O` forwarding**: same native-Windows
  mux gap.

## Expected win

Funnel stages issue 3-15 ssh round-trips each (probes, staging preclean,
qsub, per-poll status, harvest pulls). At 1s/handshake that is seconds;
at the loaded-hoffman2 30-60s it is MINUTES per stage. The broker
amortizes to one handshake per session; polls become ~RTT.

## Phasing

1. Broker daemon + `ssh_run` fast path + invariants 1-5 + guard tests.
2. `sftp` channel for transport pulls (metrics harvest is many small
   files — the worst rsync-per-file case).
3. Retire the per-call `ssh_slots` waits for brokered hosts (the cap is
   trivially satisfied at 1).
