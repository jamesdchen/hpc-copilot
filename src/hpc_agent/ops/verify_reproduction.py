"""``verify-reproduction`` — compare a reproduction run's metrics to the original.

The COMPARISON half of the reproduction receipt
(``docs/design/reproduction-receipt.md``). Given a reproduction run whose
sidecar ``reproduces`` link names the original, load each run's reduced metrics
via the artifact ladder, compare them per-key under a caller-owned tolerance,
fold a single verdict, and append a durable, self-contained receipt line to
``_aggregated/<repro_run_id>/reproduction_receipts.jsonl``.

The comparator carries **NO metric vocabulary** — it never names a metric,
never privileges one, never picks a tolerance. It compares opaque numbers with
equality-within-tolerance (or exact equality when no tolerance is supplied),
and everything else (non-numeric values, NaN, keys present on one side only) is
``incomparable`` rather than a raw ``!=`` surprise. ``n_samples`` compares like
any other number — there is no metric-name special-casing anywhere.

A mismatch or an incomparable is a SUCCESSFUL run (exit-0, ``needs_decision``):
a discovered nondeterminism is the feature working, never an error. The verb
raises only when the pair is not a genuine reproduction (the ``reproduces`` link
does not name the original) or a run's identity sidecar is missing.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.verify_reproduction import (
    ReproTolerance,
    VerifyReproductionResult,
    VerifyReproductionSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.execution.mapreduce.reduce.metrics import reduce_partials
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.runs import read_run_sidecar

if TYPE_CHECKING:
    from collections.abc import Mapping

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

#: Receipt record schema version (append-only ledger; bump on shape change).
RECEIPT_SCHEMA_VERSION = 1


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


def _flatten_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a nested metrics mapping to scalar leaves, joining keys with ``.``.

    Both artifact-ladder rungs yield the reducer's ``{grid_key: {metric: value}}``
    shape (see :func:`reduce_partials` and the combiner's ``metrics_aggregate.json``
    ``aggregated_metrics``). This flattens ONE uniform way — recursing into dict
    values only — so a single-grid-point run becomes ``{"<grid>.<metric>": v}`` and
    an already-flat dict stays flat. Non-dict leaves (numbers, strings, lists) are
    preserved RAW — no reduction — so the comparator sees non-numeric values and
    applies its equality/incomparable rules rather than dropping them.
    """
    flat: dict[str, Any] = {}
    for key, value in metrics.items():
        skey = str(key)
        if isinstance(value, dict):
            for sub_key, sub_val in _flatten_metrics(value).items():
                flat[f"{skey}.{sub_key}"] = sub_val
        else:
            flat[skey] = value
    return flat


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
                return _flatten_metrics(aggregated), str(metrics_aggregate)

    # Rung 2 — reduce the per-wave partials locally with the shared reducer.
    if combiner_dir.is_dir():
        reduced = reduce_partials(combiner_dir)  # {grid_key: metrics}, {} when no waves
        if reduced:
            return _flatten_metrics(reduced), str(combiner_dir)

    # Rung 3 — nothing to compare on this side.
    return (
        None,
        f"no metrics artifact for run {run_id!r} "
        f"(looked for {metrics_aggregate} and {combiner_dir}/wave_*.json)",
    )


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


def _identity(sidecar: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    """Lift a run's identity off its sidecar VERBATIM (never re-derived)."""
    ident: dict[str, Any] = {"run_id": run_id}
    for field in _IDENTITY_FIELDS:
        ident[field] = sidecar.get(field)
    return ident


def _receipt_path(experiment_dir: Path, repro_run_id: str) -> Path:
    """Append-only receipts ledger, beside the metrics it verdicts."""
    return experiment_dir / "_aggregated" / repro_run_id / "reproduction_receipts.jsonl"


def _append_receipt(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line under an exclusive flock + fsync (append-only).

    The harvest-ledger / decision-brief idiom: advisory-``flock``-serialized so
    two concurrent verifications cannot interleave a torn line, ``fsync``-ed so
    the durable scientific record survives a crash. No dedup — each verification
    is its own event, so a re-verify appends a SECOND line.
    """
    from hpc_agent.infra.io import advisory_flock

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    lock = path.with_suffix(path.suffix + ".lock")
    with advisory_flock(lock), path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        with contextlib.suppress(OSError):
            os.fsync(fh.fileno())


@primitive(
    name="verify-reproduction",
    verb="query",
    side_effects=[
        SideEffect(
            "filesystem",
            "<experiment>/_aggregated/<repro_run_id>/reproduction_receipts.jsonl (append-only)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=False,  # append-only receipt: each verification accretes a line
    idempotency_key=None,
    agent_facing=True,
    cli=CliShape(
        help=(
            "Compare a reproduction run's reduced metrics against the original it "
            "names (sidecar `reproduces` link), under a caller-owned tolerance, and "
            "append a durable receipt. A mismatch/incomparable is a FINDING "
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
    """Verdict + durable receipt for a reproduction pair.

    Refuses (``SpecInvalid``) when the pair is not a genuine reproduction — the
    reproduction run's sidecar ``reproduces`` field must name ``original_run_id``
    — or when either run's identity sidecar is missing. Otherwise it always
    succeeds (exit-0): a mismatch or incomparable is a ``needs_decision`` finding,
    never an error.
    """
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

    # Load each run's reduced metrics via the artifact ladder.
    orig_metrics, orig_source = _load_run_metrics(experiment_dir, original_run_id)
    repro_metrics, repro_source = _load_run_metrics(experiment_dir, repro_run_id)

    if orig_metrics is None or repro_metrics is None:
        # A missing metrics artifact is an incomparable FINDING (not an error):
        # the run may simply not have been aggregated yet.
        per_key: list[dict[str, Any]] = []
        overall = "incomparable"
        missing = []
        if orig_metrics is None:
            missing.append(f"original [{orig_source}]")
        if repro_metrics is None:
            missing.append(f"repro [{repro_source}]")
        reason = (
            "reproduction verdict: incomparable — missing metrics artifact for "
            + " and ".join(missing)
        )
    else:
        per_key = _compare_metrics(orig_metrics, repro_metrics, spec.tolerance)
        overall = _fold_overall(per_key)
        reason = _render_reason(per_key, overall)

    receipt: dict[str, Any] = {
        "ts": utcnow_iso(),
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "original": _identity(original_sidecar, original_run_id),
        "repro": _identity(repro_sidecar, repro_run_id),
        # Verbatim echo of the caller-owned tolerance (null when exact).
        "tolerance_spec": (
            spec.tolerance.model_dump(mode="json") if spec.tolerance is not None else None
        ),
        "per_key": per_key,
        "overall": overall,
        "sources": {"original_artifact": orig_source, "repro_artifact": repro_source},
    }

    path = _receipt_path(experiment_dir, repro_run_id)
    _append_receipt(path, receipt)

    return VerifyReproductionResult(
        stage_reached=overall,  # type: ignore[arg-type]
        needs_decision=overall != "match",
        reason=reason,
        receipt=receipt,
        receipt_path=str(path),
    )
