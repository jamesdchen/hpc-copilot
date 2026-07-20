# BANKED RULING MEMO — WSL-multiplexing spike
*Designed 2026-07-20; not run (needs cluster creds the human holds). Falsifiable protocol inside.*

## Landed (current mechanics + settled doctrine)

- **Platform fork is a two-branch switch in `_ssh_multiplex_opts`** (`src/hpc_agent/infra/ssh_options.py:617-659`): win32 → named-pipe `ControlPath=\\.\pipe\openssh-hpc-cm-%C` (default since 0.11, opt-out `HPC_SSH_NAMED_PIPE=0`); POSIX → real Unix socket `ControlPath=$XDG_RUNTIME_DIR/hpc-cm-%C` + `ControlPersist=10m`. A process inside WSL reports `linux` and takes the POSIX branch **as-is** — the divergence point is `sys.platform`, nothing else in the seam.
- **The Windows failure class is pinned in code** (`ssh_options.py:322-340`): `getsockname failed: Not a socket` + rc ∈ `{127, 255}`. The 2026-07-20 comment records the pre-dispatch proof: native OpenSSH 9.5p2 exits rc=127 with the marker **even for a DNS-impossible host** — the bind failure precedes any network contact, so one re-run is safe. The marker is a win32compat-shim string that "never arises" on POSIX.
- **Retry gate + idempotent-leg guard** (`ssh_options.py:343-398`, wired in `infra/remote.py:873-912`): `run_with_named_pipe_retry` rebuilds argv per attempt so the fallback lands after `mark_named_pipe_broken()`; non-idempotent legs (qsub/sbatch) take the sticky verdict but **never** re-run (double-submit hazard).
- **Exit-status doctrine**: rc=255 is OpenSSH's reserved client-failure code; any other nonzero rc is remote-command content (`ssh_options.py:1057-1087`, `remote.py:903-912`).
- **Settled doctrine this spike plans around, never relitigates**: named-pipe ControlMaster on Windows is known-bad (USER STEER 2026-07-18 — never re-propose); cross-process SSH amortization is **daemon-only, R4** (`docs/plans/daemon-engineering-2026-07-16/MEASUREMENT.md:199` names the prize: "SSH-handshake amortization across verb invocations"); the connection-broker design is dead — `docs/design/connection-broker.md` survives only as the successor record ("no UNATTENDED cold-SSH dial may exist", 2026-07-07) and its "why not alternatives" (lines 168-174: no Unix-socket mux on native Windows; Git-Bash ssh is agent-blind).
- **Two WSL hazards already closed in code**: `/mnt/c/...` normalizes to the same `repo_hash` as the Windows form (`state/run_record.py:370-398`; `test_run_record_repo_hash.py:45-58`), and running hpc-agent code in WSL has precedent (release skill installs the wheel there: `release/SKILL.md:147`).

## The spike question

Does a Unix-domain-socket `ControlPath` inside WSL give durable, doctrine-compatible connection reuse — the transport a future R4 daemon would ride — without inheriting the Windows named-pipe failure class or the WSL-interop hazards we currently only assume?

## Protocol (human, <30 min, cluster creds required)

**E1 — Signature control (no creds needed).** Inside WSL (`wsl -d Ubuntu`):
```
ssh -o ControlMaster=auto -o ControlPath=/tmp/hpc-cm-%C -o ControlPersist=10m \
    -o BatchMode=yes -o ConnectTimeout=5 spike-nonexistent.invalid true; echo rc=$?
```
*Pass:* `rc=255`, stderr `Could not resolve hostname`, **no** `getsockname` marker, and nothing in {127, 255}+marker — i.e. the `run_with_named_pipe_retry` gate provably cannot fire on the POSIX path. *Fail (closes question):* any `getsockname failed: Not a socket` — WSL ssh shares the failure class.

**E2 — Real reuse against the cluster login node.**
```
SOCK=$XDG_RUNTIME_DIR/hpc-cm-spike   # ext4, NOT /mnt/c
time ssh -o ControlMaster=auto -o ControlPath=$SOCK -o ControlPersist=10m USER@CLUSTER true   # cold
ssh -O check -o ControlPath=$SOCK USER@CLUSTER                                                 # expect "Master running (pid=…)"
time ssh -o ControlMaster=auto -o ControlPath=$SOCK USER@CLUSTER true                          # warm
ssh -O exit -o ControlPath=$SOCK USER@CLUSTER
```
*Pass:* `-O check` reports the master; warm time < ½ cold (typically ~0.1s vs 1-2s). Repeat cold/warm 3×.

**E3 — Hazards to MEASURE, not assume.**
1. *Socket filesystem:* repeat E2 cold step with `SOCK=/mnt/c/tmp/hpc-cm-spike` (DrvFs/9p). Record exact rc+stderr. Expectation to confirm: AF_UNIX sockets unsupported on DrvFs → master-bind fails — **ControlPath must live on ext4**.
2. *Agent boundary:* `ssh-add -l` in WSL (expect "Could not open a connection" — the Windows named-pipe agent is invisible). If `npiperelay` is installed, measure: `SSH_AUTH_SOCK` via relay, `ssh-add -l`, then E2 warm with agent auth; record reliability over 10 connects.
3. *VM idle-down:* leave the E2 master open, run no other WSL process, wait 20 min, then `ssh -O check` + `wsl.exe -l -v` (VM state). Expectation: the daemon-held ssh process keeps the WSL2 VM above `vmIdleTimeout` (15m); record whether the master survives. Then `wsl --terminate` and record the slave's failure signature (socket gone → `-O check` "No such file or directory").
4. *Caller path:* from **Windows**, connect to a WSL-side listener on localhost (WSL2 forwarded port) — the RPC channel needs no `\\wsl$` socket translation (the socket is daemon-internal; `repo_hash` already canonicalizes `/mnt/c` for the journal).

## Success / failure criteria

**Green (justifies a daemon spike):** E1 = clean rc=255/resolve signature over 3+ runs, zero markers; E2 `-O check` running and warm < ½ cold on 3/3; E3.1 ext4-works/DrvFs-fails cleanly with a recognizable rc; E3.3 master survives 20 min idle with a live holder process.
**Red (closes the question):** any `getsockname` marker from WSL ssh (shared failure class); warm reuse fails (master not actually shared); master dies on idle **despite** a live holder process (VM lifetime unmanageable); no agent-auth path works without key duplication the human rejects.
**Yellow:** green except agent relay is flaky → daemon spike proceeds keyed off a WSL-native agent; ruling question 3 decides.

## If green: what it enables + ruling questions

Enables: the R4 daemon (`daemon-engineering-2026-07-16/DESIGN.md`, `HPC_CLI_DAEMON=1` opt-in ladder) hosted **in WSL**, holding one ext4-socket mux master per host — cross-process handshake amortization without re-proposing named-pipe ControlMaster. The dead connection-broker's five invariants (one connection/host, breaker-gated reconnects, slot accounting, loud death, idle self-terminate) transfer verbatim; asyncssh reopens only if sftp-channel bulk pulls demand it. Fail-open unchanged: no WSL → byte-identical Windows path.

**Ruling questions:**
1. **Daemon host if green — (a) WSL-hosted with mux master, (b) Windows-native asyncssh (connection-broker's original pick), (c) both behind a transport seam?** *Recommend (a):* it reuses stock OpenSSH + the already-audited `ssh_argv` POSIX branch instead of a second ssh implementation; asyncssh stays the phase-2 sftp option.
2. **Caller↔daemon RPC — (a) WSL2 localhost TCP, (b) `wsl.exe` relay per call, (c) named pipe via npiperelay?** *Recommend (a):* E3.4 measures it; per-call `wsl.exe` spawns reintroduce the floor the daemon exists to delete.
3. **Key material — (a) npiperelay to the Windows agent (one store), (b) WSL-native agent (keys duplicated)?** *Recommend (a) if E3.2 reliability ≥ 9/10, else (b) with a one-time human key-add;* decide at daemon-design, not now.
4. **Boxes without WSL — confirm daemon stays opt-in accelerator, dead-daemon → byte-identical inline?** *Recommend yes* (matches R4's opt-in grant + guard-integrity law).
