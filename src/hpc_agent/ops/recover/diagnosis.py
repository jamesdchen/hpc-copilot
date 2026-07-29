"""Park-time diagnosis seam — the code-composed request + the opaque attach.

When a run parks on an ANOMALY (a failed canary, a watch anomaly), the human's
sitting is the scarce resource: they open the brief cold and reconstruct the
failure from raw logs. This module is the maintainer-approved seam that lets a
READ-ONLY investigator agent enrich what the human reads — WITHOUT the agent's
judgment ever entering a trusted surface:

* :func:`compose_diagnosis_request` / ``diagnosis-request`` — the KERNEL side,
  a pure code-composed read: what an investigator should look at (the parked
  verb/stage/reason, the failure-signature matches the run's stores already
  hold, the LOCAL paths worth reading, and the closed category vocabulary).
  The kernel never spawns the investigator and never consumes its output.
* ``attach-diagnosis`` — the durable ATTACH channel: shape-validated,
  provenance-stamped (``authored_by: "agent"`` — stamped by the state layer,
  never caller-supplied), written atomically beside the terminal records
  (:mod:`hpc_agent.state.diagnosis`). UNGATED: advisory data spends nothing.

The trust story is byte-identical to a world without this seam: the diagnosis
NEVER enters the decision-brief provenance journal, never becomes an
answer-menu option (that surface is code-AUTHORED data only), and no gate reads
it. Surfaces (park notification, doctor, morning digest) carry POINTERS +
COUNTS; the human reads the render from disk (§8 S13 shape).

Failure-signature classification REUSES the one catalog entry point
(:func:`hpc_agent.infra.failure_signatures.classify`) over evidence strings the
run's stores already hold — never a second matcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.attach_diagnosis import AttachDiagnosisResult, AttachDiagnosisSpec
from hpc_agent._wire.queries.diagnosis_request import DiagnosisRequestResult, DiagnosisRequestSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "UNMATCHED_CLASSIFICATION",
    "attach_diagnosis",
    "compose_diagnosis_request",
    "diagnosis_request",
]

#: The one non-catalog classification ``attach-diagnosis`` accepts: the
#: investigator read the evidence and NO catalog category fits. Distinct from
#: ``classify()``'s "unknown" (the matcher's no-hit result) on purpose — an
#: agent's "I looked and it matches nothing" is a claim, not a matcher output.
UNMATCHED_CLASSIFICATION = "unmatched"

#: Keys under which the run's stores carry raw failure text worth classifying.
#: ``cluster_log_tail`` — verify-canary's ``failure_features`` envelope;
#: ``stderr_tail`` — the same envelope's top-level sibling; ``sample`` — a
#: failure cluster's representative snippet (``runner_failures``).
_EVIDENCE_TEXT_KEYS = frozenset({"cluster_log_tail", "stderr_tail", "sample"})

#: Bounds on the request's evidence scan — the request is a pointer surface,
#: not a dossier, so both the walk and the match list stay small.
_EVIDENCE_SCAN_DEPTH = 4
_MAX_SIGNATURE_MATCHES = 8

_SEAM_NOTE = (
    "code-composed diagnosis request (park-time diagnosis seam): an "
    "investigator reads ONLY the named local paths and attaches its findings "
    "via attach-diagnosis. The attached dossier is stored as an OPAQUE, "
    "provenance-marked agent proposal — display-only advisory matter, never a "
    "decision-brief, never an answer-menu option, never a gate input."
)


def _collect_evidence_texts(
    node: Any, *, source: str, depth: int, out: list[tuple[str, str]]
) -> None:
    """Depth-bounded walk collecting ``(source, text)`` failure evidence strings.

    Bounded on purpose (the ``recommendation_options`` precedent): an unbounded
    walk would eventually classify text buried in a sub-record the boundary
    does not actually surface. Only string values under
    :data:`_EVIDENCE_TEXT_KEYS` are taken.
    """
    if depth <= 0:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            key_str = str(key)
            if key_str in _EVIDENCE_TEXT_KEYS and isinstance(value, str) and value.strip():
                out.append((f"{source}.{key_str}", value))
            else:
                _collect_evidence_texts(
                    value, source=f"{source}.{key_str}", depth=depth - 1, out=out
                )
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _collect_evidence_texts(value, source=f"{source}[{i}]", depth=depth - 1, out=out)


def _collect_stored_classifications(
    node: Any, *, source: str, depth: int, out: list[dict[str, Any]]
) -> None:
    """Relay VERBATIM any ``classified_error`` triple a store already carries.

    Those triples were produced by the same catalog at failure time
    (``ops/verify_canary._failure_features``); re-deriving them would be fine,
    but relaying the stored one keeps the request honest about what the store
    already said.
    """
    if depth <= 0:
        return
    if isinstance(node, dict):
        classified = node.get("classified_error")
        if isinstance(classified, dict) and classified.get("error_class"):
            out.append({"source": f"{source}.classified_error (stored)", **classified})
        for key, value in node.items():
            if key == "classified_error":
                continue
            _collect_stored_classifications(
                value, source=f"{source}.{key}", depth=depth - 1, out=out
            )
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _collect_stored_classifications(
                value, source=f"{source}[{i}]", depth=depth - 1, out=out
            )


def _signature_matches(
    brief: dict[str, Any] | None, terminal_result: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Classify the failure evidence the run's stores ALREADY hold.

    One classifier: every fresh classification routes through
    :func:`hpc_agent.infra.failure_signatures.classify` (the catalog's single
    entry point); stored ``classified_error`` triples are relayed verbatim.
    Deduplicated on ``error_class`` (first source wins), bounded to
    :data:`_MAX_SIGNATURE_MATCHES`.
    """
    from hpc_agent.infra.failure_signatures import classify

    matches: list[dict[str, Any]] = []
    seen_classes: set[str] = set()

    stored: list[dict[str, Any]] = []
    texts: list[tuple[str, str]] = []
    for source, node in (("brief", brief), ("terminal", terminal_result)):
        if isinstance(node, dict):
            _collect_stored_classifications(
                node, source=source, depth=_EVIDENCE_SCAN_DEPTH, out=stored
            )
            _collect_evidence_texts(node, source=source, depth=_EVIDENCE_SCAN_DEPTH, out=texts)

    for triple in stored:
        error_class = str(triple.get("error_class") or "")
        if not error_class or error_class in seen_classes:
            continue
        seen_classes.add(error_class)
        matches.append(triple)

    seen_texts: set[str] = set()
    for source, text in texts:
        if text in seen_texts:
            continue
        seen_texts.add(text)
        sig = classify(text, exit_code=None)
        error_class = str(sig.get("error_class") or "")
        if error_class in ("", "unknown") or error_class in seen_classes:
            continue
        seen_classes.add(error_class)
        matches.append({"source": source, **sig})

    return matches[:_MAX_SIGNATURE_MATCHES]


def _is_anomaly(block: str, stage: str | None, brief: dict[str, Any] | None) -> bool:
    """Whether the parked boundary is an ANOMALY terminator — code-decided.

    Primary: ``(block, stage)`` membership in
    :data:`hpc_agent.infra.block_chain.ANOMALY_TERMINATORS` when the stage is
    known (the terminal record carried it). Fallback when it is not: the park
    brief's own answer menu marks the advance option ``override: True`` at an
    anomaly terminator (``answer_menu.compose_answer_menu``) — a projection
    the driver already computed at park time, not a judgment made here.
    """
    from hpc_agent.infra.block_chain import ANOMALY_TERMINATORS

    if stage is not None:
        return (block, str(stage)) in ANOMALY_TERMINATORS
    from hpc_agent.ops.relay_render import answer_menu_of

    menu = answer_menu_of(brief) or {}
    options = menu.get("options")
    if not isinstance(options, list):
        return False
    return any(isinstance(o, dict) and o.get("override") is True for o in options)


def compose_diagnosis_request(experiment_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Compose the code-only diagnosis request for one PARKED run, or ``None``.

    Pure read. ``None`` when *run_id* is not parked on a decision (no
    pending-decision marker) — there is no boundary to investigate. Everything
    in the returned dict is code-carried: marker fields, the terminal record's
    own stage/reason, catalog classifications over stored evidence, existing
    LOCAL paths (never content), and the closed category vocabulary.
    """
    from hpc_agent._kernel.contract.layout import JournalLayout, RepoLayout
    from hpc_agent.infra.failure_signatures import CLASSIFIER_CATEGORIES
    from hpc_agent.state.block_terminal import (
        read_terminal_with_fallback,
        terminal_block_key,
        terminal_path,
    )
    from hpc_agent.state.decision_briefs import briefs_path
    from hpc_agent.state.diagnosis import diagnosis_path, read_diagnosis
    from hpc_agent.state.journal import read_pending_decision
    from hpc_agent.state.run_record import current_homedir

    marker = read_pending_decision(run_id, experiment_dir=experiment_dir)
    if not marker:
        return None

    block = str(marker.get("block") or "")
    workflow = marker.get("workflow") or None
    awaiting_since = marker.get("awaiting_since") or None
    brief = marker.get("brief") if isinstance(marker.get("brief"), dict) else None

    terminal = read_terminal_with_fallback(experiment_dir, run_id, block) if block else None
    terminal_result = terminal.get("result") if isinstance(terminal, dict) else None
    if not isinstance(terminal_result, dict):
        terminal_result = None
    stage = None
    reason = None
    if terminal_result is not None:
        stage_val = terminal_result.get("stage_reached")
        stage = str(stage_val) if stage_val else None
        reason_val = terminal_result.get("reason")
        reason = str(reason_val) if reason_val else None

    # Paths only, existing files only — the investigator READS, this composer
    # never inlines content (S13: pointers, and the human/agent reads from disk).
    layout = RepoLayout(experiment_dir)
    journal = JournalLayout(experiment_dir)
    candidates: dict[str, Path] = {
        "run_sidecar": layout.run_sidecar(run_id),
        "journal_record": journal.run_record(run_id),
        "monitor_log": journal.monitor_jsonl(run_id),
        "last_status": journal.last_status(run_id),
        "decision_briefs": briefs_path(experiment_dir, run_id),
    }
    if block:
        candidates["block_terminal"] = terminal_path(
            experiment_dir, run_id, terminal_block_key(block)
        )
    read_paths = {name: str(p) for name, p in candidates.items() if p.is_file()}

    detached_dir = current_homedir() / "_detached"
    worker_logs: list[str] = []
    if detached_dir.is_dir():
        worker_logs = sorted(str(p) for p in detached_dir.glob(f"*-{run_id}-*.log"))

    return {
        "run_id": run_id,
        "block": block,
        "workflow": str(workflow) if workflow else None,
        "awaiting_since": str(awaiting_since) if awaiting_since else None,
        "stage_reached": stage,
        "reason": reason,
        "is_anomaly": _is_anomaly(block, stage, brief),
        "signature_matches": _signature_matches(brief, terminal_result),
        "categories": sorted(CLASSIFIER_CATEGORIES),
        "read_paths": read_paths,
        "worker_logs": worker_logs,
        "attach_target": str(diagnosis_path(experiment_dir, run_id)),
        "diagnosis_attached": read_diagnosis(experiment_dir, run_id) is not None,
        "note": _SEAM_NOTE,
    }


@primitive(
    name="diagnosis-request",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid, errors.PreconditionFailed],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Compose the code-only diagnosis request for one PARKED run: the "
            "parked verb/stage/reason off the pending-decision marker, "
            "failure-signature matches over evidence the run's stores already "
            "hold (the one catalog classifier), the LOCAL log/artifact paths a "
            "read-only investigator should read (paths only), and the closed "
            "category vocabulary to classify against. Pure read; refuses for a "
            "run that is not parked. The kernel never spawns the investigator "
            "and never consumes its judgment — findings come back through "
            "attach-diagnosis as display-only advisory data."
        ),
        spec_arg=True,
        spec_model=DiagnosisRequestSpec,
        experiment_dir_arg=True,
        schema_ref=SchemaRef(input="diagnosis_request"),
    ),
    agent_facing=True,
)
def diagnosis_request(
    experiment_dir: Path, *, spec: DiagnosisRequestSpec
) -> DiagnosisRequestResult:
    """Compose the diagnosis request for ``spec.run_id`` (pure read, code-only).

    Raises :class:`errors.PreconditionFailed` when the run is not parked on a
    decision — there is no boundary to investigate, so an investigator must
    not be pointed at it.
    """
    request = compose_diagnosis_request(experiment_dir, spec.run_id)
    if request is None:
        raise errors.PreconditionFailed(
            f"run {spec.run_id!r} is not parked on a decision — no pending-decision "
            "marker, so there is nothing to investigate. diagnosis-request serves "
            "parked boundaries only."
        )
    return DiagnosisRequestResult(**request)


@primitive(
    name="attach-diagnosis",
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "<experiment_dir>/.hpc/runs/<run_id>.diagnosis.json"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Attach an investigator's diagnosis dossier for one run — stored as "
            "an OPAQUE, provenance-marked agent proposal beside the terminal "
            "records (<run_id>.diagnosis.json). Shape-validated only: the "
            "classification must come from the closed catalog vocabulary (or "
            "'unmatched'); the content is never interpreted, never enters a "
            "decision brief, an answer menu, or any gate. UNGATED — advisory "
            "data spends nothing. Re-attach overwrites (newest wins)."
        ),
        spec_arg=True,
        spec_model=AttachDiagnosisSpec,
        experiment_dir_arg=True,
        schema_ref=SchemaRef(input="attach_diagnosis"),
    ),
    agent_facing=True,
)
def attach_diagnosis(
    experiment_dir: Path, *, spec: AttachDiagnosisSpec
) -> AttachDiagnosisResult:
    """Write (OVERWRITE) the agent diagnosis dossier for ``spec.run_id``.

    SHAPE validation only, plus the one closed-set check the schema declares:
    ``classification`` must be a catalog category
    (:data:`hpc_agent.infra.failure_signatures.CLASSIFIER_CATEGORIES`) or the
    literal ``"unmatched"``. Provenance (``authored_by: "agent"``,
    ``attached_at``) is stamped by :func:`hpc_agent.state.diagnosis.write_diagnosis`
    — never accepted from the caller, so every stored dossier is honestly
    labelled. No gate is consulted (advisory data spends nothing).
    """
    from hpc_agent.infra.failure_signatures import CLASSIFIER_CATEGORIES
    from hpc_agent.state.diagnosis import diagnosis_path, write_diagnosis

    allowed = CLASSIFIER_CATEGORIES | {UNMATCHED_CLASSIFICATION}
    if spec.classification not in allowed:
        raise errors.SpecInvalid(
            f"classification {spec.classification!r} is not in the closed catalog "
            f"vocabulary — name one of {sorted(allowed)} (the set diagnosis-request "
            "disclosed), or 'unmatched' when nothing fits."
        )

    path = diagnosis_path(experiment_dir, spec.run_id)
    overwrote = path.is_file()
    record = write_diagnosis(
        experiment_dir,
        run_id=spec.run_id,
        classification=spec.classification,
        evidence_excerpts=[e.model_dump(mode="json") for e in spec.evidence_excerpts],
        proposed_actions=[a.model_dump(mode="json") for a in spec.proposed_actions],
    )
    return AttachDiagnosisResult(
        run_id=spec.run_id,
        path=str(path),
        attached_at=str(record["provenance"]["attached_at"]),
        classification=spec.classification,
        proposed_actions_count=len(spec.proposed_actions),
        overwrote=overwrote,
    )
