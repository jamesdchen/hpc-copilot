# evidence-brief

The **point query over the evidence memory**: given a scope — one or more
**tags** and/or a **lineage** run_id — it projects the cross-store digest for
that scope from the program's sealed records. Dated, sha-cited **conclusions**
(newest current first), per-tag **prior-work** counts, per-lineage determinism
**envelopes** quoted verbatim from the fingerprint ledger, and each current
conclusion's **citations re-resolved at read** (`verified` / unresolvable —
disclosed, never refused). A deterministic markdown digest rides the result for
the human to read **verbatim**.

A read-only `query` (the `attention-queue` / `run-story` posture: no SSH, no side
effects, `idempotent=True`, `requires_ssh=False`). It answers the
cross-experiment question — "what have we tested under tag X, when, with what
envelopes and what verdicts?" — as a **projection over sealed records**, never a
narrative anyone authored after the fact (`docs/design/evidence-memory.md`, E5).
Cheap enough (journal-first, no SSH) to embed in every greenlight.

## Inputs

An `EvidenceBriefSpec` (`hpc_agent._wire.queries.evidence`) plus the standard
`--experiment-dir`:

- `tags` (list of scope-tag slugs, default `[]`) — select by tag membership: a
  record matches when it carries any queried tag **or** a current conclusion
  retro-indexes it under one. Opaque caller data — core never interprets a tag's
  meaning.
- `lineage` (a run_id, default null) — select by **code identity**: the run's
  lineage chain + `cmd_sha`. The always-present fallback that needs no human to
  have tagged anything.
- **At least one of `tags` / `lineage` is required** — an unkeyed point query is
  the recorded browse non-goal (the agent is the browser; there is no faceted
  explorer).
- `as_of` (ISO-8601 timestamp, default null) — the collector includes only
  records with `ts <= as_of`, so the digest is "what was known as of that date".
  A timestamp, never a named period (`2025H1` is caller vocabulary).
- `fleet` (bool, default `false`) — when `false`, scope is the single
  `experiment_dir`. When `true`, run the identical per-namespace walk over **every
  experiment this machine has journaled** (glob the journal home for
  `*/repo.json`); a torn/unreadable namespace is **skipped** and counted in
  `skipped`, never fatal.

## Output

An `EvidenceBriefResult`:

- `computed_at` / `as_of` — the instant the digest was computed and the `as_of`
  cut echoed.
- `conclusions` — dated, sha-cited findings, newest current first (the lead).
- `activity` — per-tag prior-work counts (campaigns / runs / lineages / looks /
  newest). Counts and dates only — no ranking, no urgency.
- `envelopes` — per-lineage determinism envelopes, the evidence-label block
  (`n`, `n_full`, `n_partial`, `scales`, `clusters`) quoted verbatim from the
  fingerprint ledger.
- `citations_status` — each current conclusion's citations re-resolved at read:
  `verified` when the sha still matches on this namespace, otherwise unresolvable
  (evidence legitimately moves — archived to S3, a store re-exported, a repo
  wiped). A disclosure, **never a refusal**; only the append gate refuses loudly.
- `skipped` — namespaces skipped during fleet collection (fail-open accounting).
- `cache` — `hit` | `miss` | `disabled`, recorded honestly. The index is a
  content-keyed, disposable cache (`state/evidence_cache.py`): a hit is served
  only when nothing the collector would walk changed; **deleting the cache loses
  nothing** — it recomputes a byte-equal digest. `HPC_NO_EVIDENCE_CACHE=1` opts
  out entirely.
- `render` — the deterministic markdown digest, relayed to the human verbatim.

## Boundary

The digest is **code-composed from the records' own fields** — no LLM, no urgency
prose, no recommendation, no interpretation of what a tag or a finding means
(`docs/design/evidence-memory.md`, the E-render enforcement rows). This is a
dedicated query verb: it **may raise honestly** on a structural spec error (a
malformed tag slug), unlike the embedded advisory seats (greenlight / S1) which
fail open. It moves **no state** and writes nothing but its disposable cache.
