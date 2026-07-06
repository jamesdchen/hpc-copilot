# Design: per-host SSH connection broker

Status: **PHASE 2 SHIPPED as the asyncssh engine** (2026-07-06,
`infra/ssh_engine.py`, opt-in via `HPC_SSH_ENGINE=asyncssh`), wired ahead of
phase 1 in the `ssh_run` seam. **Phase 1** (`infra/ssh_broker.py`, opt-in
`HPC_SSH_BROKER`) is now **DEPRECATED** — retained as the middle fallback rung
until the engine is live-validated, then retired. Phase 3 (SFTP transport pull)
remains PROPOSED. The probe verdict cache (`ops/preflight/probe_cache.py`) ships
alongside as an independent connection-count reduction.

## Phase 2 (shipped as the asyncssh engine)

The command channel is outsourced to a library — `asyncssh` (>=2.23) — rather
than the hand-rolled `ssh -T host /bin/sh` framing of phase 1. The decision and
what it does NOT change:

- **Decision.** The per-command round-trip runs over a held
  `asyncssh` connection (one `SSHClientConnection` per host, commands as
  channels). The native one-shot `ssh` path stays the **permanent hard
  fallback** — it is never removed. The phase-1 in-process channel enters
  **DEPRECATED** status and is retired only after the engine is validated live.
- **Transfers stay native.** rsync/tar bulk transfers keep the native binaries:
  the `tar | ssh` pipe and rsync's delta/`--delete` semantics are not replicable
  over a generic SSH channel, so they are out of scope. Migrating the metrics
  *pull* to the engine's SFTP subsystem is **phase 3**, tracked separately.

### Capability facts that gated the decision

- **Windows named-pipe agent support.** asyncssh reaches the Windows OpenSSH
  ssh-agent over its named pipe — the same agent the native binary uses — so
  key auth works on this box without a Git-Bash-style agent-blind trap. The
  gating fix is [asyncssh#795], hence the **`asyncssh>=2.23` pin**.
- **Typed exception taxonomy.** The engine path replaces the three
  stderr-marker string classifiers (throttle / connection-closed / auth) with
  asyncssh's typed exceptions, funnelled through `classify_engine_failure`. The
  native path keeps the marker classifiers (it still shells `ssh`).
- **ControlMaster is moot.** The phase-1 note that native-Windows OpenSSH can't
  multiplex (no `ControlMaster` socket) no longer bites: the held asyncssh
  connection *is* the multiplexer, so OpenSSH mux config being ignored is
  irrelevant on the engine path.

### Ban-safety invariants (engine path)

The engine inherits phase 1's fail-open posture; the worst case still equals
today's one-shot path. The mapping:

1. **Breaker-gated connect.** The engine's connection open is gated by
   `ssh_circuit.check_circuit` exactly like a one-shot call — an open circuit
   refuses to open it; connect failures feed `record_connection_failure`.
2. **Slot-counted.** The held connection counts against the per-host
   `ssh_slots` cap — never more than one connection per host.
3. **Idle-close.** An idle engine connection self-closes so no login-node
   session lingers (clusters count those too).
4. **Hard fallback.** Any engine trouble raises `EngineUnavailable`; `ssh_run`
   falls through to the phase-1 broker check, then the one-shot path. An
   opt-in engine can never regress the ban-sensitive default — the enforcement
   map binds this ("any engine failure falls back to one-shot; engine is never
   load-bearing").

[asyncssh#795]: https://github.com/ronf/asyncssh/issues/795

## What phase 1 actually built (and why it differs from the sketch below)

Phase 1 is **in-process and dependency-free**, deliberately NOT the asyncssh
design sketched under "Shape". Two reasons drove the change:

* **No new dependency.** This project ships "without paramiko or other
  dependencies" (`infra.remote` docstring). asyncssh pulls `cryptography` +
  native wheels and must be installed on every client env. The persistent
  `ssh -T <host> /bin/sh` channel reuses the native `ssh` binary — which
  already reaches the Windows named-pipe ssh-agent — so there is nothing new
  to install and no agent-auth integration risk.
* **Ban-safety over reach.** A connection-layer change must never make the
  ban risk WORSE. Phase 1 is opt-in (`HPC_SSH_BROKER`, default OFF) with a
  hard fallback: any broker trouble raises `BrokerUnavailable` and
  `ssh_run` uses the unchanged one-shot path. So the worst case equals
  today. It also collapses the DOMINANT case already — a single detached
  poll/harvest worker firing repeated round-trips at one host — since those
  all originate in one process.

Framing: each command is bracketed by a per-command random-nonce sentinel on
BOTH stdout and stderr (drained by two reader threads — Windows can't
`select` on pipes), so streams stay separate (the throttle/error classifiers
need stderr) and the real remote exit code rides the stdout sentinel. The
persistent connection's handshake is a real breaker-gated ssh attempt; an
idle channel self-closes after `IDLE_CLOSE_SEC`.

Phase 2 (a cross-process daemon shared by CLI / detached workers / MCP
server, likely then justifying asyncssh's channel model) is still the
sketch below.

---

## (Original proposal — phase 2/3 reference)

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
