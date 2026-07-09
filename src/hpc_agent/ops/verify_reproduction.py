"""``verify-reproduction`` — compare a reproduction run's metrics to the original.

The COMPARISON half of the reproduction receipt
(``docs/design/reproduction-receipt.md``) and the CONSUMER of the determinism
fingerprint (``docs/design/determinism-fingerprint.md`` D-consume). Given a
reproduction run whose sidecar ``reproduces`` link names the original, load each
run's reduced metrics (via the artifact ladder, or per-task for a partial
reproduction), read the experiment's fingerprint LEDGER for prior evidence,
reduce the observed envelope FRESH, classify the comparison into a TIERED
verdict, append a durable receipt, and — judgment always preceding append —
append THIS comparison back as one more fingerprint sample.

The comparator carries **NO metric vocabulary** — it never names a metric,
never privileges one, never invents a tolerance. It compares opaque numbers with
equality-within-tolerance (or exact equality when no measured envelope and no
caller tolerance apply), and everything else (non-numeric values, NaN, keys
present on one side only) is ``incomparable`` rather than a raw ``!=`` surprise.
``n_samples`` compares like any other number — there is no metric-name
special-casing anywhere.

The verdict is three-tiered (design center 3, the D-attention pattern):

* **``auto_cleared``** — every float deviation comfortably inside a
  WELL-EVIDENCED envelope (``n>=3`` + scale + cluster coverage, mechanized never
  judged). A code attestation, zero human attention. An empty-ledger EXACT match
  keeps the historical ``match`` posture (a "pre-fingerprint" comparison behaves
  byte-for-byte as before — no invented tolerance).
* **``needs_verdict``** — a THIN-envelope deviation (either direction), a novel
  scale/cluster, or an ``incomparable`` key: routed to the human WITH the
  code-rendered calibrated evidence (deviation vs envelope at n, scale) — never
  an LLM-authored number.
* **``mismatch``** — a deviation outside a WELL-EVIDENCED envelope, or an
  exact-class key that moved. A FINDING (``needs_decision``, exit-0) — the
  discovered-nondeterminism-is-the-feature posture, byte-preserved.

The n=2 double-canary prior that seeds a ledger is a LABELED prior, not a truth;
the recorded n=2 failure modes (carried verbatim from the design's decision
center 2, and WHY a thin envelope only ever routes to the human):
(1) rare-event nondeterminism looks ``exact`` at n=2 and is not; (2) canary-scale
!= main-scale — BLAS/GPU libraries pick algorithms by problem size, so
canary-scale evidence is THIN for a main-scale verdict; (3) same-node correlated
samples — the double canary's two executions may land on one node/SKU
(``same_submission: true`` records it).

A mismatch or an incomparable is a SUCCESSFUL run (exit-0, ``needs_decision``):
a discovered nondeterminism is the feature working, never an error. The verb
raises only when the pair is not a genuine reproduction (the ``reproduces`` link
does not name the original) or a run's identity sidecar is missing.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.determinism import DeterminismSampleRecord
from hpc_agent._wire.queries.verify_reproduction import (
    ExternalBaseline,
    ReproTolerance,
    VerifyReproductionResult,
    VerifyReproductionSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.execution.mapreduce.reduce.metrics import reduce_partials
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.data_trace import read_trace
from hpc_agent.state.determinism import (
    Sample,
    build_sample_record,
    classify,
    diff_metrics,
    flatten_metrics,
    reduce_envelope,
    validate_sample,
)
from hpc_agent.state.fingerprint_store import (
    append_sample,
    content_sha_over_payloads,
    load_evidence,
    pulls_dir,
)
from hpc_agent.state.runs import read_run_sidecar, resolved_summary_artifact

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

# Identity fields lifted VERBATIM off each run's sidecar into the receipt —
# never re-derived. These are the run's {params, code, env, data, cluster,
# version} fingerprint, read as-is (a sidecar is the source of truth for its
# own identity).
_IDENTITY_FIELDS: tuple[str, ...] = (
    "cmd_sha",
    "tasks_py_sha",
    "env_hash",
    "data_sha",
    "cluster",
    "hpc_agent_version",
    "submitted_at",
)

# The CODE-identity fields the fingerprint ledger keys + filters on (the
# ``state/determinism.py::IDENTITY_FIELDS`` discipline). A drift on any reads
# prior samples STALE. These three are the sample's ``identity`` block — the
# wire ``SampleIdentity`` forbids extra keys, so the data-identity leg
# (Amendment 1) is NOT folded in here until that wire model gains the field.
_FINGERPRINT_IDENTITY_FIELDS: tuple[str, ...] = ("cmd_sha", "tasks_py_sha", "executor")

#: Receipt record schema version (append-only ledger; bump on shape change).
#: A comparison with no fingerprint evidence and no partiality stays v1 (a
#: "pre-fingerprint" receipt, byte-identical to the historical shape); a tiered
#: or partial comparison emits v2 (per-key envelope + partiality accounting).
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION_TIERED = 2

#: Map the overall comparison verdict AT APPEND onto the sample's verdict
#: vocabulary (``auto_cleared`` | ``needs_verdict`` | ``mismatch``). A ``match``
#: (empty-ledger exact) is a PASSING verdict → ``auto_cleared`` (admitted by
#: construction); an ``incomparable`` routes to the human → ``needs_verdict``.
_SAMPLE_VERDICT_MAP: dict[str, str] = {
    "match": "auto_cleared",
    "auto_cleared": "auto_cleared",
    "needs_verdict": "needs_verdict",
    "mismatch": "mismatch",
    "incomparable": "needs_verdict",
}


def _is_number(value: Any) -> bool:
    """True for a real numeric value — ``bool`` excluded (compares by equality).

    ``bool`` is a subclass of ``int``; excluding it keeps ``True``/``False``
    on the non-numeric (equality-only) path rather than silently comparing
    ``True`` as the number ``1`` — the same convention the metrics reducer's
    ``_coerce_weight`` uses.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_nan(value: Any) -> bool:
    """True only for a float NaN (never raises for ints / large values)."""
    return isinstance(value, float) and math.isnan(value)


def _load_run_metrics(experiment_dir: Path, run_id: str) -> tuple[dict[str, Any] | None, str]:
    """Load a run's reduced metrics via the artifact ladder → (flat_metrics, source).

    Ladder (each rung uses the SAME pure reducer the aggregate flow uses, never a
    re-implementation):

    1. ``_aggregated/<run_id>/metrics_aggregate.json`` — the cluster-final /
       default-path aggregate; read its ``aggregated_metrics``.
    2. fallback — ``reduce_partials`` over the already-pulled
       ``_aggregated/<run_id>/_combiner/`` wave files.
    3. else ``(None, <reason naming the missing artifact>)`` — that side is
       ``incomparable``.
    """
    agg_dir = experiment_dir / "_aggregated" / run_id
    metrics_aggregate = agg_dir / "metrics_aggregate.json"
    combiner_dir = agg_dir / "_combiner"

    # Rung 1 — the single reduced-aggregate file.
    if metrics_aggregate.is_file():
        try:
            data = json.loads(metrics_aggregate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            aggregated = data.get("aggregated_metrics")
            if isinstance(aggregated, dict):
                return flatten_metrics(aggregated), str(metrics_aggregate)

    # Rung 2 — reduce the per-wave partials locally with the shared reducer.
    if combiner_dir.is_dir():
        reduced = reduce_partials(combiner_dir)  # {grid_key: metrics}, {} when no waves
        if reduced:
            return flatten_metrics(reduced), str(combiner_dir)

    # Rung 3 — nothing to compare on this side.
    return (
        None,
        f"no metrics artifact for run {run_id!r} "
        f"(looked for {metrics_aggregate} and {combiner_dir}/wave_*.json)",
    )


# --- partial (per-task) load path (design center 5) --------------------------
#
# A partial reproduction compares PER-TASK, never pooled-vs-subset — the
# artifact ladder above loads REDUCED aggregates, which is wrong for a subset.
# This is the NEW named load path the design pins so it isn't improvised: each
# side's per-task ``metrics.json`` for the compared indices is loaded LOCALLY
# when present (under ``_aggregated/<run_id>/_per_task/<idx>/metrics.json``),
# else via the remote filtered-pull seam (T6-era). Each task's leaves are
# prefixed ``task<idx>.`` so the SAME opaque comparator compares them honestly.


def _partial_dir(experiment_dir: Path, run_id: str) -> Path:
    """``_aggregated/<run_id>/_per_task/`` — the local per-task metrics home."""
    return experiment_dir / "_aggregated" / run_id / "_per_task"


def _partial_indices(sidecar: Mapping[str, Any]) -> list[int] | None:
    """The compared task indices a partial reproduction recorded on its sidecar.

    T6 records ``task_sample`` (the derived-or-caller subset) on the reproduction
    sidecar; this reads it back so T5 can compare per-task. It rides the
    sanctioned ``extra`` free-form pocket (``extra["task_sample"]``) — the
    schema-stable seam for run-scoped metadata pre-T6 — so this read never
    references an unwritten first-class sidecar key. Returns ``None`` when
    absent/empty (a FULL reproduction) or malformed (bools/non-ints refuse the
    partial path rather than guessing).
    """
    extra = sidecar.get("extra")
    raw = extra.get("task_sample") if isinstance(extra, dict) else None
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    indices: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        indices.append(value)
    return indices


def _pull_partial_task_metrics(
    experiment_dir: Path, run_id: str, task_index: int
) -> dict[str, Any] | None:
    """Remote filtered-pull seam for one task's ``metrics.json`` (named, not improvised).

    The ``ops/aggregate_flow.py::_per_task_metrics_reduce`` idiom (``rsync_pull``
    a single task's ``metrics.json`` when it isn't already local). T5 implements
    the LOCAL leg only; the remote pull lands with T6 (it needs the run's journal
    record for the ssh target). Returns ``None`` (not available) so the local
    path is authoritative and the missing task is counted UNCOMPARED — never
    silently dropped.
    """
    return None


def _load_partial_side(
    experiment_dir: Path, run_id: str, indices: Sequence[int], *, filename: str
) -> tuple[dict[str, Any], list[int]]:
    """Load one side's per-task metrics for *indices* → (flat_metrics, present).

    Each present task's leaves are prefixed ``task<idx>.`` so the comparator sees
    per-task keys. ``present`` is the subset of *indices* that yielded a readable
    summary file (local, else the remote seam) — the rest are UNCOMPARED.

    ``filename`` is the side's declared per-task summary filename (F-J),
    resolved by the caller at the seam from that run's sidecar via
    ``resolved_summary_artifact`` (absent/blank → ``metrics.json``). Each side is
    resolved independently, so an original written before the field existed still
    reads ``metrics.json`` while its reproduction can key on e.g.
    ``results_reduce.json``.
    """
    flat: dict[str, Any] = {}
    present: list[int] = []
    for idx in indices:
        path = _partial_dir(experiment_dir, run_id) / str(idx) / filename
        payload: Any = None
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
        if payload is None:
            payload = _pull_partial_task_metrics(experiment_dir, run_id, idx)
        if not isinstance(payload, dict):
            continue
        present.append(idx)
        for key, value in flatten_metrics(payload).items():
            flat[f"task{idx}.{key}"] = value
    return flat, present


# --- the data-trace fingerprint interlock (docs/design/data-trace.md) --------
#
# "The fingerprint interlock": stage digests are fingerprint-admissible
# evidence. When BOTH compared runs carry ingested traces, the per-stage
# ``digest`` + ``row_count`` atoms fold into the compared metrics payloads as
# EXACT-CLASS keys (``stage:<stage>.digest`` / ``stage:<stage>.row_count``), so
# they ride the SAME per-key envelope + sample machinery with NO new admission
# rule — and a reproduction mismatch LOCALIZES to a named stage. Absent traces
# on either side → nothing folded, disclosed (never fabricated): the
# degradation-path posture.

#: The flattened-key prefix for a folded per-stage atom (flatten convention: a
#: ``.`` joins the stage-namespaced key to the atom name).
_STAGE_KEY_PREFIX = "stage:"


def _read_run_trace(experiment_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """The run's task-0 stage trace — the interlock's localization unit.

    v1 reads the ``("run", run_id)`` task-0 trace (the canonical single-/first-
    task trace the design's "diverges at <stage>" example names); multi-task
    enumeration is a deferred refinement (drift-logged). Returns ``[]`` when the
    run has no ingested trace — the degradation path (no trace → nothing folded,
    disclosed).
    """
    return read_trace(experiment_dir, "run", run_id, 0)


def _stage_atoms(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce a trace to ``{stage: {seq, digest, rows}}`` — the interlock evidence.

    Only the two fingerprint-admissible atoms (the ``digest`` sha + the
    ``row_count`` row total) are lifted; a stage seen more than once keeps its
    LAST record (append order). A malformed/absent atom degrades to ``None``
    (disclosed, never fabricated) so an off-digest-policy run still folds its
    row counts.
    """
    out: dict[str, dict[str, Any]] = {}
    for rec in records:
        stage = rec.get("stage")
        if not isinstance(stage, str) or not stage:
            continue
        atoms = rec.get("atoms")
        atoms = atoms if isinstance(atoms, dict) else {}
        digest = atoms.get("digest")
        digest = digest if isinstance(digest, str) and digest else None
        rc = atoms.get("row_count")
        rows_raw = rc.get("rows") if isinstance(rc, dict) else None
        rows = rows_raw if isinstance(rows_raw, int) and not isinstance(rows_raw, bool) else None
        seq = rec.get("seq")
        out[stage] = {
            "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
            "digest": digest,
            "rows": rows,
        }
    return out


def _stage_overlay(stage_atoms: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Flatten stage atoms into ``stage:<stage>.{digest,row_count}`` metric keys.

    The namespaced, flatten-convention-consistent keys the interlock folds into
    the compared payloads. Exact-class by construction: a digest is a sha (str),
    a row_count is an int — both always compare exactly (no envelope needed).
    """
    overlay: dict[str, Any] = {}
    for stage, a in stage_atoms.items():
        if a.get("digest") is not None:
            overlay[f"{_STAGE_KEY_PREFIX}{stage}.digest"] = a["digest"]
        if a.get("rows") is not None:
            overlay[f"{_STAGE_KEY_PREFIX}{stage}.row_count"] = a["rows"]
    return overlay


def _first_diverged_stage(
    orig_stages: Mapping[str, Mapping[str, Any]],
    repro_stages: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """The FIRST stage (by pipeline order = trace ``seq``) whose atoms diverge.

    A stage present on ONE side only is a structural divergence; otherwise a
    ``digest`` that moved (recorded on both) or a ``row_count`` that moved. A
    digest recorded on only one side is NOT called a divergence — that is a
    degraded observation, not proven drift. Returns ``None`` when every shared
    stage agrees.
    """
    names = set(orig_stages) | set(repro_stages)

    def _order(name: str) -> tuple[int, str]:
        seqs = [
            s["seq"]
            for s in (orig_stages.get(name), repro_stages.get(name))
            if s is not None and isinstance(s.get("seq"), int)
        ]
        return (min(seqs) if seqs else 2**63, name)

    for name in sorted(names, key=_order):
        o = orig_stages.get(name)
        r = repro_stages.get(name)
        if o is None or r is None:
            return name
        digest_div = (
            o.get("digest") is not None
            and r.get("digest") is not None
            and o["digest"] != r["digest"]
        )
        rows_div = (
            o.get("rows") is not None and r.get("rows") is not None and o["rows"] != r["rows"]
        )
        if digest_div or rows_div:
            return name
    return None


# --- the byte-preserved v1 comparator (NO metric vocabulary) -----------------


def _resolve_key_tol(
    tolerance: ReproTolerance | None, key: str
) -> tuple[float | None, float | None, bool]:
    """Resolve the effective ``(abs_tol, rel_tol, supplied)`` for one metric key.

    A ``per_key`` entry FULLY replaces the default for that key (even an
    all-absent entry → exact for that key). ``supplied`` is False when both
    resolved bounds are absent — that key is compared EXACTLY.
    """
    if tolerance is None:
        return None, None, False
    override = tolerance.per_key.get(key)
    if override is not None:
        abs_tol, rel_tol = override.abs_tol, override.rel_tol
    else:
        abs_tol, rel_tol = tolerance.default_abs_tol, tolerance.default_rel_tol
    supplied = abs_tol is not None or rel_tol is not None
    return abs_tol, rel_tol, supplied


def _compare_metrics(
    orig: Mapping[str, Any],
    repro: Mapping[str, Any],
    tolerance: ReproTolerance | None,
) -> list[dict[str, Any]]:
    """Pure per-key comparator → one verdict dict per key (union of both sides).

    Rules:

    * key present on ONE side only → ``incomparable``.
    * numeric vs numeric → tolerance compare (exact ``==`` when no tolerance);
      NaN on either side → ``incomparable`` (never a raw ``!=``).
    * non-numeric (either side) → equality only; a tolerance SUPPLIED for a
      non-numeric key → ``incomparable`` for that key.

    Each verdict dict: ``{key, original, repro, abs_diff, rel_diff, verdict,
    tolerance_applied}``.
    """
    verdicts: list[dict[str, Any]] = []
    for key in sorted(set(orig) | set(repro)):
        o_present, r_present = key in orig, key in repro
        entry: dict[str, Any] = {
            "key": key,
            "original": orig.get(key) if o_present else None,
            "repro": repro.get(key) if r_present else None,
            "abs_diff": None,
            "rel_diff": None,
            "verdict": "incomparable",
            "tolerance_applied": None,
        }

        if not (o_present and r_present):
            # Present on one side only — no honest comparison.
            verdicts.append(entry)
            continue

        o_val, r_val = orig[key], repro[key]
        abs_tol, rel_tol, supplied = _resolve_key_tol(tolerance, key)
        entry["tolerance_applied"] = {"abs_tol": abs_tol, "rel_tol": rel_tol} if supplied else None

        if _is_number(o_val) and _is_number(r_val):
            if _is_nan(o_val) or _is_nan(r_val):
                # NaN is never equal to anything, including itself — refuse the
                # raw ``!=`` surprise and call it incomparable.
                verdicts.append(entry)
                continue
            o_f, r_f = float(o_val), float(r_val)
            abs_diff = abs(o_f - r_f)
            denom = max(abs(o_f), abs(r_f))
            rel_diff = abs_diff / denom if denom else 0.0
            entry["abs_diff"] = abs_diff
            entry["rel_diff"] = rel_diff
            if supplied:
                matched = (abs_tol is not None and abs_diff <= abs_tol) or (
                    rel_tol is not None and rel_diff <= rel_tol
                )
            else:
                matched = o_f == r_f
            entry["verdict"] = "match" if matched else "mismatch"
        else:
            # Non-numeric on at least one side: equality only. A tolerance
            # supplied for a non-numeric key is meaningless → incomparable.
            if supplied:
                entry["verdict"] = "incomparable"
            else:
                entry["verdict"] = "match" if o_val == r_val else "mismatch"
        verdicts.append(entry)
    return verdicts


def _fold_overall(verdicts: list[dict[str, Any]]) -> str:
    """Fold per-key verdicts into one: mismatch > incomparable > match.

    Empty (no comparable keys) folds to ``incomparable`` — a "reproduction"
    with nothing to compare is not a proven match.
    """
    if not verdicts:
        return "incomparable"
    kinds = {e["verdict"] for e in verdicts}
    if "mismatch" in kinds:
        return "mismatch"
    if "incomparable" in kinds:
        return "incomparable"
    return "match"


def _render_reason(verdicts: list[dict[str, Any]], overall: str) -> str:
    """Code-rendered one-line summary of the comparison counts + verdict."""
    if not verdicts:
        return (
            f"reproduction verdict: {overall} — both runs loaded but produced "
            "no comparable metric keys"
        )
    n_match = sum(1 for e in verdicts if e["verdict"] == "match")
    n_mismatch = sum(1 for e in verdicts if e["verdict"] == "mismatch")
    n_incomparable = sum(1 for e in verdicts if e["verdict"] == "incomparable")
    total = len(verdicts)
    return (
        f"reproduction verdict: {overall} — {n_match} matched, "
        f"{n_mismatch} mismatched, {n_incomparable} incomparable of "
        f"{total} metric key{'s' if total != 1 else ''}"
    )


def _render_tiered_reason(stage: str, per_key: list[dict[str, Any]]) -> str:
    """Code-rendered summary for a tiered (fingerprint-consulted) comparison.

    Names the tier verdict + the per-tier key counts + the calibrated spread of
    the worst deviation vs its envelope — every number is READ off the receipt
    keys, never LLM-authored (the D6 archive/interface split).
    """
    n_auto = sum(
        1 for e in per_key if e.get("tier_reason") in ("exact", "within_evidenced_envelope")
    )
    n_thin = sum(
        1
        for e in per_key
        if e.get("tier_reason") in ("within_thin_envelope", "outside_thin_envelope")
    )
    n_incomp = sum(1 for e in per_key if e.get("verdict") == "incomparable")
    total = len(per_key)
    detail = ""
    # Surface the widest evidenced deviation as the calibrated brief number.
    worst: dict[str, Any] | None = None
    for e in per_key:
        env = e.get("envelope_applied")
        if not isinstance(env, dict) or e.get("rel_diff") is None:
            continue
        if worst is None or (e["rel_diff"] or 0.0) > (worst.get("rel_diff") or 0.0):
            worst = e
    if worst is not None and isinstance(worst.get("envelope_applied"), dict):
        env = worst["envelope_applied"]
        ev = env.get("evidence", {})
        detail = (
            f"; worst {worst['key']}: rel_diff={worst.get('rel_diff')} vs "
            f"[{env.get('lo')}, {env.get('hi')}] (n={ev.get('n')}, "
            f"scales={ev.get('scales')})"
        )
    return (
        f"reproduction verdict: {stage} — {n_auto} auto-clearable, {n_thin} thin/novel, "
        f"{n_incomp} incomparable of {total} metric key{'s' if total != 1 else ''}{detail}"
    )


def _data_dimension_phrase(excluded_data_drift: int, data_identity_unknown: int) -> str:
    """Code-rendered clause NAMING the data dimension (amendment leg 2), or ``""``.

    The verdict states which dimension moved: prior samples EXCLUDED as data drift
    (a different data identity — a rebuilt input, not nondeterminism), and priors
    with NO recorded manifest counted UNKNOWN ("data identity unknown, no manifest
    at record time") — disclosed, never blocking, never fabricated. Both counts
    only arise when the CURRENT comparison's data identity is KNOWN (the data leg
    was applied); empty string when the data leg had nothing to say, so a
    no-manifest verify stays byte-identical to a pre-amendment one.
    """
    parts: list[str] = []
    if excluded_data_drift:
        parts.append(
            f"{excluded_data_drift} prior sample"
            f"{'s' if excluded_data_drift != 1 else ''} excluded as DATA DRIFT "
            "(different data identity — a rebuilt input reads as data drift, not "
            "nondeterminism)"
        )
    if data_identity_unknown:
        parts.append(
            f"{data_identity_unknown} prior sample"
            f"{'s' if data_identity_unknown != 1 else ''} with data identity unknown "
            "(no manifest at record time)"
        )
    if not parts:
        return ""
    return "; data dimension: " + ", ".join(parts)


def _identity(sidecar: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    """Lift a run's identity off its sidecar VERBATIM (never re-derived)."""
    ident: dict[str, Any] = {"run_id": run_id}
    for field in _IDENTITY_FIELDS:
        ident[field] = sidecar.get(field)
    return ident


def _receipt_path(experiment_dir: Path, repro_run_id: str) -> Path:
    """Append-only receipts ledger, beside the metrics it verdicts."""
    return experiment_dir / "_aggregated" / repro_run_id / "reproduction_receipts.jsonl"


def _validate_receipt_partiality(receipt: Mapping[str, Any]) -> None:
    """Refuse a partial receipt that omits any partiality field (no-silent-caps).

    A partial comparison's receipt MUST disclose the exact task indices compared
    and what it did NOT compare (uncompared key/task counts) — a subset receipt
    that prints like a full one is the silent-caps failure this pins. Raises
    :class:`errors.SpecInvalid` naming the missing field.
    """
    if receipt.get("partial") is not True:
        return
    task_indices = receipt.get("task_indices")
    if not isinstance(task_indices, (list, tuple)) or not task_indices:
        raise errors.SpecInvalid(
            "verify-reproduction: a partial receipt must record the exact task_indices "
            f"it compared (no-silent-caps); got {task_indices!r}"
        )
    for field in ("uncompared_keys", "uncompared_tasks"):
        if receipt.get(field) is None:
            raise errors.SpecInvalid(
                f"verify-reproduction: a partial receipt must record {field} "
                "(no-silent-caps); it is null"
            )


def _append_receipt(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line via the ONE shared JSONL-append helper (append-only).

    Re-pointed onto ``infra/io.py::append_jsonl_line`` (the shared flock+fsync
    ledger discipline every append-only store routes through) — never a second
    definition. No dedup: each verification is its own event, so a re-verify
    appends a SECOND line.
    """
    from hpc_agent.infra.io import append_jsonl_line

    append_jsonl_line(path, record)


# --- the fingerprint overlay (D-consume) -------------------------------------


def _adapt_tolerance(
    tolerance: ReproTolerance | None,
) -> Callable[[str], tuple[float | None, float | None] | None] | None:
    """Adapt the caller ``ReproTolerance`` into the classifier's tolerance callable.

    T1's ``classify`` takes ``tolerance(key) -> (abs_tol, rel_tol) | None`` and
    labels a key it decides ``caller_override`` (disclosed). ``None`` (no
    tolerance, or an all-absent per-key entry) leaves the key to the measured /
    exact path.
    """
    if tolerance is None:
        return None

    def _resolver(key: str) -> tuple[float | None, float | None] | None:
        abs_tol, rel_tol, supplied = _resolve_key_tol(tolerance, key)
        if not supplied:
            return None
        return (abs_tol, rel_tol)

    return _resolver


def _load_ledger_evidence(
    experiment_dir: Path, cmd_sha: str, identity: Mapping[str, Any]
) -> tuple[list[Sample], list[bool]]:
    """Read the fingerprint ledger for *cmd_sha* → CURRENT-identity (samples, admitted).

    The store layer (T3) does the tolerant read + CURRENT-identity partition +
    the admission JOIN; this validates each surviving record to a :class:`Sample`
    (dropping any that fail the sample-shape check, keeping the admitted flags
    aligned) so the pure kernel (T1) can reduce the envelope. Returns empty when
    the ledger does not exist (a first, "pre-fingerprint" comparison).
    """
    if not cmd_sha:
        return [], []
    try:
        evidence = load_evidence(experiment_dir, cmd_sha=cmd_sha, identity=identity)
    except errors.SpecInvalid:
        return [], []
    samples: list[Sample] = []
    admitted: list[bool] = []
    for record, flag in zip(evidence.samples, evidence.admitted_flags, strict=False):
        try:
            samples.append(validate_sample(record))
        except errors.SpecInvalid:
            continue
        admitted.append(bool(flag))
    return samples, admitted


def _append_fingerprint_sample(
    experiment_dir: Path,
    *,
    original_run_id: str,
    repro_run_id: str,
    identity: Mapping[str, Any],
    cluster: str | None,
    stage: str,
    orig_metrics: Mapping[str, Any],
    repro_metrics: Mapping[str, Any],
    partial: bool,
    task_indices: list[int] | None,
) -> dict[str, Any] | None:
    """Append THIS comparison back as one more fingerprint sample (best-effort).

    D-consume: after verdicting (judgment ALWAYS precedes append — the envelope
    was reduced from PRIOR evidence only), the comparison becomes a sample. The
    two compared payloads are persisted on disk under the ledger's ``_pulls``
    area so the bind-recompute has artifacts to re-hash; the sample's ``verdict``
    is the comparison's verdict AT APPEND (``auto_cleared`` samples are admitted
    by construction; ``needs_verdict``/``mismatch`` are recorded-but-inadmissible
    until a human ``reproduction-verdict`` acceptance — T12).

    Best-effort: a comparison with no known measuring cluster or an unbuildable
    sample shape mints NO sample (returns ``None``) rather than failing the
    verdict — the receipt is the durable record; the sample is the accreting
    evidence.
    """
    if not cluster:
        return None
    try:
        area = pulls_dir(experiment_dir, repro_run_id)
        area.mkdir(parents=True, exist_ok=True)
        artifact_a = area / "compare_a.json"
        artifact_b = area / "compare_b.json"
        payload_a = dict(orig_metrics)
        payload_b = dict(repro_metrics)
        artifact_a.write_text(json.dumps(payload_a, sort_keys=True), encoding="utf-8")
        artifact_b.write_text(json.dumps(payload_b, sort_keys=True), encoding="utf-8")
        content_sha = content_sha_over_payloads(payload_a, payload_b)
        record = build_sample_record(
            ts=utcnow_iso(),
            content_sha=content_sha,
            identity=dict(identity),
            source="verify-reproduction",
            run_ids=[original_run_id, repro_run_id],
            cluster=cluster,
            scale="main",
            verdict=_SAMPLE_VERDICT_MAP.get(stage, "needs_verdict"),
            per_key=diff_metrics(payload_a, payload_b),
            same_submission=False,
            partial=partial,
            task_indices=task_indices if partial else None,
        )
        append_sample(experiment_dir, record=record, artifact_a=artifact_a, artifact_b=artifact_b)
        return record
    except (errors.SpecInvalid, OSError):
        return None


# --- the claim-check (external-baseline) mode (onboard-by-reproduction 6.5) ---
#
# The onboard-by-reproduction front door: the scientist arrives with a CLAIMED
# result and no recorded original. The claim is the baseline; the comparison runs
# the SAME caller-tolerance comparator; the receipt kind is `claim-check`, NEVER a
# reproduction (ruling 6b, the anti-laundering naming lock). NO fingerprint sample
# is minted — the fingerprint history starts from OBSERVED runs only.

#: The CODE-emitted consistency sentence on a claim-check match. Rendered by code
#: into the receipt and the result reason, relayed VERBATIM by the LLM — the
#: consistency determination is the comparator's (trusted code, caller tolerance
#: as data), never LLM-composed (ruling 6b, user-pinned 2026-07-07).
CLAIM_CONSISTENT_SENTENCE = (
    "the claim is consistent with a fresh observed run (within caller tolerance)"
)


def _assert_receipt_kind_matches_baseline(*, receipt_kind: str, external_baseline: bool) -> None:
    """The anti-laundering seam at the receipt-write boundary (ruling 6b).

    NO code path may write a reproduction-kind receipt with an external baseline:
    a reproduction requires two OBSERVED runs, and labeling a claim-match a
    "reproduction" would launder unattested history into the trust chain (the F1
    class, at the front door). Both the recorded-original path (``reproduction``,
    no external baseline) and the claim-check path (``claim-check``, external
    baseline) route through here, so the violating combination is refused by
    construction. Raises :class:`errors.SpecInvalid` on either incoherent pairing.
    """
    if external_baseline and receipt_kind != "claim-check":
        raise errors.SpecInvalid(
            "verify-reproduction: an external-baseline comparison may write only a "
            f"'claim-check' receipt, never a {receipt_kind!r} receipt — an external "
            "claim was never observed, and calling a claim-match a reproduction would "
            "launder unattested history into the trust chain (onboard-by-reproduction "
            "ruling 6b, the naming lock)."
        )
    if receipt_kind == "claim-check" and not external_baseline:
        raise errors.SpecInvalid(
            "verify-reproduction: a 'claim-check' receipt requires an external "
            "baseline (the human's claim); a recorded-original comparison writes a "
            "'reproduction' receipt."
        )


def _claim_check_receipt_path(experiment_dir: Path, repro_run_id: str) -> Path:
    """Append-only claim-check ledger, beside the fresh run's metrics — NEVER the
    reproduction ledger (the naming lock is enforced at the storage layer too)."""
    return experiment_dir / "_aggregated" / repro_run_id / "claim_check_receipts.jsonl"


def _claim_drift_disclosure(claimed_data_sha: str | None, observed_data_sha: Any) -> str:
    """Code-rendered drift-dimension disclosure for a claim-check non-match.

    Surfaces which identity dimension moved. The rung-0 data coupling: WITH a
    manifest at claim time the brief can name the data dimension; WITHOUT one it
    discloses that result decay and data drift cannot be distinguished.
    """
    if not claimed_data_sha:
        return "cannot distinguish result decay from data drift — no manifest at claim time"
    observed = str(observed_data_sha) if observed_data_sha else ""
    if observed and observed != claimed_data_sha:
        return (
            f"the data changed since the claim (claimed data {claimed_data_sha[:12]}, "
            f"observed {observed[:12]})"
        )
    return (
        f"the data is unchanged since the claim (data {claimed_data_sha[:12]}); the "
        "divergence is in code/env or result decay"
    )


def _render_claim_reason(overall: str, verdicts: list[dict[str, Any]], drift: str | None) -> str:
    """Code-rendered one-line summary of a claim-check comparison (non-match).

    A match's reason is the fixed consistency sentence; a non-match's reason names
    the verdict + the per-key counts + the drift disclosure.
    """
    n_match = sum(1 for e in verdicts if e["verdict"] == "match")
    n_mismatch = sum(1 for e in verdicts if e["verdict"] == "mismatch")
    n_incomparable = sum(1 for e in verdicts if e["verdict"] == "incomparable")
    total = len(verdicts)
    counts = (
        f"{n_match} matched, {n_mismatch} mismatched, {n_incomparable} incomparable of "
        f"{total} claimed key{'s' if total != 1 else ''}"
        if total
        else "no comparable claimed keys"
    )
    tail = f"; {drift}" if drift else ""
    return f"claim-check finding: {overall} — {counts}{tail}"


def _run_claim_check(
    experiment_dir: Path, *, repro_run_id: str, baseline: ExternalBaseline
) -> VerifyReproductionResult:
    """External-baseline (claim-check) comparison — the onboard-by-reproduction mode.

    Compares a FRESH observed run's reduced metrics against the human-authored
    CLAIM under the claim's own caller tolerance. Emits a ``claim-check`` receipt
    that embeds the claim verbatim; mints NO fingerprint sample (observed-runs-only
    lock). A mismatch/incomparable is a dated FINDING (``needs_decision``, exit-0),
    never blocking; a match carries the code-emitted consistency sentence.
    """
    try:
        repro_sidecar = read_run_sidecar(experiment_dir, repro_run_id)
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"fresh run {repro_run_id!r} has no sidecar under "
            f"{experiment_dir}/.hpc/runs/ — a claim-check compares the claim against "
            "an OBSERVED run that was actually submitted."
        ) from exc

    repro_metrics, repro_source = _load_run_metrics(experiment_dir, repro_run_id)

    consistency: str | None = None
    drift: str | None = None
    if repro_metrics is None:
        # No fresh metrics yet — an incomparable FINDING, not an error.
        per_key: list[dict[str, Any]] = []
        overall = "incomparable"
        reason = (
            "claim-check verdict: incomparable — missing metrics artifact for the "
            f"fresh run [{repro_source}]"
        )
    else:
        claim = flatten_metrics(baseline.claimed_values)
        per_key = _compare_metrics(claim, repro_metrics, baseline.tolerance)
        overall = _fold_overall(per_key)
        if overall == "match":
            consistency = CLAIM_CONSISTENT_SENTENCE
            reason = CLAIM_CONSISTENT_SENTENCE
        else:
            drift = _claim_drift_disclosure(
                baseline.claimed_data_sha, repro_sidecar.get("data_sha")
            )
            reason = _render_claim_reason(overall, per_key, drift)

    receipt: dict[str, Any] = {
        "ts": utcnow_iso(),
        "receipt_kind": "claim-check",
        "schema_version": 1,
        # The claim, embedded VERBATIM (ruling 6a — the claim lives in the receipt).
        "claim": {
            "claimed_values": dict(baseline.claimed_values),
            "tolerance": (
                baseline.tolerance.model_dump(mode="json")
                if baseline.tolerance is not None
                else None
            ),
            "claimed_data_sha": baseline.claimed_data_sha,
        },
        "repro": _identity(repro_sidecar, repro_run_id),
        "per_key": per_key,
        "overall": overall,
        "consistency": consistency,
        "drift_disclosure": drift,
        "sources": {"repro_artifact": repro_source},
    }

    # Anti-laundering: a claim-check receipt is the ONLY receipt an external
    # baseline may write (and it requires one). Refuses the launder by construction.
    _assert_receipt_kind_matches_baseline(receipt_kind="claim-check", external_baseline=True)

    path = _claim_check_receipt_path(experiment_dir, repro_run_id)
    _append_receipt(path, receipt)

    return VerifyReproductionResult.model_validate(
        {
            "stage_reached": overall,
            "needs_decision": overall != "match",
            "reason": reason,
            "receipt": receipt,
            "receipt_path": str(path),
            # No fingerprint sample — the observed-runs-only lock (ruling 6b).
            "appended_sample": None,
        }
    )


@primitive(
    name="verify-reproduction",
    verb="query",
    side_effects=[
        SideEffect(
            "filesystem",
            "<experiment>/_aggregated/<repro_run_id>/reproduction_receipts.jsonl (append-only)",
        ),
        SideEffect(
            "filesystem",
            "<experiment>/_aggregated/<repro_run_id>/claim_check_receipts.jsonl "
            "(append-only; external-baseline mode)",
        ),
        SideEffect(
            "filesystem",
            "<experiment>/_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl (append-only sample)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=False,  # append-only receipt + sample: each verification accretes
    idempotency_key=None,
    agent_facing=True,
    cli=CliShape(
        help=(
            "Compare a reproduction run's reduced metrics against the original it "
            "names (sidecar `reproduces` link), reduce the experiment's determinism "
            "envelope, tier the verdict (auto_cleared / needs_verdict / mismatch), "
            "append a durable receipt, and append the comparison back as a "
            "fingerprint sample. A mismatch/incomparable is a FINDING "
            "(needs_decision), never an error."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=VerifyReproductionSpec,
        schema_ref=SchemaRef(input="verify_reproduction"),
    ),
)
def verify_reproduction(
    experiment_dir: Path, *, spec: VerifyReproductionSpec
) -> VerifyReproductionResult:
    """Tiered verdict + durable receipt + accreting sample for a reproduction pair.

    Refuses (``SpecInvalid``) when the pair is not a genuine reproduction — the
    reproduction run's sidecar ``reproduces`` field must name ``original_run_id``
    — or when either run's identity sidecar is missing. Otherwise it always
    succeeds (exit-0): a mismatch, incomparable, or needs_verdict is a
    ``needs_decision`` finding, never an error.

    In external-baseline mode (``spec.external_baseline`` set) the comparison rides
    the SAME spec but the baseline is a human-authored CLAIM, not a recorded run:
    it emits a ``claim-check`` receipt (never a reproduction) and mints no
    fingerprint sample (onboard-by-reproduction, rulings 6a/6b).
    """
    if spec.external_baseline is not None:
        return _run_claim_check(
            experiment_dir,
            repro_run_id=spec.repro_run_id,
            baseline=spec.external_baseline,
        )

    # Recorded-original mode: the validator guarantees original_run_id is present.
    assert spec.original_run_id is not None
    original_run_id = spec.original_run_id
    repro_run_id = spec.repro_run_id

    # Identity sidecars are the source of truth for each run's {params, code,
    # env, data, cluster} fingerprint — a missing one means the run does not
    # exist, which is a genuine bad spec (refuse), NOT an incomparable metrics
    # finding.
    try:
        repro_sidecar = read_run_sidecar(experiment_dir, repro_run_id)
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"reproduction run {repro_run_id!r} has no sidecar under "
            f"{experiment_dir}/.hpc/runs/ — cannot verify a run that was never submitted."
        ) from exc
    try:
        original_sidecar = read_run_sidecar(experiment_dir, original_run_id)
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"original run {original_run_id!r} has no sidecar under "
            f"{experiment_dir}/.hpc/runs/ — cannot verify against a run that does not exist."
        ) from exc

    # The receipt only verdicts a GENUINE reproduction pair: the reproduction
    # run must declare the original via its `reproduces` provenance link.
    reproduces = repro_sidecar.get("reproduces")
    if reproduces != original_run_id:
        raise errors.SpecInvalid(
            f"run {repro_run_id!r} does not name {original_run_id!r} as the run it "
            f"reproduces (its sidecar `reproduces` is {reproduces!r}) — "
            "verify-reproduction only verdicts a genuine reproduction pair. Mint the "
            "reproduction via `reproduce-run` so the provenance link is recorded."
        )

    # Load each run's metrics — FULL via the artifact ladder, or PER-TASK when
    # the reproduction recorded a task subset (design center 5: a subset compares
    # per-task, never pooled-vs-subset).
    indices = _partial_indices(repro_sidecar)
    partial = indices is not None
    compared_indices: list[int] | None = None
    uncompared_keys: int | None = None
    uncompared_tasks: int | None = None
    # The data-dimension disclosure (amendment leg 3) — populated only on the
    # tiered path (an envelope exists to have applied the data leg); stays None on
    # the incomparable / empty-ledger paths.
    data_identity_disclosure: dict[str, Any] | None = None
    # The data-trace interlock disclosure (both/one/neither side traced) and the
    # localized stage — populated only on the full metrics-present path below;
    # stay None on the incomparable / partial paths (nothing folded, nothing to
    # disclose).
    stage_interlock: dict[str, Any] | None = None
    diverged_stage: str | None = None

    if partial:
        assert indices is not None
        # Resolve each side's declared per-task summary filename (F-J) ONCE from
        # the sidecars already read above, and thread it down — the original and
        # its reproduction are resolved independently (an undeclared original
        # stays on metrics.json while a reproduction can key on results_reduce.json).
        orig_flat, orig_present = _load_partial_side(
            experiment_dir,
            original_run_id,
            indices,
            filename=resolved_summary_artifact(original_sidecar),
        )
        repro_flat, repro_present = _load_partial_side(
            experiment_dir,
            repro_run_id,
            indices,
            filename=resolved_summary_artifact(repro_sidecar),
        )
        orig_source = str(_partial_dir(experiment_dir, original_run_id))
        repro_source = str(_partial_dir(experiment_dir, repro_run_id))
        orig_metrics: dict[str, Any] | None = orig_flat if orig_present else None
        repro_metrics: dict[str, Any] | None = repro_flat if repro_present else None
        compared_indices = sorted(set(orig_present) & set(repro_present))
    else:
        orig_metrics, orig_source = _load_run_metrics(experiment_dir, original_run_id)
        repro_metrics, repro_source = _load_run_metrics(experiment_dir, repro_run_id)

    if orig_metrics is None or repro_metrics is None:
        # A missing metrics artifact is an incomparable FINDING (not an error):
        # the run may simply not have been aggregated yet. No envelope, no sample.
        per_key: list[dict[str, Any]] = []
        stage = "incomparable"
        missing = []
        if orig_metrics is None:
            missing.append(f"original [{orig_source}]")
        if repro_metrics is None:
            missing.append(f"repro [{repro_source}]")
        reason = (
            "reproduction verdict: incomparable — missing metrics artifact for "
            + " and ".join(missing)
        )
        schema_version = RECEIPT_SCHEMA_VERSION_TIERED if partial else RECEIPT_SCHEMA_VERSION
        appended_record = None
    else:
        # The data-trace fingerprint interlock (full path only — a partial
        # reproduction already namespaces per task). When BOTH runs carry an
        # ingested trace, fold the per-stage digest/row_count atoms into the
        # compared payloads as EXACT-CLASS keys so they ride the same envelope +
        # sample machinery, and localize the first diverging stage. One side or
        # neither → nothing folded; the presence is disclosed (never fabricated).
        stage_diverged: str | None = None
        if not partial:
            orig_stages = _stage_atoms(_read_run_trace(experiment_dir, original_run_id))
            repro_stages = _stage_atoms(_read_run_trace(experiment_dir, repro_run_id))
            orig_traced = bool(orig_stages)
            repro_traced = bool(repro_stages)
            if orig_traced or repro_traced:
                both_traced = orig_traced and repro_traced
                stage_keys: list[str] = []
                if both_traced:
                    overlay_o = _stage_overlay(orig_stages)
                    overlay_r = _stage_overlay(repro_stages)
                    orig_metrics = {**orig_metrics, **overlay_o}
                    repro_metrics = {**repro_metrics, **overlay_r}
                    stage_keys = sorted(set(overlay_o) | set(overlay_r))
                    stage_diverged = _first_diverged_stage(orig_stages, repro_stages)
                stage_interlock = {
                    "original_trace_present": orig_traced,
                    "repro_trace_present": repro_traced,
                    "compared": both_traced,
                    "stage_keys": stage_keys,
                }

        # v1 comparator (BYTE-PRESERVED, no metric vocabulary) — the base per-key
        # observation + verdict every path starts from.
        per_key_v1 = _compare_metrics(orig_metrics, repro_metrics, spec.tolerance)
        overall_v1 = _fold_overall(per_key_v1)

        if partial:
            assert indices is not None
            assert compared_indices is not None
            uncompared_tasks = len(indices) - len(compared_indices)
            uncompared_keys = len(set(orig_metrics) ^ set(repro_metrics))

        # Fingerprint overlay: reduce the envelope FRESH from PRIOR evidence only
        # (judge-before-append), then tier the verdict via the ONE classifier.
        cmd_sha = str(repro_sidecar.get("cmd_sha") or "")
        cluster = repro_sidecar.get("cluster")
        code_identity = {field: repro_sidecar.get(field) for field in _FINGERPRINT_IDENTITY_FIELDS}
        # Data-identity leg (Phase-3 amendment, ruled 0b): the repro's data
        # identity, lifted from its sidecar's data_manifest_sha. Known → the
        # envelope EXCLUDES cross-data prior samples as data drift (disclosed
        # excluded_data_drift), never admitting a parquet-rebuild sample as
        # nondeterminism; None → the data leg is not applied (unknown, disclosed,
        # never blocking). The SAMPLE this comparison appends carries the leg so a
        # FUTURE comparison can filter on it — code_identity feeds the store-layer
        # read (which keys on the three code fields), the data leg rides separately.
        data_sha_raw = repro_sidecar.get("data_manifest_sha")
        data_sha = str(data_sha_raw) if data_sha_raw else None
        identity = dict(code_identity)
        if data_sha:
            identity["data_sha"] = data_sha
        samples, admitted = _load_ledger_evidence(experiment_dir, cmd_sha, code_identity)
        envelope = reduce_envelope(
            samples, admitted, identity=code_identity, data_identity=data_sha
        )
        # The data-dimension disclosure (amendment leg 2): what the data leg did to
        # the prior evidence — cross-data samples EXCLUDED as drift, or priors with
        # no manifest counted UNKNOWN. Surfaced on the v2 receipt + the reason so
        # the verdict NAMES the data dimension when it moved (never fabricated). The
        # block rides the receipt only when the CURRENT data identity is KNOWN — a
        # no-manifest verify stays byte-identical to a pre-amendment one.
        data_moved = bool(envelope.excluded_data_drift or envelope.data_identity_unknown)
        data_phrase = _data_dimension_phrase(
            envelope.excluded_data_drift, envelope.data_identity_unknown
        )
        if data_sha is not None:
            data_identity_disclosure = {
                "current": data_sha,
                "excluded_data_drift": envelope.excluded_data_drift,
                "data_identity_unknown": envelope.data_identity_unknown,
            }
        diffs = diff_metrics(orig_metrics, repro_metrics)
        classification = classify(
            diffs,
            envelope,
            current_scale="main",
            current_cluster=str(cluster or ""),
            tolerance=_adapt_tolerance(spec.tolerance),
        )

        # A comparison the fingerprint actually judged (a non-empty envelope)
        # runs the TIERED verdict; an empty-ledger comparison keeps the
        # historical v1 posture (no invented tolerance — a "pre-fingerprint"
        # comparison is byte-identical to before). Partiality forces v2 (the
        # receipt must carry the partiality accounting) but reuses the v1
        # verdicts when there is no envelope to consult.
        # data_moved forces v2 even on an empty envelope: when ALL priors were
        # excluded as data drift the envelope is empty (untiered), yet the
        # exclusion is exactly what must be disclosed (a rebuilt input).
        # An active interlock (at least one side traced) FORCES v2 so its
        # stage_interlock disclosure + diverged_stage ride the receipt; an
        # untraced comparison never forces it (byte-identical to a pre-interlock
        # receipt — nothing folded, nothing disclosed).
        tiered = bool(envelope.per_key)
        schema_version = (
            RECEIPT_SCHEMA_VERSION_TIERED
            if (tiered or partial or data_moved or stage_interlock is not None)
            else RECEIPT_SCHEMA_VERSION
        )

        if tiered:
            kv_by_key = {kv.key: kv for kv in classification.per_key}
            per_key = []
            for entry in per_key_v1:
                kv = kv_by_key.get(entry["key"])
                merged = dict(entry)
                if kv is not None:
                    merged["verdict"] = kv.verdict
                    merged["tier_reason"] = kv.tier_reason
                    merged["envelope_applied"] = kv.envelope_applied
                else:
                    merged["tier_reason"] = None
                    merged["envelope_applied"] = None
                per_key.append(merged)
            stage = classification.stage_reached
            reason = _render_tiered_reason(stage, per_key) + data_phrase
        else:
            per_key = []
            for entry in per_key_v1:
                if schema_version == RECEIPT_SCHEMA_VERSION_TIERED:
                    merged = dict(entry)
                    if entry["tolerance_applied"] is not None:
                        merged["tier_reason"] = "caller_override"
                    elif entry["verdict"] == "match":
                        merged["tier_reason"] = "exact"
                    else:
                        merged["tier_reason"] = None
                    merged["envelope_applied"] = None
                    per_key.append(merged)
                else:
                    per_key.append(entry)
            stage = overall_v1
            reason = _render_reason(per_key_v1, overall_v1) + data_phrase

        # Stage-localized mismatch: surface the first diverging stage ONLY when
        # the overall verdict routes to the human (mismatch / needs_verdict /
        # incomparable) — an auto-cleared / exact-match comparison never diverges
        # at a stage (its stage keys matched). Never prose-invented.
        if stage_diverged is not None and stage not in ("match", "auto_cleared"):
            diverged_stage = stage_diverged
            # Code-rendered off the machine field — never prose-invented.
            reason += f"; diverges at stage {diverged_stage!r} (data-trace interlock)"

        appended_record = _append_fingerprint_sample(
            experiment_dir,
            original_run_id=original_run_id,
            repro_run_id=repro_run_id,
            identity=identity,
            cluster=cluster,
            stage=stage,
            orig_metrics=orig_metrics,
            repro_metrics=repro_metrics,
            partial=partial,
            task_indices=compared_indices,
        )

    receipt: dict[str, Any] = {
        "ts": utcnow_iso(),
        "receipt_kind": "reproduction",
        "schema_version": schema_version,
        "original": _identity(original_sidecar, original_run_id),
        "repro": _identity(repro_sidecar, repro_run_id),
        # Verbatim echo of the caller-owned tolerance (null when exact).
        "tolerance_spec": (
            spec.tolerance.model_dump(mode="json") if spec.tolerance is not None else None
        ),
        "per_key": per_key,
        "overall": stage,
        "sources": {"original_artifact": orig_source, "repro_artifact": repro_source},
    }
    if schema_version == RECEIPT_SCHEMA_VERSION_TIERED:
        # v2 partiality accounting (design center 5 — no-silent-caps).
        receipt["partial"] = partial
        receipt["task_indices"] = compared_indices if partial else None
        receipt["uncompared_keys"] = uncompared_keys
        receipt["uncompared_tasks"] = uncompared_tasks
        # v2 data-identity disclosure (amendment leg 3): the current data identity
        # + what the data leg did to the prior evidence. None on an empty-ledger v2
        # (partial-but-untiered) receipt — no envelope applied the data leg.
        receipt["data_identity"] = data_identity_disclosure
        # v2 data-trace interlock (docs/design/data-trace.md): only present when a
        # trace exists on at least one side (else the receipt is byte-identical to
        # a pre-interlock one). ``diverged_stage`` names the first stage a routed
        # verdict localizes to; null otherwise.
        if stage_interlock is not None:
            receipt["stage_interlock"] = stage_interlock
            receipt["diverged_stage"] = diverged_stage

    # No-silent-caps: refuse a partial receipt missing any partiality field.
    _validate_receipt_partiality(receipt)

    # Anti-laundering: the recorded-original path writes a REPRODUCTION receipt and
    # never an external baseline — the guard refuses the launder by construction.
    _assert_receipt_kind_matches_baseline(receipt_kind="reproduction", external_baseline=False)

    path = _receipt_path(experiment_dir, repro_run_id)
    _append_receipt(path, receipt)

    # Echo the appended sample only when it validates against the wire model —
    # a shape the wire cannot represent (e.g. a non-scalar leaf) is still
    # recorded in the ledger by the store, but not surfaced on the result.
    sample_echo: dict[str, Any] | None = None
    if appended_record is not None:
        try:
            DeterminismSampleRecord.model_validate(appended_record)
            sample_echo = appended_record
        except ValidationError:
            sample_echo = None

    # Constructed via ``model_validate`` (a dict payload) rather than keyword
    # args so the result carries the schema_version-2 ``appended_sample`` echo
    # even where a stale installed wire model is on the type-checker's path.
    result_payload: dict[str, Any] = {
        "stage_reached": stage,
        "needs_decision": stage not in ("match", "auto_cleared"),
        "reason": reason,
        "receipt": receipt,
        "receipt_path": str(path),
        "appended_sample": sample_echo,
        "diverged_stage": diverged_stage,
    }
    return VerifyReproductionResult.model_validate(result_payload)
