"""``classify-axis-auto``: one composite verb for the deterministic head of
the ``hpc-classify-axis`` skill (WS Surface 3 — incident 3).

The bug this kills: an autonomous agent hand-sequenced
preflight → easy → record and mislabelled the strict
``preflight``-produces-the-source-path → ``easy``-consumes-it dependency
as "in parallel". The sequence is deterministic, so it belongs in code.

This primitive collapses the chain into ONE call. It imports and calls
the three functions DIRECTLY (no subprocess fan-out):

* :func:`hpc_agent.ops.classify_axis_preflight.classify_axis_preflight`
  — ``discover-runs`` + cache-check + (conditional) ``recall``.
* :func:`hpc_agent.incorporation.classify_axis_easy.classify_axis_easy`
  — the stdlib AST fast-path matcher.
* :func:`hpc_agent.incorporation.classify_axis.classify_axis`
  — the recorder that writes ``<experiment>/.hpc/axes.yaml``.

Internal sequence
-----------------

1. **preflight** → resolve the single ``@register_run`` from
   ``discover_runs.envelope.data.runs`` (caller scope if ``run_name``
   given; else the exactly-one run; else ``SpecInvalid`` ``ambiguous_run``).
   Capture ``run_name`` / ``source_path`` (the row's ``path``) /
   ``run_signature_sha``.
2. **branch**:

   * **A** — caller supplied ``data_axis`` → record it,
     ``classified_by="interview"``.
   * **B** — preflight cache hit (``cache_check.data.hit``) → the stored
     classification is still valid; reuse it, **no re-write**.
   * **C** — a prior campaign in ``recall`` classified the same
     ``run_name`` with a confident kind (a code-checkable structural
     match) → record it, ``classified_by="recall"``.
   * **D** — ``classify-axis-easy`` returns a confident kind → map it to a
     ``data_axis`` and record, ``classified_by="agent"``.
   * **E** — the matcher abstained (``unclassifiable`` /
     ``function_not_found``) → **no record**; return
     ``{needs_llm_tree: true, source_path, run_name, run_signature_sha,
     evidence, tried}`` so the LLM walks the long-tail decision tree.

The LLM's role shrinks to one tool call plus, only on branch E, the
genuine judgement of the decision tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.classify_axis import ClassifyAxisInput
from hpc_agent._wire.actions.classify_axis_auto import ClassifyAxisAutoInput
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["classify_axis_auto"]

# The matcher kinds confident enough to record without the LLM tree —
# imported from the matcher primitive so this composite and the matcher
# never drift on what "confident" means.
from hpc_agent.incorporation.classify_axis_easy import _CONFIDENT_KINDS

# Map each confident matcher kind to the recorded ``data_axis`` block.
# ``no_loop_detected`` is the terminal "no ordered series" verdict →
# recorded as the plain ``cartesian`` sweep (distinct from ``independent``,
# which has a parallelizable series). ``bounded_halo`` carries the
# structurally-extracted ``halo_expr``, handled in-line below.
_EASY_KIND_TO_AXIS: dict[str, str] = {
    "independent": "independent",
    "sequential": "sequential",
    "no_loop_detected": "cartesian",
}


def _resolve_single_run(
    discover_subresult: dict[str, Any], *, run_name: str | None
) -> dict[str, Any]:
    """Resolve the single ``@register_run`` row from discover-runs' output.

    *discover_subresult* is the preflight's ``discover_runs`` SubResult
    (``{envelope, elapsed_sec, ok}``). Returns the resolved run row
    (``{name, path, gpu, run_signature_sha, flags}``).

    Resolution mirrors the skill's Step-1 contract exactly:

    * caller scoped via *run_name* → that row (``SpecInvalid`` if absent);
    * else exactly one run → that row;
    * else (multiple runs, no scope) → ``SpecInvalid`` ``ambiguous_run``.
    """
    envelope = discover_subresult.get("envelope") or {}
    if not discover_subresult.get("ok"):
        code = envelope.get("error_code", "internal")
        msg = envelope.get("message", "discover-runs failed")
        raise errors.SpecInvalid(f"discover-runs failed ({code}): {msg}")
    runs: list[dict[str, Any]] = ((envelope.get("data") or {}).get("runs")) or []

    if run_name is not None:
        for row in runs:
            if row.get("name") == run_name:
                return row
        names = sorted(r.get("name", "?") for r in runs)
        raise errors.SpecInvalid(
            f"ambiguous_run: caller-supplied run_name {run_name!r} is not among "
            f"the discovered @register_run functions {names}"
        )

    if len(runs) == 1:
        return runs[0]
    if not runs:
        raise errors.SpecInvalid(
            "ambiguous_run: no @register_run functions discovered under the "
            "experiment dir — nothing to classify"
        )
    names = sorted(r.get("name", "?") for r in runs)
    raise errors.SpecInvalid(
        f"ambiguous_run: multiple @register_run functions {names} and no "
        "run_name supplied — pass run_name to scope the classification"
    )


def _recall_structural_match(
    recall_subresult: dict[str, Any] | None, *, run_name: str
) -> dict[str, Any] | None:
    """Return a prior campaign's confident classification for *run_name*, or None.

    A code-checkable structural match (the plan's branch C): walk
    ``recall``'s campaign summaries for a ``data_axes`` entry keyed by the
    SAME ``run_name`` whose recorded ``kind`` is a confident
    classification. The structural key is run-name identity — the
    composite never re-derives a halo or guesses across differently-named
    runs (that judgement stays in the LLM tree / interview).

    Returns the matched ``{kind, halo_expr?, monoid?}`` projection (the
    recall summary shape from ``_axis_classifications``) or ``None`` when
    no clean match exists.
    """
    if recall_subresult is None or not recall_subresult.get("ok"):
        return None
    data = (recall_subresult.get("envelope") or {}).get("data") or {}
    for campaign in data.get("campaigns") or []:
        axes = campaign.get("data_axes")
        if not isinstance(axes, dict):
            continue
        proj = axes.get(run_name)
        if not isinstance(proj, dict):
            continue
        kind = proj.get("kind")
        # A confident kind is one the matcher would have recorded directly.
        # ``no_loop_detected`` is a matcher-only verdict; the recorded form
        # is ``cartesian``. Accept the persisted DataAxis kinds.
        if kind in {"independent", "associative", "bounded_halo", "sequential", "cartesian"}:
            return proj
    return None


def _record(
    experiment_dir: Path,
    *,
    run_name: str,
    run_signature_sha: str,
    data_axis: dict[str, Any],
    classified_by: str,
) -> dict[str, Any]:
    """Build a ClassifyAxisInput and call the classify-axis recorder directly.

    The recorder validates *data_axis* (constructing the live ``DataAxis``,
    which compiles a ``bounded_halo`` expr) before any disk write, so a
    malformed block surfaces as ``SpecInvalid`` here.
    """
    from hpc_agent.incorporation.classify_axis import classify_axis as _record_axis

    spec = ClassifyAxisInput.model_validate(
        {
            "run_name": run_name,
            "run_signature_sha": run_signature_sha,
            "data_axis": data_axis,
            "classified_by": classified_by,
        }
    )
    written = _record_axis(experiment_dir, spec=spec)
    return {
        "recorded": True,
        "run_name": run_name,
        "kind": written["data_axis"]["kind"],
        "classified_by": classified_by,
        "axes_path": written["axes_path"],
    }


@primitive(
    name="classify-axis-auto",
    verb="scaffold",
    composes=["classify-axis-preflight", "classify-axis-easy", "classify-axis"],
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/axes.yaml"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Composite head of hpc-classify-axis: preflight (discover-runs + "
            "cache-check + recall) → classify-axis-easy → classify-axis "
            "recorder, in ONE call. Resolves the single @register_run, then "
            "branches: caller data_axis (interview) / cache hit (reuse) / "
            "recall structural match / a confident AST match (agent) all "
            "record into .hpc/axes.yaml; an unclassifiable/function_not_found "
            "matcher verdict records NOTHING and returns "
            "{needs_llm_tree: true, source_path, run_name, run_signature_sha, "
            "evidence, tried} so the caller walks the LLM decision tree. The "
            "LLM makes one call and only works the genuine long tail."
        ),
        spec_arg=True,
        spec_required=False,
        schema_ref=SchemaRef(input="classify_axis_auto"),
        spec_model=ClassifyAxisAutoInput,
        experiment_dir_arg=True,
    ),
    agent_facing=True,
)
def classify_axis_auto(
    experiment_dir: Path,
    *,
    spec: ClassifyAxisAutoInput | None = None,
) -> dict[str, Any]:
    """Run preflight → (branch) → record / hand-off, in one call.

    *experiment_dir* is the framework-context kwarg; *spec* carries the
    optional ``run_name`` / ``data_axis`` / ``root`` / ``task_kind`` (a
    bare call with no ``--spec`` is valid — it classifies the sole run
    autonomously).

    Returns a discriminated dict — ``{recorded: true, run_name, kind,
    classified_by, axes_path}`` (branches A–D) OR ``{needs_llm_tree: true,
    run_name, source_path, run_signature_sha, evidence, tried}`` (branch
    E). Raises ``errors.SpecInvalid`` when the run can't be resolved
    unambiguously (``ambiguous_run``) or a recorded ``data_axis`` is
    internally inconsistent.
    """
    from hpc_agent.incorporation.classify_axis_easy import classify_axis_easy
    from hpc_agent.ops.classify_axis_preflight import classify_axis_preflight

    if spec is None:
        spec = ClassifyAxisAutoInput()

    data_axis_supplied = spec.data_axis is not None

    # 1. Preflight (discover-runs + cache-check + conditional recall).
    #    Pass run_name only when the caller scoped it; the run's
    #    run_signature_sha is only known AFTER discover-runs, so the first
    #    pass cache-check reports a miss when run_name is None — we re-read
    #    the cache against the resolved run below.
    preflight = classify_axis_preflight(
        experiment_dir=experiment_dir,
        run_name=spec.run_name,
        run_signature_sha=None,
        root=spec.root,
        task_kind=spec.task_kind,
        data_axis_supplied=data_axis_supplied,
    )

    # Resolve the single run from discover-runs (the source_path + sha the
    # rest of the pipeline consumes — the invariant the bug violated:
    # `easy` MUST receive exactly what preflight produced).
    run_row = _resolve_single_run(preflight["discover_runs"], run_name=spec.run_name)
    run_name = run_row["name"]
    source_path = run_row["path"]
    run_signature_sha = run_row["run_signature_sha"]

    # ── Branch A: caller supplied data_axis → record as 'interview'. ──────
    if spec.data_axis is not None:
        data_axis = spec.data_axis.model_dump(exclude_none=True, mode="json")
        return _record(
            experiment_dir,
            run_name=run_name,
            run_signature_sha=run_signature_sha,
            data_axis=data_axis,
            classified_by="interview",
        )

    # ── Branch B: a still-valid cached classification → reuse, no re-write.
    #    The first preflight pass had no sha to compare; re-check the cache
    #    now that the run is resolved.
    from hpc_agent.ops.classify_axis_preflight import _run_cache_check

    cache = _run_cache_check(
        experiment_dir=experiment_dir,
        run_name=run_name,
        run_signature_sha=run_signature_sha,
    )
    cache_data = (cache.get("envelope") or {}).get("data") or {}
    if cache.get("ok") and cache_data.get("hit"):
        stored = cache_data.get("stored") or {}
        stored_axis = stored.get("data_axis") or {}
        return {
            "recorded": True,
            "run_name": run_name,
            "kind": stored_axis.get("kind"),
            "classified_by": stored.get("classified_by", "agent"),
            "axes_path": _axes_path_str(experiment_dir),
        }

    # ── Branch C: a prior campaign classified the same run confidently. ───
    recalled = _recall_structural_match(preflight.get("recall"), run_name=run_name)
    if recalled is not None:
        data_axis = _recall_proj_to_axis(recalled)
        return _record(
            experiment_dir,
            run_name=run_name,
            run_signature_sha=run_signature_sha,
            data_axis=data_axis,
            classified_by="recall",
        )

    # ── Branch D/E: the AST fast-path matcher. ───────────────────────────
    easy = classify_axis_easy(source_path=source_path, run_name=run_name)
    kind = easy["kind"]

    if kind in _CONFIDENT_KINDS:
        # Branch D: map the confident matcher kind to a data_axis block.
        if kind == "bounded_halo":
            data_axis = {"kind": "bounded_halo", "halo": {"expr": easy["halo_expr"]}}
        else:
            data_axis = {"kind": _EASY_KIND_TO_AXIS[kind]}
        return _record(
            experiment_dir,
            run_name=run_name,
            run_signature_sha=run_signature_sha,
            data_axis=data_axis,
            classified_by="agent",
        )

    # Branch E: unclassifiable / function_not_found → NO record. Hand the
    # source_path + run_name + sha + evidence to the LLM decision tree.
    return {
        "needs_llm_tree": True,
        "run_name": run_name,
        "source_path": source_path,
        "run_signature_sha": run_signature_sha,
        "evidence": easy["evidence"],
        "tried": list(easy["tried"]),
    }


def _recall_proj_to_axis(proj: dict[str, Any]) -> dict[str, Any]:
    """Map a recall ``data_axes`` projection to a recordable ``data_axis`` block.

    The recall projection is ``{kind, halo_expr?, monoid?}`` (the flat
    shape ``_axis_classifications`` emits). The recorder wants the nested
    ``halo: {expr}`` form, so re-nest the halo here.
    """
    axis: dict[str, Any] = {"kind": proj["kind"]}
    if proj.get("halo_expr"):
        axis["halo"] = {"expr": proj["halo_expr"]}
    if proj.get("monoid"):
        axis["monoid"] = proj["monoid"]
    return axis


def _axes_path_str(experiment_dir: Path) -> str:
    """Absolute path to the experiment's axes.yaml (for the cache-hit reuse echo)."""
    from hpc_agent.state.axes import axes_path

    return str(axes_path(experiment_dir))
