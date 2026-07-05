---
name: net-triage
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent net-triage [--spec <path>]
  python: hpc_agent.ops.recover.net_triage.net_triage
---
## Purpose

The mechanized connectivity differential: **WHY can't I reach the cluster?**
Run it before concluding any network cause — never diagnose with improvised
ssh probes. The 2026-07-05 incident it exists for: a host's SSH circuit
breaker was OPEN (with its cooldown deadline durably recorded) and the driving
agent, lacking a tool that answered "why", probed with raw ssh, saw timeouts,
and mis-diagnosed a local VPN outage. For each configured cluster host (plus
an optional caller host) this verb gathers, deterministically and bounded: the
circuit-breaker state (read-only), one shared control-plane HTTPS probe,
bounded DNS resolution, and ONE bounded TCP connect to `host:22` — then
returns a verdict per host with remediation text.

## Inputs

A `NetTriageSpec` JSON spec, all fields optional (`hpc-agent net-triage` with
no spec triages every configured cluster host):

- `host` (str, optional) — an EXTRA host to triage in addition to the
  cluster-config hosts; `user@host` is normalized to the bare host.
- `control_timeout_sec` (float, default `5`, 1–30) — budget for the one
  control-plane HTTPS check.
- `dns_timeout_sec` (float, default `5`, 1–30) — per-host DNS budget.
- `tcp_timeout_sec` (float, default `8`, 1–60) — per-host budget for the
  single TCP connect to port 22.

## Outputs

A `NetTriageResult`: `now`, the shared `control` check
(`{https_ok, url, detail}`), `all_reachable`, a one-line `summary`, and one
`HostTriage` per host — `{host, cluster, breaker, dns_ok/dns_detail,
tcp_ok/tcp_detail, verdict, remediation}`. `verdict` is one of:

- `reachable` — `host:22` accepts TCP; if ssh still fails, suspect auth/config,
  not the network.
- `breaker_open_cooling` — the SSH circuit breaker is OPEN: ssh fails fast BY
  DESIGN (ban-risk protection). Wait until `breaker.cooldown_until` for the
  automatic half-open probe, or override per host with
  `HPC_SSH_CIRCUIT_OVERRIDE=<host>` if you know why the failures happened.
- `host_unreachable_network_ok` — the control probe passed but `host:22` did
  not connect: a cluster-side outage or a source-IP filter/ban at their
  border (a traceroute stalling at the cluster's edge discriminates the two).
  Do NOT retry-storm; verify out-of-band.
- `local_network_down` — the control probe failed: THIS machine's network/VPN
  is down; every cluster looks dark. Fix local connectivity first.
- `dns_failure` — the hostname never resolved (note: an OpenSSH *alias* never
  resolves by itself — its HostName lives in ssh config).

Verdict precedence is fixed: direct evidence (`reachable`) outranks
everything; a failed control probe outranks host-side conclusions; an open
breaker outranks DNS/TCP inference.

## Errors

- `spec_invalid` — malformed spec (out-of-range timeout); enforced at the wire
  boundary.

## Idempotency

Idempotent — pure diagnosis. It reads breaker state files and opens bounded,
ephemeral probe connections; it writes nothing and never mutates breaker
state. Re-running is always safe.

## Notes

- **Breaker-respecting by construction:** while a host's circuit is open the
  TCP probe is SKIPPED (`tcp_ok: null` with the reason) — the breaker's single
  half-open probe slot is claimed by `ssh_circuit.check_circuit` under the
  state-file lock, and triage never touches it, races it, or adds a connection
  the cluster's intrusion filter would count.
- Fail-open on local state: a missing/corrupt breaker state file reads as
  healthy (`breaker.state: "missing"`); an unloadable clusters.yaml just means
  no configured hosts (supply `host` in the spec).
- The per-host "ssh circuit OPEN" one-liners this module derives also surface
  on `doctor` (`open_ssh_circuits` + the NEEDS ATTENTION summary) and in the
  `status-snapshot` brief, so a breaker-dark host is visible on the surfaces
  an agent already reads.
