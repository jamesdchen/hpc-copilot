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

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.classify_axis import ClassifyAxisInput
from hpc_agent._wire.actions.classify_axis_auto import ClassifyAxisAutoInput
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from hpc_agent.state.pack_declarations import AxisHintsDecl

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

# --- S3 domain-pack axis hints (docs/design/domain-packs.md, T9c) -----------
#
# A pack may DECLARE ``axis_hints: [{pattern, axis}]`` — a name-regex + one of
# core's EXISTING closed ``DataAxis`` literals (validated at load by
# ``state/pack.py::load_axis_hints`` against ``AXIS_LITERALS``, never a new
# vocabulary). The LOAD-BEARING rule (the "locking is the safe direction"
# posture): a hint ADDS CAUTION, NEVER CLEARANCE. When a matching hint AGREES
# with core's structural heuristic the classification proceeds unchanged (the
# hint only confirms); when it DISAGREES the case demotes to needs-decision
# with BOTH candidates named; a hint can NEVER auto-resolve an axis core's own
# heuristic would not have resolved. This module stays pack-IGNORANT: it consumes
# the typed opaque ``AxisHintsDecl`` list the ``state`` resolver hands it and
# copies the ``{pack, version, sha}`` echo verbatim, never reading it for meaning.

# Map a hint's DataAxis literal (PascalCase class name — core's closed
# ``AXIS_LITERALS``) to the lowercase axis KIND the matcher/recorder speak.
# ``cartesian`` (core's "no ordered series" terminal) has no DataAxis-literal
# twin, so no hint can ever name it — a run core classifies ``cartesian`` that a
# hint touches therefore always DISAGREES (demotes), never confirms.
_HINT_AXIS_TO_KIND: dict[str, str] = {
    "Independent": "independent",
    "Associative": "associative",
    "BoundedHalo": "bounded_halo",
    "Sequential": "sequential",
}


@dataclass(frozen=True)
class _AxisHintOutcome:
    """The verdict of applying declared axis hints to a structural classification.

    Pure and pack-ignorant — the whole caution-not-clearance decision, computed
    off an opaque hint list + core's own verdict. ``verdict``:

    * ``"none"`` — no declared hint's ``pattern`` matched ``run_name`` (or there
      were no hints); the classification is UNTOUCHED (byte-identical to the
      no-pack path).
    * ``"agree"`` — every matching hint names the SAME axis core's heuristic
      resolved; the classification proceeds unchanged and ``confirmations``
      echoes the agreeing hints (each carries its pack ``{pack, version, sha}``)
      as confirmation evidence.
    * ``"conflict"`` — at least one matching hint names a DIFFERENT axis (or core
      abstained, ``core_kind is None``, so no hint can clear); the case demotes
      to needs-decision. ``core_kind`` + ``hint_kinds`` name BOTH candidate
      sides; ``conflicts`` carries the disagreeing hints + their pack echoes.
    """

    verdict: str  # "none" | "agree" | "conflict"
    core_kind: str | None
    hint_kinds: tuple[str, ...]
    confirmations: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]


def _apply_axis_hints(
    core_kind: str | None,
    run_name: str,
    hint_decls: Sequence[AxisHintsDecl],
) -> _AxisHintOutcome:
    """Match declared hints against *run_name* and classify caution vs. clearance.

    *core_kind* is the lowercase axis kind core's structural heuristic resolved
    (branch D), or ``None`` when the matcher abstained (branch E — a hint can add
    caution but never resolve). A hint APPLIES iff its ``pattern`` (a name regex)
    matches *run_name*. An applying hint whose kind equals *core_kind* CONFIRMS;
    any other applying hint CONFLICTS (and every applying hint conflicts when core
    abstained). Any conflict → ``verdict="conflict"`` (caution wins — the safe
    direction); otherwise a match → ``"agree"``; no match → ``"none"``.
    """
    confirmations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    ordered_kinds: list[str] = []
    for decl in hint_decls:
        echo = decl.echo.as_dict()
        for hint in decl.hints:
            if re.search(hint["pattern"], run_name) is None:
                continue
            hint_kind = _HINT_AXIS_TO_KIND[hint["axis"]]
            ordered_kinds.append(hint_kind)
            entry = {
                "pattern": hint["pattern"],
                "axis": hint["axis"],
                "kind": hint_kind,
                "pack": echo,
            }
            if core_kind is not None and hint_kind == core_kind:
                confirmations.append(entry)
            else:
                conflicts.append(entry)
    hint_kinds = tuple(dict.fromkeys(ordered_kinds))  # de-duped, first-seen order
    if not ordered_kinds:
        return _AxisHintOutcome("none", core_kind, (), (), ())
    if conflicts:
        return _AxisHintOutcome(
            "conflict", core_kind, hint_kinds, tuple(confirmations), tuple(conflicts)
        )
    return _AxisHintOutcome("agree", core_kind, hint_kinds, tuple(confirmations), ())


def _hint_packs(entries: Sequence[dict[str, Any]]) -> str:
    """The comma-joined, sorted pack names named by *entries* (for evidence text)."""
    return ", ".join(sorted({str(e["pack"]["pack"]) for e in entries}))


def _conflict_evidence(run_name: str, outcome: _AxisHintOutcome) -> str:
    """One-line evidence naming BOTH candidate sides of a demoting hint conflict."""
    core = outcome.core_kind or "unresolved"
    hint_kinds = ", ".join(outcome.hint_kinds)
    return (
        f"axis-hint conflict on {run_name!r}: core's structural heuristic resolved "
        f"{core!r}, but declared pack hint(s) name {hint_kinds!r} "
        f"(pack: {_hint_packs(outcome.conflicts)}). A disagreeing hint adds caution, "
        "never clearance — demoted to the decision tree (hints never auto-resolve)."
    )


def _abstain_hint_evidence(base: str, run_name: str, outcome: _AxisHintOutcome) -> str:
    """Augment the matcher's abstain evidence with the un-applied hint (caution only)."""
    hint_kinds = ", ".join(outcome.hint_kinds)
    return (
        f"{base} | declared pack hint(s) name {hint_kinds!r} "
        f"(pack: {_hint_packs(outcome.conflicts)}) for {run_name!r}, but the "
        "structural matcher abstained — a hint never auto-resolves, so the "
        "decision tree still decides."
    )


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
    axis_hints: Sequence[AxisHintsDecl] | None = None,
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

    # S3 axis hints (T9c). ``axis_hints`` is CALLER-injected per the pack-ignorant
    # consumer posture; when unset (the CLI/skill path) resolve them off the pack
    # opt-in via the ``state`` substrate. Absent ``packs`` block → the D7 silence:
    # an empty list, zero probes beyond interview.json (the resolver short-circuits
    # on an empty opt-in before touching the pack journal). ``incorporation`` may
    # import ``state`` freely; the reader mirrors ops/pack/status_op's forward-
    # compatible T8 wiring (the ``"pack"`` scope kind lands with T8).
    if axis_hints is None:
        from hpc_agent.state.decision_journal import read_decisions
        from hpc_agent.state.pack_declarations import resolve_axis_hints

        axis_hints = resolve_axis_hints(
            experiment_dir,
            records_reader=lambda name: read_decisions(experiment_dir, "pack", name),
        )

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
            core_kind = "bounded_halo"
            data_axis = {"kind": "bounded_halo", "halo": {"expr": easy["halo_expr"]}}
        else:
            core_kind = _EASY_KIND_TO_AXIS[kind]
            data_axis = {"kind": core_kind}

        # S3: a DISAGREEING pack hint demotes an otherwise-confident structural
        # verdict to needs-decision, naming BOTH candidates. An AGREEING (or
        # absent) hint only confirms — the classification proceeds byte-identically
        # and records as 'agent' (the hint adds caution, never clearance).
        hint_outcome = _apply_axis_hints(core_kind, run_name, axis_hints)
        if hint_outcome.verdict == "conflict":
            return {
                "needs_llm_tree": True,
                "run_name": run_name,
                "source_path": source_path,
                "run_signature_sha": run_signature_sha,
                "evidence": _conflict_evidence(run_name, hint_outcome),
                "tried": list(easy["tried"]),
            }
        return _record(
            experiment_dir,
            run_name=run_name,
            run_signature_sha=run_signature_sha,
            data_axis=data_axis,
            classified_by="agent",
        )

    # Branch E: unclassifiable / function_not_found → NO record. Hand the
    # source_path + run_name + sha + evidence to the LLM decision tree. A pack
    # hint here can NEVER auto-resolve the axis (core's heuristic did not resolve
    # it) — it only rides along as caution in the evidence.
    hint_outcome = _apply_axis_hints(None, run_name, axis_hints)
    evidence = easy["evidence"]
    if hint_outcome.verdict != "none":
        evidence = _abstain_hint_evidence(evidence, run_name, hint_outcome)
    return {
        "needs_llm_tree": True,
        "run_name": run_name,
        "source_path": source_path,
        "run_signature_sha": run_signature_sha,
        "evidence": evidence,
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
