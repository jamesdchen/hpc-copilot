"""``adopt-run`` — first-class ingest of a run submitted OUTSIDE hpc-agent.

Agents explore freestyle (hand-rolled scripts, raw ``sbatch``/``qsub``);
hpc-agent verifies ex-post. ``adopt-run`` is the ingest half of that posture:
given the foreign run's identity + cluster contract, it engages the SAME
fidelity machinery a framework-submitted run gets — never a parallel one:

1. **Refuse-not-clobber**: a run_id that already has a sidecar
   (``<exp>/.hpc/runs/<run_id>.json``) OR a journal record is refused,
   pointing at the existing record.
2. **Layout inference** (local-only): when ``result_dir_template`` /
   ``task_count`` are missing, infer them from ``results_sample`` — glob the
   task dirs, detect the trailing-integer task pattern (``task_count`` is
   ``max(index)+1``, gap-safe), verify ``summary_artifact`` presence. This
   primitive declares ``file_write`` only, so inference NEVER probes over
   ssh: a remote / non-resolving anchor yields a ``needs_elicitation``
   envelope naming exactly what to supply — never a guess.
3. **Sidecar** via the existing ``write-run-sidecar`` primitive — its
   real-per-task-command guard, dispatcher refusal, and ``.hpc/tasks.py``
   identity cross-check apply unchanged. ``cmd_sha`` is ALWAYS derived here
   as ``sha256(command.strip())`` (full 64 hex), never caller-supplied.
4. **Journal record** through the existing record path — NO scheduler call:
   in-flight adoptions go through :func:`submit_and_record` (the documented
   record-only path); an already-terminal adoption mirrors its fresh-record
   construction with ``job_ids=[]`` (``SubmitSpec`` cannot express an empty
   job list by design — it guards real submissions).
5. **Terminal settle** (``job_ids`` absent) through the settle-run mechanism
   on the directed ``terminal_evidence``: ``append_decision`` (scope ``run``,
   response ``y``, block ``adopt-run``) → ``update_run_status`` →
   ``mark_run(complete)`` → the receipt-gated ``harvest_on_terminal``. An
   empty ``terminal_evidence`` is refused — a settle with no evidence is a
   bare status flip (the settle-run doctrine).
6. Standard envelope with ``next_block``: in-flight → ``status-watch``;
   terminal → ``aggregate-check``. ``verify-reproduction`` in
   external-baseline mode is the claim-comparison path from there.
"""

from __future__ import annotations

import glob as _glob
import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.adopt_run import AdoptRunInput, AdoptRunResult
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["adopt_run"]

# Trailing-integer task-dir pattern: ``task_7`` / ``seed3`` / ``12``.
_TRAILING_INT = re.compile(r"^(?P<prefix>.*?)(?P<idx>\d+)$")
_GLOB_CHARS = frozenset("*?[")


def _derive_cmd_sha(command: str) -> str:
    """The ONE derivation: sha256 of the stripped command, full 64 hex."""
    return hashlib.sha256(command.strip().encode("utf-8")).hexdigest()


def _candidate_task_dirs(anchor: str) -> tuple[Path | None, list[Path]]:
    """Resolve *anchor* (a LOCAL path or glob) to ``(parent, task_dirs)``.

    Three accepted shapes: a glob over task dirs, a parent dir whose children
    are task dirs, or one task dir (siblings sharing its prefix are swept in).
    Returns ``(None, [])`` when the anchor resolves to nothing locally — the
    caller elicits instead of guessing (no ssh probe; file_write is the only
    declared side effect).
    """
    if any(ch in anchor for ch in _GLOB_CHARS):
        dirs = [Path(m) for m in _glob.glob(anchor) if Path(m).is_dir()]
        dirs = [d for d in dirs if _TRAILING_INT.match(d.name)]
        if not dirs:
            return None, []
        return dirs[0].parent, sorted(dirs)
    p = Path(anchor)
    if not p.is_dir():
        return None, []
    children = [c for c in p.iterdir() if c.is_dir() and _TRAILING_INT.match(c.name)]
    if children:
        return p, sorted(children)
    own = _TRAILING_INT.match(p.name)
    if own:
        prefix = own.group("prefix")
        sibs = [
            c
            for c in p.parent.iterdir()
            if c.is_dir() and c.name.startswith(prefix) and _TRAILING_INT.match(c.name)
        ]
        return p.parent, sorted(sibs)
    return None, []


def _infer_layout(results_sample: str, summary_artifact: str) -> tuple[str, int] | str:
    """Infer ``(result_dir_template, task_count)`` from a LOCAL sample tree.

    Mechanical rules only (no representative/importance heuristics):

    * every sampled dir must share ONE trailing-integer name pattern
      (``<prefix><int>``) with a consistent prefix;
    * ``task_count = max(index) + 1`` — max+1 not count, so an index gap never
      undercounts the array;
    * at least one sampled dir must contain *summary_artifact* — a tree with
      no summary artifact proves the anchor is not the result tree;
    * the template is ``<parent.name>/<prefix>{task_id}`` — the anchor names
      the run-root-relative leading component.

    Returns the pair on success, or a reason string when inference is
    impossible (the caller wraps it into the elicitation envelope).
    """
    parent, task_dirs = _candidate_task_dirs(results_sample)
    if parent is None or not task_dirs:
        return (
            f"results_sample {results_sample!r} resolves to no local task directories "
            "with a trailing-integer name pattern (remote paths are never probed — "
            "this primitive declares file_write only)"
        )
    prefixes = set()
    indices: list[int] = []
    for d in task_dirs:
        m = _TRAILING_INT.match(d.name)
        if m is None:  # pragma: no cover — _candidate_task_dirs pre-filters
            continue
        prefixes.add(m.group("prefix"))
        indices.append(int(m.group("idx")))
    if len(prefixes) != 1:
        return (
            f"sampled task dirs under {parent} carry {len(prefixes)} distinct name "
            f"prefixes ({sorted(prefixes)!r}) — the trailing-integer task pattern is "
            "ambiguous"
        )
    if not any((d / summary_artifact).is_file() for d in task_dirs):
        return (
            f"none of the {len(task_dirs)} sampled task dirs under {parent} contain "
            f"the summary artifact {summary_artifact!r}"
        )
    prefix = next(iter(prefixes))
    task_count = max(indices) + 1  # max+1, never len(): index gaps must not undercount
    template = f"{parent.name}/{prefix}{{task_id}}"
    return template, task_count


@primitive(
    name="adopt-run",
    verb="mutate",
    composes=["write-run-sidecar"],
    side_effects=[
        SideEffect(
            "file_write",
            "<experiment>/.hpc/runs/<run_id>.json + the journal record "
            "(+ the directed-settle decision on the terminal branch)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=False,
    cli=CliShape(
        help=(
            "Adopt a run submitted OUTSIDE hpc-agent (freestyle sbatch/qsub) so the "
            "fidelity machinery engages: derive cmd_sha from the command, write the "
            "sidecar via write-run-sidecar (its guards apply unchanged), mint the "
            "journal record WITHOUT any scheduler call, and — when job_ids is absent — "
            "settle terminal on directed terminal_evidence through the settle-run "
            "mechanism. Refuses a run_id that already has a sidecar or journal record. "
            "Missing result layout is inferred from a LOCAL results_sample or elicited; "
            "never guessed. next_block: in-flight → status-watch, terminal → "
            "aggregate-check; verify-reproduction external-baseline is the "
            "claim-comparison path."
        ),
        spec_arg=True,
        spec_model=AdoptRunInput,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="adopt_run"),
    ),
    agent_facing=True,
)
def adopt_run(
    experiment_dir: Path,
    *,
    spec: AdoptRunInput,
    _aggregate: Callable[[Path, str], Any] | None = None,
    _sweep: Callable[[str, str], dict[int, list[str]]] | None = None,
) -> AdoptRunResult:
    """Adopt the foreign run described by *spec* under *experiment_dir*.

    ``_aggregate`` / ``_sweep`` are injected seams forwarded to
    ``harvest_on_terminal`` on the terminal branch (test-only; production
    leaves them at the defaults — same seams as ``settle-run``).

    Raises :class:`errors.SpecInvalid` on a duplicate run_id, an empty
    ``terminal_evidence`` on the terminal branch, or a spec the composed
    ``write-run-sidecar`` guards refuse.
    """
    from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
    from hpc_agent.infra.time import utcnow_iso
    from hpc_agent.ops.write_run_sidecar import write_run_sidecar
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import run_sidecar_path

    run_id = spec.run_id
    cmd_sha = _derive_cmd_sha(spec.command)
    in_flight = spec.job_ids is not None
    summary_artifact = spec.summary_artifact or "metrics.json"

    # ── (1) refuse-not-clobber: an existing sidecar OR journal record wins ────
    existing_sidecar = run_sidecar_path(experiment_dir, run_id)
    if existing_sidecar.is_file():
        raise errors.SpecInvalid(
            f"adopt-run: run_id {run_id!r} already has a sidecar at "
            f"{existing_sidecar} — adopt-run never clobbers an existing record. "
            "Inspect it (status-snapshot / read-run-sidecar), or adopt under a "
            "distinct run_id."
        )
    if load_run(experiment_dir, run_id) is not None:
        raise errors.SpecInvalid(
            f"adopt-run: run_id {run_id!r} already has a journal record — "
            "adopt-run never clobbers an existing record. Inspect it "
            "(status-snapshot), or adopt under a distinct run_id."
        )

    # ── terminal-evidence doctrine (same as settle-run): empty ⇒ refuse ───────
    evidence = (spec.terminal_evidence or "").strip()
    if not in_flight and not evidence:
        raise errors.SpecInvalid(
            "adopt-run: terminal_evidence is required when job_ids is absent — an "
            "already-terminal adoption journals WHAT proves the terminal state "
            "(e.g. 'reporter RC=0 all-100; result tree on disk'). An adoption "
            "with no evidence is a bare status flip (the settle-run doctrine)."
        )
    if evidence:
        # Checker-path obligation 3 (harness-contract): terminal_evidence is a
        # human-authored input — the same tiered authorship lock settle-run
        # applies at its intake (lock when an utterance log exists; disclosed
        # unverified fallback otherwise).
        from hpc_agent.ops.decision.journal import assert_elicited_value_human_authored

        assert_elicited_value_human_authored(
            experiment_dir, field="terminal_evidence", value=evidence
        )

    # ── (2) layout inference (LOCAL only; elicit, never guess) ────────────────
    result_dir_template = spec.result_dir_template
    task_count = spec.task_count
    if result_dir_template is None or task_count is None:
        missing = [
            name
            for name, val in (
                ("result_dir_template", result_dir_template),
                ("task_count", task_count),
            )
            if val is None
        ]
        if spec.results_sample is None:
            return AdoptRunResult(
                stage_reached="needs_elicitation",
                needs_decision=True,
                reason=(
                    f"adopt-run needs {' + '.join(missing)} and no results_sample "
                    "was given to infer from — supply them explicitly, or a LOCAL "
                    "results_sample path/glob anchored on the run's task dirs. "
                    "Nothing was written."
                ),
                run_id=run_id,
                cmd_sha=cmd_sha,
            )
        inferred = _infer_layout(spec.results_sample, summary_artifact)
        if isinstance(inferred, str):
            return AdoptRunResult(
                stage_reached="needs_elicitation",
                needs_decision=True,
                reason=(
                    f"adopt-run could not infer {' + '.join(missing)}: {inferred}. "
                    "Supply result_dir_template + task_count explicitly. Nothing "
                    "was written."
                ),
                run_id=run_id,
                cmd_sha=cmd_sha,
            )
        inferred_template, inferred_count = inferred
        # Caller-supplied values always win; inference fills only the gaps.
        result_dir_template = result_dir_template or inferred_template
        task_count = task_count or inferred_count

    # ── (3) sidecar via the EXISTING write-run-sidecar primitive ──────────────
    # Its guards apply unchanged: real-per-task-command check, dispatcher
    # refusal, per-task result-dir isolation (wire model), and the
    # .hpc/tasks.py identity cross-check. job_ids is passed through — the
    # documented foreign-run case for setting it at write time.
    sidecar_spec = WriteRunSidecarInput(
        run_id=run_id,
        cmd_sha=cmd_sha,
        executor=spec.executor or spec.command,
        result_dir_template=result_dir_template,
        task_count=task_count,
        cluster=spec.cluster,
        profile=spec.profile,
        remote_path=spec.remote_path,
        resources=spec.resources,
        summary_artifact=spec.summary_artifact,
        job_ids=list(spec.job_ids) if spec.job_ids else None,
        # Durable adoption marker, composed through the existing extra
        # pass-through (recorded verbatim by state.runs.write_run_sidecar).
        extra={"adopted": {"by": "adopt-run", "at": utcnow_iso()}},
    )
    written = write_run_sidecar(experiment_dir=Path(experiment_dir), spec=sidecar_spec)
    sidecar_path = str(written["path"])

    # ── (4) journal record — records WITHOUT submitting (no scheduler call) ───
    profile = spec.profile or "adopted"
    job_name = spec.job_name or run_id
    if in_flight:
        from hpc_agent._wire.actions.submit import SubmitSpec
        from hpc_agent.ops.submit.runner import submit_and_record

        record, _deduped = submit_and_record(
            Path(experiment_dir),
            spec=SubmitSpec(
                profile=profile,
                cluster=spec.cluster,
                ssh_target=spec.ssh_target,
                remote_path=spec.remote_path,
                job_name=job_name,
                run_id=run_id,
                job_ids=list(spec.job_ids or []),
                total_tasks=task_count,
            ),
            # cmd_sha deliberately NOT threaded: the A5 journal-wiped dedup scan
            # replays PRIOR submissions; adoption's own step-1 refusal already
            # guarantees this run_id is fresh on both stores.
        )
        return AdoptRunResult(
            stage_reached="adopted_in_flight",
            needs_decision=False,
            reason=(
                f"adopted {run_id!r} in-flight (jobs {', '.join(record.job_ids)}) — "
                "sidecar + journal record written, no scheduler call made"
            ),
            run_id=run_id,
            cmd_sha=cmd_sha,
            status=record.status,
            job_ids=list(record.job_ids),
            task_count=task_count,
            result_dir_template=result_dir_template,
            sidecar_path=sidecar_path,
            next_block={
                "verb": "status-watch",
                "why": (
                    "the adopted run is live on the scheduler — watch it to "
                    "terminal, then aggregate-check; verify-reproduction "
                    "external-baseline is the claim-comparison path"
                ),
                "spec_hint": {"run_id": run_id},
            },
        )

    # Terminal branch: mirror submit_and_record's fresh-record construction
    # with job_ids=[] (SubmitSpec refuses an empty job list by design — it
    # guards real submissions), then settle through the settle-run mechanism.
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        Path(experiment_dir),
        RunRecord(
            run_id=run_id,
            profile=profile,
            cluster=spec.cluster,
            ssh_target=spec.ssh_target,
            remote_path=spec.remote_path,
            job_name=job_name,
            job_ids=[],
            total_tasks=int(task_count),
            submitted_at=utcnow_iso(),
            experiment_dir=str(Path(experiment_dir).resolve()),
        ),
    )

    # ── (5) settle via the settle-run MECHANISM on directed evidence ──────────
    # Same steps as ops.settle_run (append_decision sign-off → typed
    # last_status → mark_run → receipt-gated harvest), keyed block="adopt-run"
    # so the sign-off names the verb that took the evidence.
    from hpc_agent.ops.monitor.harvest_guard import harvest_on_terminal, harvest_receipt_exists
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import mark_run, update_run_status

    status = "complete"
    append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=run_id,
        block="adopt-run",
        response="y",
        proposal=evidence,
        resolved={"status": status, "terminal_cause": status},
        provenance={
            "directed": True,
            "kind": "adopt-run-directed-settle",
            "evidence": evidence,
            "source": "adopt-run",
        },
    )
    update_run_status(
        experiment_dir,
        run_id,
        last_status={
            "verdict": status,
            "verdict_reason": "adopt_run_directed_settle",
            "verdict_source": "human_directed",
            "evidence": evidence,
            "checked_at": utcnow_iso(),
        },
    )
    prior_status = "in_flight"  # the record was minted just above
    updated = mark_run(experiment_dir, run_id, status=status)
    # Same receipt gate as settle-run: fire on the transition (always, here —
    # the record was just minted in_flight) OR on the no-receipt backstop.
    if status != prior_status or not harvest_receipt_exists(experiment_dir, run_id):
        harvest_on_terminal(
            experiment_dir,
            run_id,
            terminal_cause=status,
            record=updated,
            _aggregate=_aggregate,
            _sweep=_sweep,
        )

    return AdoptRunResult(
        stage_reached="adopted_terminal",
        needs_decision=False,
        reason=(
            f"adopted {run_id!r} as terminal ({status}) on directed evidence — "
            "sidecar + journal record written and settled through the "
            "settle-run mechanism"
        ),
        run_id=run_id,
        cmd_sha=cmd_sha,
        status=status,
        job_ids=[],
        task_count=task_count,
        result_dir_template=result_dir_template,
        sidecar_path=sidecar_path,
        next_block={
            "verb": "aggregate-check",
            "why": (
                "the adopted run is terminal — aggregate-check verifies "
                "readiness, aggregate-run computes the numbers in code; "
                "verify-reproduction external-baseline is the claim-comparison "
                "path"
            ),
            "spec_hint": {"run_id": run_id},
        },
    )
