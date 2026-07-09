# conformance-record

Journal **one live conformance observation** against the registration it tests —
the emitter's evidence FOR/AGAINST a sealed hypothesis at production cadence
(`docs/design/live-conformance.md` C-verbs / C-store / C1). Each call appends
exactly one line to the registration-scoped ledger
`<experiment>/_aggregated/_conformance/<registration_id>.jsonl` — a journaled
**CODE attestation**, sha-linked to the registration. Its ONLY side effect is
that single append.

**`agent_facing=False`.** A human/cron-invoked CLI verb, never an agent tool: an
agent authoring the outcome stream that judges its own registration is the
receipt-laundering class at the operation boundary. The emitter is caller
machinery, not the driving agent. Core never gains a connector, a credential, or
a polling loop — this verb OBSERVES and RECORDS, it never actuates.

## The recording posture, by registration status

- **ABSENT** — no registration record reduces for the id → refused loudly
  (`spec_invalid`): there is no hypothesis to test (the fabrication class).
- **PRESENT** — the registration's journal reduces to a status, STAMPED into
  `status_at_record`. Recording is **fail-open for evidence**: a
  stale / revoked / superseded registration is RECORDED with the reduced status
  disclosed, never refused (production is the experiment that never stops;
  refusing evidence is the one thing an evidence system must not do).

`status_at_record` is the registration's **reduced** status at record time,
resolved with `verify-registration`'s reader/facade idioms but ONLY the
dossier-sha leg — not the full four-leg verify view (template, prerequisites,
brief, `view_sha`), which is the query/verify seat's job. The id's journal is
read through the one reader, the winner's live dossier signature is recomputed
(degrade-to-`None` on any gap), and the reduction reports `current` when the
sealed dossier still hashes to the sha it bound, `stale` when it drifted (or the
run vanished), and `revoked` when the newest family record is an overturn. (The
horizon-lapse `stale` cause of C-horizon is inherited once registration T6
lands.) The live *window* is judged against the sealed baseline by the
`conformance-status` query, not here. Cost note: re-gathering the dossier
signature per observation is the honest price of an honest stamp; a caching
optimization is future work.

## The trust boundary (C1 / F8 honesty)

The verb BINDS the exact recorded bytes: the payload `content_sha` is recomputed
SERVER-SIDE over `{payload, labels, observed_at}` (the harness sha
canonicalization) and bound at append (`state/attestation.py::bind`). A caller
CANNOT assert a sha into existence — there is **no sha field on the spec**.
Truthfulness of the `payload` / `observed_at` VALUES is the emitter's own (the
same trust class as a conforming harness's out-of-band writes); core vouches for
the bytes it hashed, never for the world they describe. `payload` keys and
values, `labels`, and `emitter` are opaque caller data — identity-compared,
range-compared, counted; never read for meaning.

## The emitter contract (C-emitter — caller-side forever)

The observation EMITTER is caller-side machinery this verb records for. Core
ships none of it; the ~30-line convention is the caller's, verbatim:

1. The emitter lives in the CALLER's environment and owns all domain I/O — it is
   the only thing that ever touches a broker, instrument, or data feed. Core
   never gains a connector, a credential field, or a polling loop.
2. It reduces each observation to the flat opaque payload `{key: scalar}` using
   the SAME keys the registered baseline carries (the caller's mapping — core
   never learns what a fill is).
3. It records via **`conformance-record`** with `registration_id`, `payload`,
   `observed_at`, optional `labels`, and its `emitter` id — one CLI call per
   observation or batch.
4. Its truthfulness is its own: the verb binds the sha, not the world. An
   emitter that lies is the same trust class as a harness that edits its own
   config (`docs/internals/harness-contract.md`, "The honest trust limit") — out
   of scope, honestly named.
5. Cadence, batching, retention of raw domain data: caller policy, never core's.

## Inputs

`ConformanceRecordSpec` (`hpc_agent._wire.actions.conformance_record`):

- `registration_id` — the registration this observation tests (a filesystem-safe
  slug; the ledger is keyed on it). An absent registration is refused loudly.
- `payload` — the flat, already-reduced observation `{caller-key: opaque
  scalar}`, using the SAME keys the registered baseline carries.
- `observed_at` — the ISO timestamp the CALLER says the observation occurred
  (caller-attested; core hashes it, never verifies it). Feeds query-time window
  selection; distinct from the server-stamped record `ts`.
- `labels` — opaque caller labels (a cluster, a batch id, a venue tag). Label
  novelty relative to a window is disclosed at query time, never interpreted.
- `emitter` — an opaque caller-declared emitter id, recorded for provenance.

Deliberately carries **no `content_sha`** — a sha on the wire is a claim core
ignores, so it is not accepted at all.

## Output

`ConformanceRecordResult`: `registration_id`, the SERVER-recomputed
`content_sha`, the stamped `status_at_record`
(`current` | `stale` | `revoked` | `superseded`), the echoed `observed_at`, and
the `ledger_path` the line appended to.

## Boundary

This verb OBSERVES and RECORDS; it never actuates. A recorded observation
changes NO registration status, revokes nothing, halts nothing — drift routes
attention (via `conformance-status` and the attention queue), never action. The
only remedies are the registration kernel's own human acts (re-register,
revoke).
