# The harness-conformance kit — publishing the harness contract

**Status: PLANNED (2026-07-07), not yet implemented.** This is the hand-off
plan (the `notebook-audit.md` pattern): settled decisions + rationale,
file-disjoint Opus task waves, enforcement rows, boundary-drift flags. Cite
`path::symbol`, never line numbers. Record implementation drift in a drift
log at the end of this document when the waves land.

## Product intent

`docs/internals/harness-contract.md` is documented but NOT published.
Publishing a contract = a **version**, a **name**, and an **executable
CONFORMANCE KIT** a second harness runs to prove conformance — the
TCK / Web-Platform-Tests pattern. The kit does two jobs at once:

1. It turns "harness-agnostic" from prose into a **checkable claim**: a
   stranger's harness runs the kit against its own capability providers and
   earns (or is refused) the named conformance verdict.
2. It **pins our own side in CI**: the Claude Code hooks and the notebook
   plugin are certified by the same kit on every push, so the contract can
   never drift from the code that implements it (the same drift-guard
   philosophy as `tests/contracts/test_harness_contract.py`, upgraded from
   doc-prose pins to behavior).

The protocol rides (user-approved, DECIDED inputs to this plan):

- **Capability 1 rides MCP ELICITATION** where clients support it — the
  elicitation sections of `docs/internals/harness-contract.md` have LANDED
  (ba83eac3), and the `harness-capabilities` verb (`ops/harness_capabilities.py`)
  EXISTS in the registry. This kit CONSUMES both; it builds neither. (K6
  consumes the E3-a-reshaped `harness-capabilities` result; wave C's K10
  re-reads the now-present elicitation sections before stamping the
  version.)
- **The attestation/dossier export steals the in-toto/DSSE Statement
  envelope**: subject = sealed file digests, predicateType = our record
  vocabulary, predicate = the records verbatim; v1 = unsigned Statements in
  a DSSE-ready envelope. Ecosystem tools verify the bundle without hpc-agent.
- **Capability negotiation is LSP-style DETECTION** (the
  `harness-capabilities` verb): `declared == detected == behaved` is the
  kit's negotiation assertion. No self-asserted capability manifests.
- **Capability 2 splits into INSPECT and ACT**: INSPECT (reading the final
  agent-visible message) may ride the OTel GenAI semantic conventions where
  the harness emits them; ACT (forcing one continuation) has two conforming
  implementation shapes — harness hooks (the
  `_kernel/hooks/relay_audit_stop.py` posture) OR a **response gateway**
  applying `ops/decision/verify_relay.py::verify_relay` pre-delivery. The
  kit's scenario fixtures exercise both through one adapter seam.

## Settled decisions

### D-K1 — kit shape: a pytest package SHIPPED IN THE WHEEL, parameterized by a harness adapter

The kit is a pytest test package at `src/hpc_agent/conformance/`, run as

```bash
pytest --pyargs hpc_agent.conformance --harness-adapter mypkg.adapter:build
```

**Where it lives — the tradeoff, recorded.** Three candidates:

- `tests/conformance/` (repo-only): rejected — the kit's defining
  requirement is running OUTSIDE our repo against a stranger's harness;
  repo tests don't ship.
- A separate installable (`hpc-agent-conformance` wheel): rejected — a
  second release artifact means version-skew between contract, kit, and
  reference implementation, plus duplicated release machinery. The
  plugins-lane precedent (`examples/plugins/`) is explicitly UNPUBLISHED
  example code, so it is not a home for a normative artifact either.
- **A subpackage of the shipped `hpc_agent` wheel (chosen).** A conforming
  harness necessarily has hpc-agent installed — the CLI is the invariant
  substrate the harness wraps ("The CLI is the invariant substrate",
  harness-contract.md) — so the kit rides the wheel the harness already
  depends on, `pytest --pyargs` makes it runnable anywhere, fixtures ship
  as package data, and the kit version is pinned to the package version by
  construction. Cost: pytest becomes a runtime requirement *of running the
  kit* — NOT of core; the kit package imports pytest only inside its own
  test modules, and nothing in `hpc_agent` outside `conformance/` may
  import from it (enforcement row below).

### D-K2 — the adapter interface (exact)

`src/hpc_agent/conformance/adapter.py` — stdlib-only, importable without
pytest:

```python
class EnforcementOutcome(NamedTuple):
    blocked: bool            # the harness forced a continuation
    reason: str | None       # itemized mismatch summary when blocked

class WakeEvent(NamedTuple):
    woke: bool               # the driver was re-invoked after detach
    terminal_seen: bool      # the wake observed the worker's terminal record

class HarnessAdapter(Protocol):
    name: str                # the harness's published name (report identity)

    # --- capability 1: the out-of-band utterance channel ---
    def write_utterance(self, experiment_dir: Path, text: str) -> None:
        """Deliver *text* through YOUR harness's human-input channel
        end-to-end, exactly as if a human typed it — so the record (if any)
        lands via your writer, filters included. The kit never writes the
        log directly; it drives your channel and reads the log back through
        state/utterances.py::read_utterances."""

    # --- capability 2: the relay enforcement point (ACT) ---
    def run_enforcement_point(
        self, experiment_dir: Path, final_message: str, *, previously_blocked: bool = False
    ) -> EnforcementOutcome:
        """Run YOUR enforcement seam over *final_message* as the final
        agent-visible text for the cwd repo. Hook-shaped harnesses replay
        their Stop seam; gateway-shaped harnesses run their pre-delivery
        verify_relay pass. previously_blocked=True models the
        stop_hook_active re-entry — a conforming seam NEVER blocks twice."""

    # --- capability 3: backgrounding / wake ---
    def start_background(self, experiment_dir: Path, argv: list[str]) -> Any: ...
    def await_wake(self, handle: Any, timeout_s: float) -> WakeEvent: ...

    # --- optional; kit skips the matching assertions when absent ---
    def answer_question(
        self, experiment_dir: Path, offered_labels: list[str], answer: str
    ) -> None:
        """Drive YOUR structured-question channel (the AskUserQuestion /
        MCP-elicitation analog) with *answer* against *offered_labels* —
        exercises the clicked-vs-typed provenance line (_is_clicked)."""

    def detect_capabilities(self, experiment_dir: Path) -> frozenset[str]:
        """Default implementation (provided): invoke the core
        `harness-capabilities` verb via the CLI in the harness's
        environment and return its detected set."""
```

Adapter loading: `--harness-adapter <module.path:factory>` (a zero-arg
factory returning the adapter). "Declared" = the set of Protocol methods the
adapter actually implements (no manifest field — implementing the callable
IS the declaration; detection-only doctrine). Capability names are the three
contract nouns: `"utterance-log"`, `"relay-enforcement"`, `"backgrounding"`.

**Skips are honest, not failures**: an adapter without capability 3 gets the
backgrounding modules SKIPPED and the final report names the degraded tier
verbatim from the contract ("synchronous in-turn execution; correctness
unaffected"). But the CONFORMANCE verdict ("conforming to harness contract
v1") requires all three capabilities passing — the contract says a
conforming harness MUST provide all three; anything less is reported as
"partial: <capability list>", never as conforming.

### D-K3 — per-capability assertions

**Capability 1 — the utterance log** (`test_capability_utterance_log.py`).
Every write goes through `adapter.write_utterance`; every read through
`state/utterances.py::read_utterances`. Asserted, per the normative §2:

- **Record schema byte-rules**: exactly `{ts, sha256, text}`, sorted-keys
  JSON, one object per line, append-only oldest-first; writer adds no other
  fields.
- **Full-text sha**: `sha256` digests the FULL raw text even when `text` is
  capped (fixture: a >4096-byte utterance; assert digest matches the
  uncapped input).
- **Codepoint truncation**: a multi-byte-codepoint fixture straddling the
  `MAX_UTTERANCE_BYTES` boundary decodes cleanly — never a mid-codepoint
  cut.
- **Storage locator**: the record lands at
  `state/utterances.py::utterances_path` for the fixture repo — i.e. the
  writer reused `_current_homedir` + `repo_hash`, never a re-derived hash
  (a divergent hash simply fails the read-back).
- **No-scaffold**: writing against a repo with NO journal namespace leaves
  ZERO footprint (no directory created, no exception).
- **Provenance filters**: text opening with each `HARNESS_INJECTION_RE` tag,
  fed through the adapter's human channel, must NOT land (the filter routes
  through `state.utterances.is_harness_injected` — one definition; the kit
  derives its tag fixtures FROM the exported regex so a filter extension
  auto-extends the fixtures). A tag quoted mid-text MUST land. When
  `answer_question` is implemented: a pure click on offered labels must not
  land; typed "Other" residue must land (the `_is_clicked` line).
- **Fail-open**: an unwritable log (read-only namespace fixture) degrades to
  a clean no-op — the channel never raises into the harness.
- **The consumer-defined pass (load-bearing integration assert)**: after the
  adapter writes utterances stating a sweep ("20 seeds at 1M samples"),
  `append-decision` committing a REQUIRED_CALLER `task_generator` derived
  from that text is GRANTED at tier 1 by hpc-agent's own gate
  (`ops/decision/journal.py::_assert_human_authorship` over
  `::_harness_human_texts`), and a fabricated value the utterances never
  stated is REFUSED. Both directions — the gate passing is only meaningful
  because it can also fire (engineering-principles: verify a guard can
  actually fire). This is the assertion that makes the kit a TCK rather
  than a lint: the candidate harness's records satisfy the real consumer.

**Capability 2 — relay enforcement** (`test_capability_relay.py`).
Scenario fixtures: `(journal state, final message, expected verdict)`
triples under `conformance/fixtures/relay/`, exercising every contradiction
kind in `relay_audit_stop._CONTRADICTION_KINDS` plus the pass cases:

- `number` — a rounded/altered numeric claim vs the recorded value;
- `state` — "running" relayed over a journaled `failed` (the proving-run-#3
  scenario), and the notebook form: a wrong section status / module
  `passed` verdict (`verify_relay.py::verify_notebook_relay` reuses the
  kind);
- `run_id` — a run-id-like token matching no journaled id;
- notebook `number` — a sha-hex matching neither current nor recorded shas;
- PASS: `unverifiable` claims (a final message legitimately carries them),
  truncation-prefix decimals, a message naming no run/audit at all;
- **loop safety**: the same triple with `previously_blocked=True` must not
  block (block at most once, never hard-block a session);
- **fail-open**: an unreadable journal fixture → not blocked.

The SAME triples certify both conforming ACT shapes — hooks and response
gateway — because the adapter seam is outcome-shaped
(`EnforcementOutcome`), not mechanism-shaped. INSPECT is the adapter's
business (transcript tail, OTel GenAI `gen_ai` events, gateway buffer); the
kit hands it the final text and judges only the ACT.

**Capability 3 — backgrounding/wake** (`test_capability_backgrounding.py`).
One detached-lifecycle fixture: the kit supplies a stub worker script that
sleeps briefly and writes a terminal JSON into the journal namespace;
`start_background` + `await_wake` must yield `woke=True, terminal_seen=True`
within the timeout — i.e. the journal remained the durable rendezvous the
woken driver reads. No scheduler, no SSH, no network.

**Negotiation** (`test_negotiation.py`). The kit's closing assertion:
`declared == detected == behaved` — the adapter's implemented-method set,
the `harness-capabilities` verb's detected set (via
`adapter.detect_capabilities`), and the set of capabilities whose kit
modules actually PASSED must be one set. A harness that detects a
capability it cannot behave, or behaves one detection misses, is
non-conforming: detection is only trustworthy if it is exact.

**The projection rule (verb → kit nouns; coherence review 2026-07-07).** The
real `harness-capabilities` verb reports FOUR capabilities, one of which —
`trusted_display` — it always reports as `"unknown"` (core cannot verify a
harness renders trusted content; there is no kit noun for it, and it is
EXCLUDED from the negotiation set). The other three project onto the kit's
three contract nouns `{"utterance-log", "relay-enforcement",
"backgrounding"}`; the negotiation set is that projection, never the raw
four.

**Which detection leg is a per-harness SEAM vs a core-side CONSTANT** (the
honest-detection rule):

- `"backgrounding"` detection is a CORE-SIDE constant — always true (the CLI
  substrate always supports the detached-worker path), so the kit asserts
  only BEHAVED for it (the fixture wakes), never a per-harness detection.
- `"relay-enforcement"` and `"utterance-log"` detection are per-harness SEAMS.
  For **Claude Code**, capability-1 detection is by SEAM — the hook needles
  the verb probes. For a **non-Claude-Code harness**, capability-1 detection
  is detection-BY-BEHAVIOR: the adapter's `write_utterance` path proving the
  reader (`read_utterances`) accepts what it wrote, NOT the Claude-Code hook
  needles (which a foreign harness never presents). A harness providing a
  capability through a different seam is detected by BEHAVING it.
- An honestly-PARTIAL adapter (a capability genuinely absent) earns a SKIP on
  that capability's modules and is reported partial — never a negotiation
  FAILURE. Negotiation fails only on three-way DISAGREEMENT for a capability
  the harness DOES claim (detected-but-not-behaved, or behaved-but-not-
  detected): detection must be exact for what it claims, silent for what it
  skips.

**Canonicalization** (`test_canonicalization.py`). Fixture payloads (JSON
values + expected digests, committed under `conformance/fixtures/canon/`)
recomputed byte-for-byte per the normative sha section: `sort_keys=True`
(code-point key order — deliberately NOT RFC 8785), compact separators,
`ensure_ascii=False`, UTF-8, SHA-256 lowercase hex; plus
`state/audit_source.py::normalize_source` vectors (CRLF, lone CR, trailing
whitespace). Fixtures include the adversarial cases where Python sort and
JCS UTF-16 ordering DIVERGE (keys above U+FFFF vs BMP), so a non-Python
implementation that silently plugged JCS fails loudly here — the recorded
escape is `canon_version` on new records, never a silent swap.

### D-K4 — the in-toto export: a SIBLING verb, `export-attestations`

**Not an extension of `export-dossier`.** Three pins force the sibling:
`tests/contracts/test_dossier_boundary.py` holds the manifest entry shape
to exactly `{source, path, sha256, bytes}` by AST (a Statement-shaped entry
breaks it), holds `DOSSIER_SOURCES` closed by equality, and bans
`json.load(s)` in the module. The dossier stays the sealing layer; the
attestation export is the PORTABILITY layer over it.

- **Verb shape**: `export-attestations` (mutate, `agent_facing=True`,
  idempotent on `run_id`), module `ops/export_attestations.py`, wire
  `_wire/actions/export_attestations.py`. Spec
  `{run_id, include_lineage?, output_path?}`; it calls the public
  `ops/export_dossier.py::export_dossier` in-process (one gather
  definition — never a second walk of the stores) and projects the sealed
  entries into one Statement per bundled store file. Default output:
  `<experiment>/_dossier/<run_id>.attestations.jsonl` (one DSSE envelope
  per line).
- **The Statement mapping table** (record store noun → predicateType URI;
  scheme `https://hpc-agent.dev/attestation/<store-noun>/v1`, one URI per
  `DOSSIER_SOURCES` noun):

  | store noun (`DOSSIER_SOURCES`) | predicateType |
  |---|---|
  | `sidecar` | `https://hpc-agent.dev/attestation/sidecar/v1` |
  | `decision-journal` | `.../decision-journal/v1` |
  | `briefs` | `.../briefs/v1` |
  | `block-terminal` | `.../block-terminal/v1` |
  | `journal-record` | `.../journal-record/v1` |
  | `scope-journal` | `.../scope-journal/v1` |
  | `look-ledger` | `.../look-ledger/v1` |
  | `aggregated` | `.../aggregated/v1` |
  | `audited-source` | `.../audited-source/v1` |
  | `notebook-journal` | `.../notebook-journal/v1` |

  The map is derived FROM `DOSSIER_SOURCES` (one closed vocabulary, one
  derivation), so a new store noun automatically fails the sibling boundary
  test until its URI row is added deliberately.
- **Statement form**: `_type = https://in-toto.io/Statement/v1`; `subject`
  = the entry's `{name: <archive path>, digest: {sha256: <entry sha>}}` —
  digests copied VERBATIM from the dossier manifest, never recomputed here;
  `predicate` = `{"contentType": "<application/x.jsonl | application/json |
  text/x-python | application/octet-stream>", "content": "<the store's
  bytes verbatim as UTF-8 text, or base64 for non-UTF-8>"}`. **The export
  never parses record contents** — the predicate embeds the raw text; no
  `json.loads` anywhere in the module (the dossier no-parse posture
  extends, pinned by the same AST test shape).
- **Unsigned v1, DSSE-ready**: each Statement rides a DSSE envelope
  (`payloadType: application/vnd.in-toto+json`, base64 payload,
  `signatures: []`). Signing is a future concern; the envelope shape means
  adding a signature later changes nothing upstream.
- **The kit assertion** (`conformance/test_attestation_export.py`): an
  exported bundle round-trips through STOCK in-toto tooling — the
  `in-toto-attestation` Python bindings parse every Statement and the
  subject digests verify against the bundled dossier's entries. in-toto is
  a **dev-dep of the kit's CI lane only** (installed in the conformance CI
  job like the plugins job installs jupytext); the test module
  `pytest.importorskip`s it, and it NEVER enters core dependencies.

### D-K5 — self-conformance in CI: the first two certified harnesses ship with the kit

Reference adapters live in `src/hpc_agent/conformance/adapters/`:

- `claude_code.py` — drives the hook modules IN-PROCESS as the harness:
  `write_utterance` builds a `UserPromptSubmit` payload and calls
  `_kernel/hooks/utterance_capture.py::capture`; `answer_question` builds a
  PostToolUse payload for `answer_capture.py::capture`;
  `run_enforcement_point` builds a Stop payload (writing a synthetic
  transcript JSONL) and calls `relay_audit_stop.py::build_hook_output`;
  backgrounding drives the detach machinery's local worker path. No live
  Claude Code needed — the hooks ARE the implementation under test.
- `notebook_render.py` — the SECOND harness, certified FIRST among
  externals: `write_utterance` materializes a rendered `.ipynb` with the
  text typed into a sign-off cell and runs the plugin's
  `notebook-ingest-signoffs` path (lazy plugin import; the CI job installs
  `examples/plugins/hpc-agent-notebook-render` + the render stack). It
  certifies capability 1 (+ the consumer pass) and is honestly PARTIAL on
  capabilities 2–3 — the report says so; the notebook harness has never
  claimed relay enforcement.

CI: a new `conformance` job in `.github/workflows/ci.yml`, plugins-style
ISOLATED (matrix `adapter: [claude-code, notebook-render]`), offline only,
installing in-toto + (for the notebook leg) the plugin and render stack.
Isolation matters for the same reason as the plugins job: installing the
plugin shifts the entry-point registry, which must never leak into the core
`test` matrix.

### D-K6 — publishing mechanics

- **Contract version stamp**: `docs/internals/harness-contract.md` gains a
  SemVer header line (`Contract version: 1.0.0`) and a core constant
  `HARNESS_CONTRACT_VERSION = "1.0.0"` that the `harness-capabilities` verb
  reports. **K10 explicitly OWNS both the constant and the verb's result
  field that carries it** — the concurrent elicitation stream reshaped the
  verb's evidence keys (`docs/design/mcp-elicitation.md` E3-a) but did not
  add this field; the constant's single home is beside the verb in
  `ops/harness_capabilities.py` (which now exists), and the doc stamp and the
  constant are pinned equal by a contract test. The kit stamps its report
  with both the contract version and `hpc_agent.__version__`.
- **What "conforming to v1" claims**: all three capabilities' kit modules
  passed, plus canonicalization and negotiation, against a named
  `hpc-agent` version — stated as "conforming: harness contract v1
  (kit hpc-agent X.Y.Z)". Partial results claim only the named
  capabilities. The report never grades on a curve: skips are listed with
  their contract-named degraded tier.
- **Deprecation posture**: within major 1, changes are ADDITIVE-ONLY — a
  new assertion/fixture may land as a minor bump only if both reference
  adapters stay green (a previously-conforming harness failing a minor is
  the definition of a breaking change → major). The sha canonicalization is
  the canonical major trigger: changing it drift-revokes every stored
  attestation, so it can only ever ship as v2 + `canon_version` on new
  records. Capability REMOVAL never happens; a capability may gain a new
  conforming implementation shape (as ACT did with the gateway) as a minor.
- **Relation to `tests/contracts/test_harness_contract.py`**: the doc-prose
  pins STAY (they are cheap, run in core CI, and guard the document
  itself); the kit ABSORBS their behavioral intent (the frozen schema is
  deliberately double-covered — the doc says it, the kit proves it). The
  registry pin `test_no_utterance_writing_verb_in_registry` stays core: it
  guards our registry, not a harness. K10 adds the version-stamp pin there.

## Task waves (file-disjoint, Opus-sized)

**Inputs from the concurrent stream (now landed):** the
`harness-capabilities` verb (`ops/harness_capabilities.py`, in the registry)
+ the MCP-elicitation sections of `harness-contract.md` (ba83eac3). K6 and
K10 consume them; everything else does not.

Wave A (parallel):

- **K1** — `src/hpc_agent/conformance/` skeleton: `adapter.py` (the exact
  Protocol above, stdlib-only), `conftest.py` (`--harness-adapter` loading,
  per-capability skip machinery, the conformance report summary hook),
  fixture-repo builder (a temp experiment dir with a claimed journal
  namespace, honoring `HPC_JOURNAL_DIR`). Unit tests for the kit machinery
  itself in `tests/conformance_kit/` (the kit is code; it gets tests).
  Packaging: fixtures as package data in `pyproject.toml`.
- **K2** — canonicalization fixtures + `conformance/test_canonicalization.py`
  (the JCS-divergence vectors, `normalize_source` vectors, expected-digest
  files).
- **K3** — `ops/export_attestations.py` + `_wire/actions/export_attestations.py`
  + `tests/contracts/test_attestation_export_boundary.py` (predicateType
  map derived from `DOSSIER_SOURCES`, equality-pinned; the no-`json.loads`
  AST pin; Statement/DSSE shape pins; the delegate-to-`export_dossier`
  one-gather pin). Regen tail (below) belongs to this task.

Wave B (after A, parallel):

- **K4** — `conformance/test_capability_utterance_log.py` (all cap-1
  assertions incl. the consumer-defined authorship-gate pass, both
  directions).
- **K5** — `conformance/test_capability_relay.py` +
  `conformance/fixtures/relay/` triples (all contradiction kinds, notebook
  claims, loop-safety, fail-open).
- **K6** — `conformance/test_capability_backgrounding.py` + the stub worker
  fixture, and `conformance/test_negotiation.py`
  (declared==detected==behaved; consumes the E3-a-reshaped
  `harness-capabilities` result — `ops/harness_capabilities.py`, present).
- **K7** — `conformance/test_attestation_export.py` (stock in-toto
  round-trip, `importorskip`-guarded; needs K3).

Wave C (sequential — hot/shared files):

- **K8** — reference adapters `conformance/adapters/claude_code.py` +
  `notebook_render.py` (each verified by running the kit against itself
  locally).
- **K9** — the `conformance` CI job in `.github/workflows/ci.yml`
  (plugins-style isolation, offline, matrix of the two adapters, in-toto +
  render-stack installs).
- **K10** — publishing: version stamp on `harness-contract.md` (the
  elicitation sections have LANDED, ba83eac3 — re-read before stamping);
  **K10 OWNS adding the `HARNESS_CONTRACT_VERSION` constant in
  `ops/harness_capabilities.py` and the `harness-capabilities` result field
  that carries it** (E3-a left the result shape open for this additive
  field); the `HARNESS_CONTRACT_VERSION` pin test added to
  `tests/contracts/test_harness_contract.py`, README/docs pointers, and the
  MCP curated-catalog decision for `export-attestations` (expose it beside
  `export-dossier`'s posture).

**Regen / inventory tails** (K3 and K10 own them): `export-attestations` is
a new `@primitive` → `python scripts/bake_operations_json.py --write` +
schema regen + the registry-count contract tests move; MCP server curated
catalog updated if exposed; `pyproject.toml` package-data for
`conformance/fixtures/**`; CI matrix docs.

## Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| Core never imports the conformance kit (pytest stays out of runtime) | new `tests/contracts/test_conformance_kit_boundary.py` | any module under `src/hpc_agent/` outside `conformance/` imports `hpc_agent.conformance`, or `conformance/adapter.py` imports pytest |
| The attestation export never parses record contents | `tests/contracts/test_attestation_export_boundary.py` (AST, the dossier no-parse pin shape) | `json.load`/`json.loads` appears in `ops/export_attestations.py` |
| predicateType vocabulary is closed and derived from `DOSSIER_SOURCES` | same boundary test, equality pin | the URI map and `DOSSIER_SOURCES` diverge in either direction |
| One gather definition — the export delegates to `export_dossier` | same boundary test | `ops/export_attestations.py` re-walks any store path instead of consuming the dossier result |
| in-toto never enters core deps | existing `tests/contract/test_no_heavy_toplevel_imports.py` posture + a named dep-list assert | `in-toto`/`in-toto-attestation` appears in `pyproject.toml` dependencies or a non-kit extra |
| Doc stamp == reported contract version | `tests/contracts/test_harness_contract.py` (K10 addition) | `harness-contract.md`'s version line and `HARNESS_CONTRACT_VERSION` disagree |
| Kit fixtures derive injection tags from the exported filter | kit-internal test in `tests/conformance_kit/` | provenance fixtures hard-code a tag list instead of deriving from `HARNESS_INJECTION_RE` |
| Both reference adapters stay conforming (self-conformance) | the `conformance` CI job | any push makes the Claude Code hooks or the notebook plugin fail the kit — i.e. the contract drifted from the code |

## Boundary-drift flags (Q1 watch list)

- **The kit never weakens a filter to admit a harness.** The fixtures are
  normative: an adapter failing the provenance assertions is
  NON-CONFORMING; `HARNESS_INJECTION_RE` / `_is_clicked` never grow
  harness-specific carve-outs to make a candidate pass. Pressure to soften
  here is the feature working.
- **Adapters never run in core CI with network.** The conformance job is
  offline like the plugins job; a reference adapter that needs a live
  harness, a token, or a socket has crossed the lane.
- **No self-asserted capability manifests — detection only.** The adapter
  declares by implementing; the verb detects from the environment; the kit
  proves by behavior. A `capabilities:` field on an adapter (or a config
  file the harness writes about itself) is exactly the guard-the-LLM-
  satisfies failure shape, one level up.
- **The export never parses record contents.** Subject digests are copied
  from the dossier manifest; predicates embed verbatim bytes. The moment
  `export_attestations` reads a field out of a record it is interpreting
  the trail it seals (the dossier's Q1 line, extended).
- **The kit asserts outcomes, never mechanisms.** `EnforcementOutcome` and
  `WakeEvent` are the seam; a kit test that inspects HOW an adapter blocked
  (hook JSON shapes, gateway internals) couples the contract to one
  implementation and forfeits the two-shapes-of-ACT decision.
- **Skips stay honest.** A partial harness is reported partial with the
  contract-named degraded tier — the kit never rounds partial up to
  conforming, and never invents a tier name the contract lacks.
