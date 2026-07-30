---
name: cluster-readiness
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent cluster-readiness --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.cluster_readiness_op.cluster_readiness
---
# cluster-readiness

The **standing per-cluster readiness ledger, rendered with the age of every
verdict**. Pillar 1 of [`docs/design/s2-readiness.md`](../design/s2-readiness.md).

The principle it serves: **S2 never discovers anything at fire time.** S2 is
where local intent becomes remote reality — transport, remote env, storage,
scheduler and harness permissions must all work — and the failure mode it
replaces is learning that by attempting the whole operation serially and
diagnosing it from worker-log archaeology. Instead, a durable ledger accumulates
verdict atoms and this verb reads them *before* the y.

## Two tiers, one vocabulary

`infra/readiness_sensors.py` owns the **sensor** layer: the `ssh -G` chain
resolution, the pure per-leg sensors, and the unit of record `VerdictAtom`. It
also carries an in-process `record_readiness` / `consult_readiness` ledger,
written explicitly as "the API the durable ledger will offer, so the ledger
builder swaps the storage and nothing above it moves."

`state/readiness.py` is that storage — the same ledger one tier down:

| tier | where | lifetime |
|---|---|---|
| cache | the sensors' in-process dict | one invocation |
| durable | `<journal home>/_readiness/<host>.json` | every process on the box, across restarts |

Read path is **consult-process-then-durable**; write path is **write-through**.
A composer that senses records into both, so a later invocation with a cold cache
still gets the reading from disk — which is what makes readiness *standing*
rather than per-invocation.

This verb reads the durable tier only. It is read-only and honestly so:
`verb="query"`, `side_effects=[]`, `idempotent=True`, **no SSH**. It never calls
the sensor layer: a read surface that senses on read is the fire-time discovery
this design removes.

The ledger is **machine-scoped**, not experiment-scoped: it sits beside the ssh
circuit breaker's state (`_readiness/`, sibling of `_ssh_circuit/`), because the
facts it records are properties of the cluster. `--experiment-dir` is accepted
for CLI-shape uniformity and deliberately unused.

## Inputs

A `ClusterReadinessSpec` (`hpc_agent._wire.queries.cluster_readiness`) plus the
standard `--experiment-dir`. Every field is optional — the empty spec `{}` is
valid and means "every cluster in `clusters.yaml` plus every host that has a
ledger":

- `cluster` (string, optional) — restrict to one `clusters.yaml` key. An unknown
  key is **not refused**: it reports as `unknown` with no ledger, because "I have
  never observed this" is a real answer and refusing would hide it.
- `host` (string, optional) — restrict to one ssh host. `user@host` is accepted
  and normalized to the host key the breaker, the throttle and the sensor
  layer's `_bare_host` all agree on. Combines with `cluster` as an intersection.
- `now` (ISO-8601 UTC string, optional) — deterministic-testing override (the
  `doctor` precedent). Sets `computed_at` and the single instant every age and
  freshness horizon is measured against. Never an agent-facing knob for reshaping
  ages.

## Outputs

A `ClusterReadinessResult`:

- `computed_at` — the one instant the whole projection is dated by.
- `clusters[]` — one entry per cluster/host, ordered by `(cluster or "", host)`.
  Each carries `cluster` (null for a ledger-only host), `host`, the overall
  `verdict`, `atoms[]`, and `ledger_corrupt`.
- `counts` — entries per overall verdict.
- `render` — the deterministic markdown digest, **relayed to the human
  verbatim**.

### The atom

An atom is `VerdictAtom` — the sensor layer's own unit of record — stored
verbatim, so a stored reading and a live one can never disagree about what was
seen. Its identity in the durable ledger is `(sensor, route, target)`: one atom
per identity, most recent wins (a ledger, not a log).

**`sensor`** — the sensor layer's `SensorKind`, extended in the SAME flat
vocabulary by the durable tier with the four pillar-3 invariants no sensor covers
yet:

| sensor | meaning | fed today |
|---|---|---|
| `hop` | TCP leg: one `ProxyJump` hop of the effective chain | sensor layer (`sense_route_legs`) |
| `direct` | TCP leg: the hop-bypassing alternative | sensor layer |
| `path` | the derived end-to-end verdict | sensor layer |
| `connect` | a session established over a named route | **also harvested** — the ssh circuit breaker's own record sites |
| `preamble` | the `module load` / `conda activate` class | sensor layer, **and** harvested from the breaker's degradation classifier |
| `auth` | credentials accepted | no — seam **by construction**: the breaker's SUCCESS folds "auth rejected but the host answered" into "reached the host", so feeding auth from there would assert what the evidence does not support |
| `scratch` | scratch reachability | no — seam (a preflight sensor holds the result) |
| `scheduler` | the scheduler answered | no — seam (a submit-block sensor holds the result) |
| `env` | remote env vs the expected wheel | no — seam (the env-lock compare holds the result) |

**`route`** — `effective` (what `ssh -G` resolved, hops included) / `direct`
(hop-bypassing) / `n/a`. The route is part of the atom's **subject**, not its
evidence: `preamble` failing on the effective route while passing on the direct
one is a *different fact* from failing on both, and collapsing them is exactly
the 2026-07-30 misread this substrate exists to prevent.

**`verdict`** — the sensor layer's `SensorVerdict` verbatim: `ok` / `down` /
`timeout` / `unknown` / `skipped`. `unknown` means the sensor ran but could not
settle it; `skipped` means it never ran. **Neither is "fine"**, and neither
grants readiness. A sensor kind nothing has fed is **emitted anyway** with
`verdict="unknown"` and a null `at` — absence is reported, never omitted, so an
unfed invariant can never be mistaken for a green one.

Plus `target`, `latency_ms` (never estimated), `at` / `at_epoch`, `detail`, and
one additive durable-tier-only `source` naming the seam that recorded it
(`VerdictAtom` does not carry it; reconstruction drops it).

### The overall verdict

Computed by `state/readiness.overall_verdict` — the one definition every surface
routes through:

- `unknown` — no atoms at all; nothing has ever been observed for this host.
- `degraded` — some **fresh** atom reads `down` or `timeout`.
- `stale` — no fresh failure, but the evidence does not support `ready`: a
  required sensor is missing, or any atom is past its horizon or is not `ok`. A
  **stale** failure lands here, not in `degraded` — the host may have healed and
  nothing has looked since, and fencing a cluster on expired evidence is the
  mistake `ssh_circuit.effective_state` exists to avoid.
- `ready` — every recorded atom is `ok` and fresh, and `connect` (the only
  required sensor) is present.

Only `connect` is required because it is the only atom anything feeds without
probing; promising more would make `ready` unreachable rather than more truthful.
The rest never block `ready` by **absence** — but a present one that failed or
went stale does downgrade, so wiring a seam can only ever make the verdict more
honest.

Freshness horizons are per sensor: 900s by default, 86400s for `env` (the answer
to "is the right wheel installed" changes on a deliberate reinstall, so holding
it to the transport horizon would keep every ledger permanently `stale` for no
evidence gain). These are the OVERALL-VERDICT horizons, distinct from — and much
longer than — the sensor layer's 120s *consult* window, which decides only
"may I skip a probe".

## Errors

- `spec_invalid` (`user`, not retry-safe) — `now` is not an ISO-8601 UTC string.

Nothing else raises. A missing or unreadable `clusters.yaml` contributes no
entries; a **corrupt ledger file** is reported as an empty ledger with
`ledger_corrupt=true` and an overall `unknown`, named in the render — and the
host stays **visible**, because vanishing from a readiness report reads as
"nothing to worry about here". A readiness read must never be the thing that
fails.

## Idempotency

`idempotent=true`, no idempotency key. Pure read, recomputed on every call, moves
no state and writes nothing. Two calls with the same `now` against the same
ledger are byte-identical.

## Notes

- **Harvest, never probe.** `state/readiness.record_observation` (one atom) and
  `record_atoms` (a composed read, write-through) are the only ways in, and the
  standing rule for every call site is: call them where a result is *already in
  hand*. A feed site that would open a connection or add a network call is out of
  contract — sensing belongs to the sensor layer.
- **Harvested today** at `infra/ssh_circuit.record_connection_success` /
  `record_connection_failure`: a `connect`/`effective` atom (`ok`, or `timeout`
  vs `down` split from the detail the breaker already recorded), plus a
  `preamble`/`effective` `timeout` when that call's own doc satisfies
  `is_preamble_degraded` (the run-13 livelock).
- **Writes are coalesced** for single observations: an unchanged atom younger
  than `readiness.MIN_REWRITE_SEC` writes nothing, so the breaker's hot success
  path stays one lock-free read. A healthy atom's age can therefore lag reality
  by up to that interval — far below every horizon, and disclosed as age rather
  than hidden. `record_atoms` does **not** coalesce: a composed sensor read is a
  deliberate, already-expensive act whose result must land whole.
- Related read surfaces: `doctor` (live breaker state) and `attention-queue`
  (`ssh-circuit-open` items). This verb is the *standing* view — what is known
  about a cluster, and how old that knowledge is.
