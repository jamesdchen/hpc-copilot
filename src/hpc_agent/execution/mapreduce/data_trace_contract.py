#!/usr/bin/env python3
"""The data-trace EMISSION CONTRACT — constants shared with the dispatcher.

Wave-1 task **T2** of ``docs/design/data-trace.md``. This module is the
lock-step twin of the ``_EXIT_*`` codes in
:mod:`hpc_agent.execution.mapreduce.dispatch`: it holds the handful of
string constants that must mean the same thing in three places that never
import each other at runtime —

* the standalone cluster-side **dispatcher** (``dispatch.py`` is scp'd to
  the compute node with no ``hpc_agent`` on ``sys.path``, so it hardcodes
  a copy of :data:`TRACE_TRANSPORT_FILENAME` / :data:`TRACE_DIGEST_ENV_VAR`
  with a "kept in lock-step" comment, exactly like ``_EXIT_NO_OUTPUT`` ↔
  ``HPC_DISPATCH_EXIT_NO_OUTPUT`` in ``hpc_preamble.sh``; the export logic
  itself is **T3**'s),
* the pack-side **emitter** (reads :data:`TRACE_DIGEST_ENV_VAR` from its
  env to decide whether to pay for the ``digest`` atom; writes its records
  to :data:`TRACE_TRANSPORT_FILENAME` beside its outputs), and
* the local, package-side **consumers** (the Class-A read helper below;
  **T1**'s ingest/store; **T4**'s harvest pull; **T-R**'s runner) which
  import this module directly.

Design constraints this module honours (so the dispatcher can import it
without dragging ``state``/``ops``):

* **stdlib-only** — no ``hpc_agent`` imports, no third-party deps. It sits
  in the ``execution/mapreduce`` family alongside ``metrics_io`` and
  ``combiner``, all of which are import-safe on a bare cluster node.
* **self-contained** — the constants and the Class-A read helper only; the
  record model, the store layout, ingestion, and the digest CLASSIFIER
  live elsewhere (T1 / T3). ``# T1 seam:`` notes mark where the shapes meet.

No policy lives here. Whether digests are on is T3's classifier; how a
record is shaped is T1's model. This module only pins the NAMES and the
Class-A (authoring, pre-ingestion) read order.
"""

from __future__ import annotations

__all__ = [
    "LOCAL_SCOPE_KIND",
    "READ_STORE",
    "READ_TRANSPORT",
    "RECEIPT_GRADE_SOURCES",
    "TRACE_DIGEST_ENV_VAR",
    "TRACE_SOURCE_DRAFT",
    "TRACE_SOURCE_ENGINE",
    "TRACE_SOURCE_FIELD",
    "TRACE_SOURCE_RUNNER",
    "TRACE_SOURCE_TIERS",
    "TRACE_TRANSPORT_FILENAME",
    "TransportRead",
    "find_freshest_transport",
    "local_scope",
    "read_freshest_transport",
    "read_transport_records",
    "resolve_read_order",
]

import json
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. The transport filename.
# ---------------------------------------------------------------------------
#: The per-task file the emitter writes BESIDE its outputs — "emission is
#: transport" (data-trace.md §Storage). The running process appends
#: ``_trace.jsonl`` wherever its output contract points ($HPC_RESULT_DIR on
#: the cluster, the local output dir for a draft). A packet in flight, never
#: a home: it is disposable after T1 ingests it into the one canonical store.
#:
#: LOCK-STEP: ``dispatch.py`` hardcodes ``_TRACE_TRANSPORT_FILENAME`` with a
#: comment pointing here; ``test_data_trace_contract`` pins the two equal.
TRACE_TRANSPORT_FILENAME = "_trace.jsonl"

# ---------------------------------------------------------------------------
# 2. The digest env-var NAME (no policy — T3 owns the classifier).
# ---------------------------------------------------------------------------
#: The env var the DISPATCHER exports and the pack EMITTER reads to decide
#: whether to compute the (expensive) ``digest`` atom. This module pins only
#: the NAME; the VALUE ("1"/"0") is computed by T3's sidecar-driven digest
#: classifier and exported there. "NO KNOB": code sets it, the human never
#: sees a decision point (data-trace.md §Digest policy). Follows the
#: ``HPC_*`` task-env convention (``HPC_RESULT_DIR``, ``HPC_TASK_ID``, ...).
#:
#: LOCK-STEP: ``dispatch.py`` hardcodes ``_TRACE_DIGEST_ENV_VAR``; pinned by
#: test. T3 wires the actual ``env[...] = ...`` export.
TRACE_DIGEST_ENV_VAR = "HPC_TRACE_DIGESTS"

# ---------------------------------------------------------------------------
# 5. Source-tier constants (Amendment 10 — THE OBSERVER IS THE RUNNER).
# ---------------------------------------------------------------------------
#: The record field naming the emission source's trust tier. Every trace
#: record carries it so a consumer can honour the trust hierarchy without
#: re-deriving provenance.
#:
#: # T1 seam: the record model (state/data_trace.py) includes this field;
#: this is its canonical name.
TRACE_SOURCE_FIELD = "source"

#: Runner-observed (cell boundaries × declared observables). Total coverage
#: by construction — THE ONLY receipt-grade source; the ONLY tier sign-off
#: surfaces and trace-as-receipt ever count (A10 tier 1).
TRACE_SOURCE_RUNNER = "runner"
#: Engine-emitted (the pack wraps its own engines once). A refinement layer:
#: ungameable per-call sub-cell detail, but not receipt-grade (A10 tier 2).
TRACE_SOURCE_ENGINE = "engine"
#: Draft-emitted (``trace.emit`` in the draft). Untrusted annotation —
#: Class-A self-checking convenience only; never enters receipts (A10 tier 3).
TRACE_SOURCE_DRAFT = "draft"

#: The CLOSED set of source tiers, in descending trust order. A record whose
#: ``source`` is outside this set is malformed. Pinned closed by test so a
#: fourth tier can never be added without a reviewed edit here.
TRACE_SOURCE_TIERS: tuple[str, ...] = (
    TRACE_SOURCE_RUNNER,
    TRACE_SOURCE_ENGINE,
    TRACE_SOURCE_DRAFT,
)

#: The tiers receipts / sign-off surfaces are allowed to consume. Runner-tier
#: ONLY (A10/A11): "no code inside the run is trust-bearing". A CONSTANT +
#: docstring here; the CONSUMING logic (filtering a trace to receipt-grade
#: records) is later tasks' — T-R emits these, the sign-off view filters by
#: this set.
RECEIPT_GRADE_SOURCES: tuple[str, ...] = (TRACE_SOURCE_RUNNER,)

# ---------------------------------------------------------------------------
# 3. The local-emission fallback rule (transport-vs-store read order).
# ---------------------------------------------------------------------------
#: The two places a trace can be read from. TRANSPORT = the in-flight
#: ``_trace.jsonl`` beside outputs (pre-ingestion). STORE = the canonical
#: ``.hpc/traces/<scope_kind>/<scope_id>/`` T1 owns (post-ingestion).
READ_TRANSPORT = "transport"
READ_STORE = "store"

#: Scope kind for ad-hoc local runs with neither run_id nor audit_id
#: (Amendment 12 G-c): they trace under ``("local", <cmd_sha12>)`` — see
#: :func:`local_scope`.
LOCAL_SCOPE_KIND = "local"

#: The consumer classes whose read order this rule decides (data-trace.md
#: Amendment 7). Only Class A (authoring) is permitted to read transport
#: directly; every other class reads the store post-ingestion.
_CONSUMER_AUTHORING = "authoring"  # Class A — the builder mid-creation
_CONSUMER_VERIFICATION = "verification"  # Class C — diff/fingerprint/audit/dossier


def resolve_read_order(consumer_class: str, *, is_local_run: bool) -> tuple[str, ...]:
    """Return the ordered sources a consumer should try, freshest first.

    The fallback rule from data-trace.md, as code — a two-axis decision
    table (consumer class × execution locality):

    ======================  ==========  ==========================
    consumer_class          is_local    read order
    ======================  ==========  ==========================
    ``"authoring"`` (A)     True        (TRANSPORT, STORE)
    ``"authoring"`` (A)     False       (TRANSPORT,)
    ``"verification"`` (C)  True        (STORE,)
    ``"verification"`` (C)  False       (STORE,)
    ======================  ==========  ==========================

    Rationale:

    * **Class A reads transport directly** — it is the ONE consumer allowed
      to (Amendment 7): the builder wants "my draft's latest execution",
      freshness = per cell-run, PRE-ingestion. Transport is always tried
      first. On a *local* run the trace also *ingests immediately* (a
      zero-length hop; see :func:`ingests_immediately` semantics), so the
      STORE is a valid fallback once the transport copy has been moved. On a
      *cluster* run nothing is ingested locally until harvest, so the store
      is not yet an option — transport only.
    * **Class C never reads transport** — verification/identity consumers are
      POST-ingestion only (exact keys, sha-bound comparison); they always
      read the store, whether the run was local (immediate ingest) or
      cluster (harvest ingest).

    # T1 seam: STORE reads resolve against
    # ``.hpc/traces/<scope_kind>/<scope_id>/task-<n>.jsonl`` — T1's layout.
    # T4 seam: ``is_local_run`` is the same bit that decides ingest-at-emission
    # (local) vs ingest-at-harvest (cluster).

    Raises ``ValueError`` on an unknown ``consumer_class`` (fail loud — a
    misclassified consumer must not silently get transport access).
    """
    if consumer_class == _CONSUMER_AUTHORING:
        return (READ_TRANSPORT, READ_STORE) if is_local_run else (READ_TRANSPORT,)
    if consumer_class == _CONSUMER_VERIFICATION:
        return (READ_STORE,)
    raise ValueError(
        f"unknown trace consumer_class {consumer_class!r}; "
        f"expected {_CONSUMER_AUTHORING!r} or {_CONSUMER_VERIFICATION!r}"
    )


def local_scope(cmd_sha: str) -> tuple[str, str]:
    """The scope key for an ad-hoc local run (Amendment 12 G-c).

    A local execution with neither ``run_id`` nor ``audit_id`` traces under
    ``("local", <cmd_sha12>)`` — mechanical, collision-free. ``cmd_sha`` is
    truncated to 12 hex chars (the house short-sha width).

    # T1 seam: T1's store keys a trace by ``(scope_kind, scope_id, ...)``;
    # this returns the ``(scope_kind, scope_id)`` pair for that case.
    """
    return (LOCAL_SCOPE_KIND, cmd_sha[:12])


# ---------------------------------------------------------------------------
# 4. The Class-A read helper — freshest transport copy for a draft.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TransportRead:
    """Result of a Class-A transport read.

    * ``path`` — the ``_trace.jsonl`` chosen (mtime-newest under the tree),
      or ``None`` if none was found.
    * ``records`` — the tolerant-parsed records, file order preserved. Each
      record is surfaced AS-IS: its source tier is
      ``record.get(TRACE_SOURCE_FIELD)`` (``runner``/``engine``/``draft``),
      left untouched so the caller applies its own tier policy (e.g. a
      receipt view keeps only :data:`RECEIPT_GRADE_SOURCES`).
    * ``scope`` — the ``(scope_kind, scope_id)`` this read is for, if the
      caller supplied one (passthrough for the T1 ingest that typically
      follows a Class-A read on a local run). Purely informational here.
    """

    path: Path | None
    records: list[dict] = field(default_factory=list)
    scope: tuple[str, str] | None = None


def find_freshest_transport(output_root: Path) -> Path | None:
    """Return the mtime-newest ``_trace.jsonl`` anywhere under *output_root*.

    Class A's lookup is "my draft's latest execution": a draft may write
    several transport files across cell-runs / output subdirs (per-arm dirs,
    ``_wip_*`` staging, nested result trees), so the freshest one wins. Ties
    (identical mtimes) break on the lexicographically-greatest path for
    determinism. Returns ``None`` when the tree holds no transport file or
    the root does not exist.

    Symlinks are followed for files but the walk does not follow directory
    symlinks (``os.walk`` default) — a draft's outputs are a real tree.
    """
    if not output_root.exists():
        return None
    candidates: list[tuple[float, str, Path]] = []
    for path in output_root.rglob(TRACE_TRANSPORT_FILENAME):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # A file that vanished mid-walk / is unreadable is simply not a
            # candidate — never raise while hunting for the freshest copy.
            continue
        candidates.append((mtime, str(path), path))
    if not candidates:
        return None
    # Newest mtime first; break ties on the path string (both descending) so
    # selection is stable across filesystems and runs.
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return candidates[0][2]


def read_transport_records(path: Path) -> list[dict]:
    """Tolerant JSONL read: return the object-records in *path*, in order.

    Never raises. A missing file, an unreadable file, a blank line, a
    non-JSON line, or a JSON value that is not an object is silently
    skipped — a half-written transport file (the process may still be
    appending) must degrade to "the records I could parse", never an
    exception in the drafting agent's inner loop. Mirrors the fall-through
    posture of ``infra/io._read_json_doc``.

    # T1 seam: T1 owns record VALIDATION (schema_version, atom shapes). This
    # helper only guarantees "each element is a JSON object"; a record that
    # parses but is semantically malformed is surfaced for T1 to reject.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def read_freshest_transport(
    output_root: Path, *, scope: tuple[str, str] | None = None
) -> TransportRead:
    """The Class-A read helper: freshest transport trace for a draft.

    Given a draft's output/working tree (and optionally its scope), find the
    mtime-newest :data:`TRACE_TRANSPORT_FILENAME` under it, tolerant-read its
    records, and return them with their source tiers surfaced as-is. This is
    the ONE sanctioned direct transport read (Amendment 7, Class A) — the
    drafting agent reading its own fresh receipts, PRE-ingestion, to correct
    against facts instead of beliefs (Amendment 9).

    Returns an empty :class:`TransportRead` (``path=None``) when the tree
    holds no transport file yet — a draft that has not run emits nothing;
    the caller renders an empty waterfall, never an error.
    """
    path = find_freshest_transport(output_root)
    if path is None:
        return TransportRead(path=None, records=[], scope=scope)
    return TransportRead(path=path, records=read_transport_records(path), scope=scope)
