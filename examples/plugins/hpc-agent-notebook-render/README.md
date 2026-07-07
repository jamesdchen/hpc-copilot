# hpc-agent-notebook-render

The jupytext **notebook export** for the notebook-audit substrate — a projection
over *sealed* records (the audited source `.py`, the template, and the notebook
decision journal). It is a plugin, not core: jupytext/nbformat/nbclient are the
renderer's deps and never enter `src/hpc_agent`
([`docs/design/notebook-audit.md`](../../../docs/design/notebook-audit.md)
D-source; the boundary is a hard rule, and this plugin is deliberately outside
every core boundary lint).

Installing it adds two ordinary CLI + MCP verbs (via the `hpc_agent.plugins`
entry point — no host edit):

```bash
pip install -e examples/plugins/hpc-agent-notebook-render
hpc-agent notebook-render --spec .hpc/specs/render.json --experiment-dir .
hpc-agent notebook-ingest-signoffs --spec .hpc/specs/ingest.json --experiment-dir .
```

## Two roles, in order

### 1. The portability artifact — an audit readable anywhere, no harness

`notebook-render` converts the audited percent-format `.py` to a `.ipynb` and
annotates it: a header cell stating **this notebook is a render, never the source
of truth**, a per-section **audit cell** (status / tier / classification and the
short `section_sha` / `view_sha` the *core* view computed), and, for each
`human_required` section, a **sign-off scaffold** cell.

- The `.py` stays the single source of truth. Nothing here writes it; edits happen
  in the `.py` and are re-rendered.
- A **non-executed** render is byte-deterministic: identical inputs produce an
  identical `.ipynb` (fixed notebook metadata — no jupytext version block — empty
  per-cell metadata, and deterministic sequential cell ids; no timestamps).
- `--execute` runs the notebook via nbclient in the current env and computes a
  per-section `output_sha`. **Canonicalization (outsourced to nbdime):** the
  attestation *shape* is ours — `sha256` over the canonical JSON of each code
  cell's outputs — but the *definition* of what in an output is transient vs
  meaningful is nbdime's (Jupyter's own diff tool, the maintained encoding of
  that distinction). We strip exactly the output-level fields nbdime classifies
  as transient — `execution_count` and output `metadata` — and hash every
  remaining field verbatim: a stream's `name` + `text`, a result's mime `data`,
  an error's `ename` + `evalue` **and its `traceback`** (nbdime treats the
  traceback as meaningful, normalizing it only at compare-time). `error` is true
  iff any cell raised. The nbdime symbols that pin this form are
  `nbdime.merging.notebooks` (`strategies.transients`),
  `nbdime.diffing.notebooks.set_notebook_diff_targets`, and
  `compare_output_strict`. Consequence of adopting nbdime's classification:
  deterministic code yields the same `output_sha` across re-runs on one machine,
  but an **erroring** section is no longer guaranteed cross-machine-stable (the
  traceback now enters the hash, where the prior hand-rolled form dropped it).
  The canonicalizer identity `{canonicalizer: "nbdime", canonicalizer_version}`
  is recorded on the render result and in the notebook's
  `metadata.hpc_audit_canonicalizer` (core's receipt-entry model forbids extra
  keys, so it cannot ride the receipt itself), so an nbdime version bump reads as
  an explicit canonicalizer change, never as silent receipt drift.
- `--record_receipts` (requires `--execute`) journals a sha-bound render receipt
  per section via the **core** `notebook-record-receipt` op, in-process — so the
  receipt is bound to the freshly-parsed section sha server-side (the plugin
  cannot launder a receipt for a drifted section). Re-run `notebook-auto-clear`
  afterward and an assertion-bearing section auto-clears on its fresh receipt.

### 2. The ceiling — a SECOND CONFORMING HARNESS

A human typing into a rendered sign-off cell is out-of-band from the LLM, so the
render **is** a conforming harness: `notebook-ingest-signoffs` reads the
human-edited `.ipynb` and, for each typed sign-off, does exactly what a Claude
Code sign-off does:

1. writes the raw typed text through the documented utterance-log write API
   ([`docs/internals/harness-contract.md`](../../../docs/internals/harness-contract.md)
   §2 — `state/utterances.py::append_utterance`: same locator, frozen schema,
   no-scaffold precondition, provenance filter, fail-open), and
2. appends the sign-off through the **core** append-decision path
   (`scope_kind="notebook"`, `block="notebook-sign-off"`), recomputing
   `section_sha` / `view_sha` from the **current** source + template so the T8
   sign-off gate enforces recompute + authorship.

This makes the whole audit loop work with **no Claude Code anywhere**:

```
notebook-render  →  human types in Jupyter  →  notebook-ingest-signoffs  →  full-strength tier
```

Honest degradation (the no-scaffold rule): if the journal namespace for the repo
does not exist, the utterance write is a clean no-op and the result reports
`utterance_log: "absent-namespace"` — the sign-off still lands via append-decision,
but the full-strength authorship channel was absent and the tier is reported
honestly, never overclaimed. Per-section refusals (a bare ack, a slug the current
source no longer has, an unchanged scaffold, harness-injection text) are reported
per-section (`refused` / `skipped_empty`), never fatal to the batch.

## Verb reference

### `notebook-render` (mutate)

Spec: `{audit_id, source, template, output_path?, execute?, record_receipts?,
lint_findings?, attention_order?}` — `output_path` defaults to
`_notebooks/<audit_id>.ipynb`.

Result: `{audit_id, output_path, sections: [{slug, status, tier}], executed,
receipts_recorded: [slug], receipts_skipped: [slug]}`.

### `notebook-ingest-signoffs` (mutate)

Spec: `{audit_id, source, template, notebook_path}`.

Result: `{audit_id, ingested: [{section, section_sha, view_sha}],
refused: [{section, reason}], skipped_empty: [slug],
utterance_log: "written" | "absent-namespace"}`.

## The receipt-emitter convention (for non-notebook callers)

The execution-receipt half is ~15 lines of caller-side convention — a non-notebook
harness that already runs the sections just needs to emit
`{slug: {output_sha, error}}` and feed the **core** `notebook-record-receipt`
verb. This plugin's `_annotate.section_output_sha` is the reference canonicalizer;
the shape a hand-rolled emitter reproduces (strip nbdime's transient output
fields, hash the rest):

```python
# For each audited section: run its cells, then —
import hashlib, json
_NBDIME_TRANSIENT = {"execution_count", "metadata"}  # nbdime's output-level transient set
def output_sha(outputs):  # outputs = the section's cell outputs
    canon = [{k: v for k, v in o.items() if k not in _NBDIME_TRANSIENT} for o in outputs]
    blob = json.dumps(canon, sort_keys=True, separators=(",",":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()

# then: hpc-agent notebook-record-receipt --spec {audit_id, source, entries:{slug:{output_sha,error}}}
```

An emitter should also record its canonicalizer identity
(`{canonicalizer: "nbdime", canonicalizer_version}`) out of band, since the core
receipt entry carries only `{output_sha, error}`. Core binds each receipt to the
freshly-parsed section sha, so a receipt can only ever be recorded against
current source and drifts stale when the section moves.

## Tests

Offline only (no kernel network, a local `python3` kernelspec from `ipykernel`):

```bash
pytest examples/plugins/hpc-agent-notebook-render/tests -q
```
