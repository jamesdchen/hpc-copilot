# WS-AGENT — cluster-side campaign agent: DESIGN DOC + digest schema

Unit 5.4 · latency-elimination program · baseline main @ 8bbff383 · 2026-07-16

**Status: DESIGN ONLY. No `src/**` change lands with this unit.** The build is a
**follow-on program**, gated on a WS-PUSH (unit 2.6) telemetry cycle at cluster
scale (ARCHITECT-MEMO §6 risk 2 makes 2.6's Wave-2 telemetry gate *mandatory*
before anything is built on it). This document fixes the two doctrine mandates,
the digest schema, and the degrade ladder so the follow-on cannot silently
re-open a settled boundary.

Claim: `restructure.cluster-campaign-agent`. Enforcement row owed at land:
row 11 restated for the WS-AGENT digest (§7).

---

## 1. Problem — the latency this kills, and only this

A campaign today executes **fully asynchronously**: greenlit once, then
`reconcile` ticks self-chain in code (`meta/campaign/blocks.py:7`,
`campaign-watch` OBSERVES, never runs a tick) with no per-iteration human
boundary — only anomaly briefs and the completion brief. But each reconcile
tick is driven from the **local control plane**, so every tick pays a cold-SSH
round-trip census against the cluster (ControlMaster multiplexing is broken on
native Windows — the standing lesson behind the connection broker's retirement
and the probe-cache work). Over a multi-hour campaign that is tens to hundreds
of full handshakes whose only product is "nothing changed yet."

WS-PUSH (unit 2.6, P3) already moved the *wait* remote-side: one per-host census
waiter blocks on a cluster-side `sh` poll loop and returns a `{woke, acked,
waited}` HINT. WS-AGENT is the next rung: move the **reconcile driver itself**
onto the node so the tick loop runs where the data is, and the control plane
reads a compact **digest** instead of re-deriving cluster state over SSH each
tick. The win is the campaign's steady-state poll cost, not its correctness path
— which is exactly why the digest must be DATA (§4, §5) and never a verdict.

Expected kill (verifier-corrected, ARCHITECT-MEMO §2 Wave-5 posture): the
per-tick census round-trip collapses to a single digest read per control-plane
look; the tick cadence stops gating on local-side SSH latency. This is a
throughput win layered on top of P3's already-landed remote wait, informed by
its telemetry — never a substitute for it.

---

## 2. Design constraints (verbatim from the unit spec)

> SHIP the real hpc_agent reducer on-node (one-definition — never a stdlib
> reimplementation of combine); lifecycle kill at campaign-complete; install
> ladder degrades to today's polling; informed by WS-PUSH telemetry.

Unpacked, each constraint with its mechanism:

1. **The real `hpc_agent` reducer runs on-node — one definition, never a
   re-implementation.** The cluster-side agent MUST invoke the deployed
   `hpc_agent` combine/reduce/reporter code already shipped by
   `transport.deploy_runtime` / `_build_deploy_items`
   (`.hpc/_hpc_combiner.py` ← `execution/mapreduce/combiner.py`,
   `.hpc/_hpc_dispatch.py`, `execution/mapreduce/metrics_io.py`, and the status
   reporter `execution/mapreduce/reduce/status.py` at the **Python 3.8 floor**,
   unit 2.5's deployed reporter). It NEVER carries a hand-rolled stdlib
   re-implementation of combine/reduce. The one-definition rule that governs the
   deployed reducer (`aggregate` skill: "the reducer — never the LLM — computes
   every aggregate number") governs the on-node agent identically: an on-node
   process that recomputes an aggregate its own way is the divergence the
   lifecycle-verdicts principle exists to prevent (two copies that disagree).
   The agent is a *scheduler* of the deployed reducer, not a second reducer.

2. **Lifecycle: killed at `campaign-complete`.** The on-node agent's lifetime is
   bounded by the campaign's. When the control plane reaches
   `campaign-complete` (the completion brief — spend vs budget, terminal
   tallies), it MUST terminate the on-node agent as part of the same terminal
   sequence. A campaign that completes, is killed, or is abandoned must leave no
   orphaned reconcile driver on the login node holding a slot or re-arming a
   poll. This mirrors the tree-kill / no-orphan discipline the bounded
   subprocess runner already enforces for transport ops — a terminal campaign
   verdict is reporter-independent and kill-confirmed, and the agent teardown
   rides that same terminal evidence.

3. **Install ladder degrades to today's polling.** The on-node agent is an
   *optimization*, never a dependency. Where it cannot be installed or run, the
   system MUST fall back — byte-identically in behavior — to today's
   local-driven reconcile ticks (§6). No campaign is blocked, slowed on its
   correctness path, or made unrunnable by the agent's absence. A degraded
   install is a latency regression to baseline, never a functional one.

4. **Informed by WS-PUSH telemetry.** The build does not start until unit 2.6's
   remote-census telemetry has run a cluster-scale cycle (ARCHITECT-MEMO §6
   risk 2: a lab-green P3 can still fail only at cluster scale — NFS/Lustre
   cross-node fire, login-pool attribute-cache skew). The agent reuses P3's
   sticky-host + wake-is-a-hint posture; if P3's telemetry surfaces cross-node
   staleness, WS-AGENT inherits the fix rather than papering a second copy of
   the same NFS assumption.

---

## 3. Shape (informative — the follow-on's starting point, not a build spec)

- **Placement.** One agent per running campaign, launched on the cluster
  login node at greenlight, torn down at `campaign-complete` (§2.2). Launched
  via `python -m` on the deployed runtime (never a console-script `.exe` — the
  WinError-32 class does not apply on the node, but the `python -m` handshake on
  the full build fingerprint is the same durability posture the daemon program
  adopted; a fingerprint mismatch self-exits and the ladder degrades to §6).
- **Loop.** The agent runs the reconcile tick against local (on-node) reads —
  the deployed reporter (`reduce/status.py`) and the per-task announce markers
  P3 already writes — and, at each tick boundary, writes/refreshes a single
  **digest** sidecar (§4). It performs NO scheduler-mutating action beyond what
  today's reconcile tick does; it never resolves a decision, never appends a
  decision record, never authors a brief.
- **Control-plane read.** The local control plane, on its `campaign-watch`
  look, reads the digest (one cheap read, or one P3 wake + one digest read)
  instead of driving a full remote census. It then feeds the digest's DATA
  through the SAME `classify_polling` / `settle` it uses today (§4, §5). The
  digest changes *where the counts are gathered*, never *who computes the
  verdict*.
- **Anomaly / completion boundaries stay control-plane.** Only the control
  plane emits an anomaly brief or the completion brief; the on-node agent can
  populate a digest field that *routes attention* (a HINT) but the human-facing
  brief is code-rendered control-plane-side from the block's own evidence, as
  today.

---

## 4. THE DIGEST SCHEMA — the digest is DATA

The digest is a small, versioned JSON sidecar the on-node agent writes and the
control plane reads. It carries **observations and counts**, never verdicts.
Every consumer treats it as raw evidence fed into the control-plane decision
functions.

```
digest_schema_version : int         # pinned; lockstep with the reader (schema-version lint)
campaign_id           : str         # opaque; joins the control-plane campaign record
generated_at          : str         # ISO-8601, agent-node clock (informational)
generator_fingerprint : str         # the on-node hpc_agent build fingerprint (handshake / staleness)

# --- OBSERVED COUNTS (raw evidence, per run in the campaign) -------------
runs : {
  <run_id> : {
     # Reporter's own summary, byte-passed from reduce/status.py — the four
     # keys are the reporter's parse contract, NOT re-derived by the agent:
     summary   : {complete:int, running:int, pending:int, failed:int, unknown:int},
     rollup    : {<grid_key>: {complete,running,pending,failed,unknown,total}},
     # Announce-plane observation (P3 markers), a HINT surface only:
     announce  : {woke:bool, acked:bool, waited:float},
     # Reporter emission markers, passed through so the control plane can pick
     # the single-report vs two-call path (F3 rows_observed posture, unit 2.5):
     rows_observed_emitted : bool,
     # Provenance: reporter errors passed VERBATIM, never swallowed to a verdict:
     errors    : [{code:str, detail:str}],
  }, ...
}

# --- OPTIONAL: env-fingerprint OBSERVATION (see mandate (i), §5) ---------
env_fingerprint : {                 # PRESENT only if it routes through the kernel
   attestor    : "code",
   subject_kind: "cluster_env_fingerprint",
   subject_id  : <opaque>,          # caller/agent-authored, never core-invented
   content_sha : <str>,             # recompute-and-compare at the reader (bind)
   view_sha?   : <str>,
   evidence?   : <opaque>,
}
```

**What the digest MUST NOT carry:**

- No `verdict`, `status: complete|failed|abandoned`, `settled`, `terminal`,
  `is_complete`, or any field that names a lifecycle *decision*. Counts and
  reporter errors only. (A `summary.complete` **count** is data; a
  run-level `verdict: "complete"` is a settle — forbidden.)
- No `resolved`, `greenlit`, `consented`, or any decision-journal field — the
  agent never resolves a decision.
- No re-computed aggregate (mean/tally) that the deployed reducer did not
  compute — the agent passes the reducer's numbers through, it does not make
  its own (mandate: one-definition combine, §2.1).
- No spend authorization, budget override, or cost verdict.

The `env_fingerprint` block is the ONE place the word "attestation" could appear
in the digest, and it is governed by mandate (i) below: it either routes through
the attestation kernel or it does not call itself one.

---

## 5. THE TWO DOCTRINE MANDATES (verbatim)

These are reproduced verbatim from the unit spec and are binding on the
follow-on build.

### Mandate (i)

> env-fingerprint 'attestation' in the digest routes through
> `state/attestation.py`::bind/reduce or stops calling itself an attestation
> (row L141).

**Mechanism.** If the digest's `env_fingerprint` block is called an
*attestation*, it MUST be a genuine instance of the ONE attestation kernel — the
record shape `{attestor, subject_kind, subject_id, content_sha, view_sha?,
attestor_id?, evidence?}` — and it MUST be appended/consumed through
`state/attestation.py::bind` (recompute-and-compare: the asserted `content_sha`
is re-hashed at the reader and must equal the recompute, so a fingerprint hash
cannot be asserted into existence) and reduced through
`state/attestation.py::reduce` (newest-first drift-revocation: an env that moved
since the fingerprint was taken reads STALE, with no state machine). `subject_id`
is agent-authored and NEVER core-invented (kernel invariant). A `code`
attestation rests on `bind`'s recompute alone — no human-authorship gate applies
to it, and equally, a code fingerprint can NEVER satisfy a human tier.

If the follow-on does not want to pay the kernel's recompute-and-bind contract
for the env fingerprint, it MUST rename the field to a plain observation (e.g.
`env_fingerprint_observed_sha`) and drop the word "attestation" — a field that
calls itself an attestation while bypassing `bind`/`reduce` is precisely the
receipt-laundering class the kernel exists to close, and the enforcement wall
(the conformance boundary's `test_append_binds_through_the_attestation_kernel`
precedent) rejects it. There is no third option.

### Mandate (ii)

> the digest is DATA — control-plane classify_polling/settle still compute
> every verdict; a cluster-computed verdict is never trusted directly (rows
> L244/L249 extended; marker-never-settles posture restated from 2.6/row 11).

**Mechanism.** The control plane's `classify_polling` / `settle`
(`ops/monitor` — the ONE count→verdict definition every call site routes
through) computes every terminal verdict from the digest's OBSERVED counts. The
digest supplies the raw `summary`/`rollup`/`errors`/`announce` evidence; the
verdict (complete / failed / abandoned, and its `verdict_reason`) is derived
control-plane-side, exactly as today. A `verdict`-shaped field arriving from the
node is NEVER trusted directly — it does not exist in the schema (§4), and if a
future digest smuggled one in, the reader discards it and re-derives from counts.

This is the **marker-never-settles** posture (unit 2.6 / row 11) restated one
rung up: P3's per-host census marker returns a `{woke, acked, waited}` HINT and
the control plane re-reads the per-task markers (the truth) to settle; WS-AGENT's
digest is the same shape at campaign scale — **a marker/digest WAKES or INFORMS,
never SETTLES**. A forged or premature digest can waste a control-plane look, but
it can never move a run to terminal: the settle read still runs against the
truth, its ack must fire, and a non-terminal re-read keeps the campaign
watching. The failure mode designed out here is the program's dominant one
(ARCHITECT-MEMO §6 risk 1): a wrong-but-plausible cluster-computed verdict
trusted over the control plane's own recomputation.

The lifecycle-verdicts corollary also holds: **the verdict is revisable, the
evidence is durable.** The digest is durable observed evidence; the verdict
derived from it is not sticky — reconcile legitimately downgrades a premature
`complete` when new evidence arrives, and the digest feeding a fresher count is
exactly how that correction reaches the control plane. No "terminal is sticky"
guard is added to the digest path.

---

## 6. THE DEGRADE LADDER (install ladder → today's polling)

The on-node agent degrades, rung by rung, to today's local-driven reconcile
polling. Each rung is behaviorally byte-identical to baseline on the correctness
path; only latency degrades.

| Rung | Precondition | Behavior |
|---|---|---|
| **0 — Full WS-AGENT** | On-node `hpc_agent` runtime deployed AND its build fingerprint matches the control plane's AND the node permits a long-lived login-node process AND WS-PUSH announce-plane is live | Agent runs the reconcile tick on-node against local reads + the deployed reducer, refreshes the digest each tick; control plane reads the digest (+ one P3 wake) instead of driving a remote census. Full latency win. |
| **1 — Digest-less, P3 wait** | Agent cannot launch/persist, but WS-PUSH remote census waiter IS available | Fall back to unit 2.6's remote `sh` poll wait: the *wait* is still remote-side (`{woke, acked, waited}` HINT), but the reconcile tick and reporter read are driven control-plane-side. Partial win — the tick round-trip returns. |
| **2 — Local polling (today)** | Neither the agent nor the remote waiter is available (older cluster, no deployed runtime, plugin/env mismatch, `python -m` handshake fingerprint mismatch → self-exit) | The control plane drives reconcile ticks with a local client sleep + cold-SSH census, exactly as today. **This is the baseline; it is never worse than pre-program behavior.** |

Ladder rules:

- **Fail toward polling, never toward silence.** Any agent-side failure — launch
  refused, fingerprint mismatch (self-exit), a severed digest read, a digest
  whose `generator_fingerprint` disagrees with the control plane, a truncated
  or malformed digest — drops one rung and is disclosed in the tick's
  provenance. A degraded read is UNKNOWN → drop to a live census; it is NEVER
  parsed-and-trusted (the F3 severed-report lesson: a severed channel raises
  UNKNOWN, never "zero rows"/"all complete").
- **The digest is a cache with a walk fallback pinned byte-identical.** Like
  every cache in the program (ARCHITECT-MEMO §6 risk 1), the digest is
  content/fingerprint-keyed, success-only, and its miss/staleness path recomputes
  the counts via the live census with byte-identical downstream behavior. A stale
  digest is a latency miss, never a wrong answer.
- **A missing agent is never a blocked campaign.** Rung 2 always exists. No
  campaign requires WS-AGENT to run, complete, or settle.

---

## 7. Enforcement-map row owed (restated for the WS-AGENT digest)

Owed in `docs/internals/principles/lifecycle-verdicts.md`'s enforcement map when
the follow-on build lands (this docs-only unit records it; it does not mechanize
it — there is no `src/**` to guard yet):

> **Row 11 (restated for WS-AGENT):** the WS-AGENT digest is DATA — a
> marker/digest WAKES or INFORMS, never SETTLES; the control plane computes
> every verdict. A digest carrying a lifecycle verdict, or a control-plane path
> that trusts a cluster-computed verdict instead of re-deriving via
> `classify_polling`/`settle`, is the fire.

When built, the row's *Enforced by* names the fire-path test (a digest with a
planted `verdict` field must be discarded and the run re-settled from counts; a
severed/forged digest must drop to a live census, never settle) and its *Fires
when* names the regression (a digest schema field that encodes a decision, or a
reader that shortcuts the control-plane settle). Every new lint/guard lands with
a synthetic-violation fire test (repo standard).

Mandate (i) is enforced by the existing attestation-kernel wall: the follow-on's
`env_fingerprint` block, if named an attestation, is covered by the
`test_append_binds_through_the_attestation_kernel` precedent (bypass `bind` →
red); if renamed to a plain observation, no attestation claim is made and the
row does not apply.

---

## 8. Non-goals / explicitly out of scope

- **Concurrency on the node.** v1 (when built) serializes, mirroring the daemon
  program's single-worker posture; multi-campaign concurrency on one node is a
  later ruling, not this design.
- **The agent authoring any decision.** It never resolves, greenlights,
  consents, or writes a brief. Attention-routing is a HINT; the brief is
  control-plane code-rendered.
- **Replacing the deployed reducer.** The agent schedules the one deployed
  reducer; it is not a second reduce implementation (§2.1).
- **A default-on rollout.** Like the asyncssh (445ce69a) and daemon (R4)
  ladders, the first cut is opt-in / telemetry-gated; a default flip is a
  separate ruling with live numbers.

---

## Drift log

- 2026-07-16: Created (unit 5.4, latency-elimination program). Docs-only; build
  is a follow-on gated on unit 2.6 (WS-PUSH) cluster-scale telemetry. Carries the
  two doctrine mandates verbatim, the digest schema, the degrade ladder, and the
  restated row-11 owed at build. Rulings context: R1(+D1)/R2/R3(+sha pin)/R5
  APPROVED; R4 amended (daemon superseded by `docs/plans/daemon-engineering-2026-07-16/`);
  R6 defer, R7 deny/defer.
