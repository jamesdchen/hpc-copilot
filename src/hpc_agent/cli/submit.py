"""Submit-domain argparse adapters — Tier 2 (handler escape hatch).

Each ``cmd_*`` is a hand-written shim around its corresponding
primitive. The registry-driven dispatcher delegates wholly to these via
``CliShape(handler=...)`` because each one carries branching shape the
auto-dispatcher cannot model:

* :func:`cmd_submit` — manual required-field check; dry-run emits a
  different envelope shape than the success path (``would_launch`` /
  ``dry_run`` vs ``run_id`` / ``job_ids``).
* :func:`cmd_submit_flow` — auto-routes to :func:`cmd_submit_flow_batch`
  when ``spec.specs`` is a list; injects ``--partial-ok`` into the spec
  dict before validation; dry-run shape diverges from success.
* :func:`cmd_submit_flow_batch` — runs TWO schema passes (wrapper +
  per-entry against ``submit_flow.input.json``) and the dry-run shape
  surfaces ``would_launch``/``shared_targets`` instead of ``results``.

Helpers come from :mod:`hpc_agent.cli._helpers` (the adapter SDK) —
external plugins import the same symbols, so the adapter contract here
is the same one ``hpc-agent-pro`` consumes.
"""

from __future__ import annotations

import argparse

from hpc_agent import errors
from hpc_agent.cli._helpers import (
    EXIT_OK,
    _err,
    _load_spec,
    _ok,
    _validate_against_schema,
)


def cmd_submit(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec, schema_name=None)
    _validate_against_schema(spec, "submit")
    required = (
        "profile",
        "cluster",
        "ssh_target",
        "remote_path",
        "job_name",
        "run_id",
        "job_ids",
        "total_tasks",
    )
    missing = [k for k in required if k not in spec]
    if missing:
        raise errors.SpecInvalid(
            f"--spec missing required fields: {missing}. See docs/reference/cli-spec.md."
        )

    if args.dry_run:
        # Skip ``name=...`` so the dry-run-specific shape isn't validated
        # against ``SubmitResult`` (which requires job_ids / total_tasks
        # / deduped and forbids would_launch / dry_run).
        _ok(
            {
                "would_launch": int(spec["total_tasks"]),
                "profile": spec["profile"],
                "cluster": spec["cluster"],
                "run_id": spec["run_id"],
                "dry_run": True,
            },
            idempotent=True,
        )
        return EXIT_OK

    from hpc_agent._wire.actions.submit import SubmitSpec as _SubmitSpec
    from hpc_agent.ops.submit.runner import submit_and_record

    record, deduped = submit_and_record(
        args.experiment_dir,
        spec=_SubmitSpec.model_validate(spec),
    )
    _ok(
        {
            "run_id": record.run_id,
            "job_ids": record.job_ids,
            "total_tasks": record.total_tasks,
            "deduped": deduped,
        },
        name="submit-spec",  # honest now that submit_and_record dedups
    )
    return EXIT_OK


def cmd_submit_flow(args: argparse.Namespace) -> int:
    """Workflow atom — pre-flight + rsync + deploy + qsub + record in one shot.

    See ``hpc_agent/job/submit_flow.py`` for the pipeline contract
    and ``schemas/submit_flow.{input,output}.json`` for the envelope
    shapes. Idempotent on ``run_id`` via the same dedup mechanism as
    ``submit``.

    **Auto-dispatch**: if the loaded spec is a batch shape (an object
    with a ``specs`` list, matching ``submit_flow_batch.input.json``)
    this subcommand transparently routes to
    :func:`cmd_submit_flow_batch`. Single-spec callers see no change;
    multi-spec callers don't have to know about a separate CLI.
    """
    spec = _load_spec(args.spec, schema_name=None)
    # Auto-dispatch: any shape that the batch CLI accepts (an object
    # with a `specs` list) routes there, bypassing the per-spec path.
    # Lets the slash command always say "call submit-flow" and stay
    # right whether the iteration emits 1 spec or N.
    if isinstance(spec, dict) and isinstance(spec.get("specs"), list):
        return cmd_submit_flow_batch(args)

    from hpc_agent.ops.submit_flow import submit_flow

    # Surface --partial-ok at the CLI in addition to spec.partial_ok so a
    # caller can opt in via either path. Flag wins over spec when both
    # are set (CLI is the more explicit override).
    if getattr(args, "partial_ok", False):
        spec = dict(spec)
        spec["partial_ok"] = True
    _validate_against_schema(spec, "submit_flow")

    if args.dry_run:
        # The dry-run path reads spec fields directly instead of going
        # through ``SubmitFlowSpec.model_validate``; guard the required
        # keys so a missing field is a clean spec_invalid (exit 1) rather
        # than a bare KeyError → generic handler → exit 3.
        required = ("total_tasks", "profile", "cluster", "run_id")
        missing = [k for k in required if k not in spec]
        if missing:
            raise errors.SpecInvalid(
                f"submit-flow --dry-run spec missing required field(s): {', '.join(missing)}"
            )
        # Skip ``name=...`` so the dry-run-specific shape isn't validated
        # against ``SubmitFlowResult`` (which requires job_ids /
        # canary_done and forbids the dry-run-only fields).
        _ok(
            {
                "would_launch": int(spec["total_tasks"]),
                "profile": spec["profile"],
                "cluster": spec["cluster"],
                "run_id": spec["run_id"],
                "canary": bool(spec.get("canary", True)),
                "dry_run": True,
            },
            idempotent=True,
        )
        return EXIT_OK

    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    submit_spec = SubmitFlowSpec.model_validate(spec)
    result = submit_flow(args.experiment_dir, spec=submit_spec)
    _ok(result.to_envelope_data(), name="submit-flow")
    return EXIT_OK


def cmd_submit_flow_batch(args: argparse.Namespace) -> int:
    """Workflow atom — submit N specs sharing one (ssh_target, remote_path).

    The bundle does ONE rsync_push + ONE deploy_runtime + N × (qsub +
    record), reusing the ssh ControlMaster across qsubs. This is the
    correct shape for campaign-time fan-out (e.g. 5 lgbm-tune
    submissions sharing one cluster) — the per-spec submit_flow path
    fired N × 13 ssh handshakes which tripped MaxStartups on CARC.

    Spec file is a JSON list of submit-flow specs (each matching
    ``schemas/submit_flow.input.json``); all entries MUST share
    ssh_target and remote_path. The CLI emits one envelope wrapping
    a list of per-spec result records.
    """
    from hpc_agent._wire.workflows.submit_flow_batch import SubmitFlowBatchSpec
    from hpc_agent.ops.submit_flow import submit_flow_batch

    raw = _load_spec(args.spec, schema_name=None)
    # Wrapper-shape validation (object with `specs` array, per-entry
    # required keys via submit_flow_batch.input.json), then full per-entry
    # validation against submit_flow.input.json. The two schemas overlap
    # on the required-keys check; the wrapper exists so an agent / external
    # orchestrator can sanity-check the bundle in one call.
    _validate_against_schema(raw, "submit_flow_batch")
    if not isinstance(raw, dict) or "specs" not in raw:
        return _err(
            error_code="spec_invalid",
            message="submit-flow-batch spec must be an object with a 'specs' list",
            category="user",
            retry_safe=False,
        )
    for entry in raw["specs"]:
        _validate_against_schema(entry, "submit_flow")
    batch_spec = SubmitFlowBatchSpec.model_validate(raw)

    if args.dry_run:
        targets = sorted({(s.ssh_target, s.remote_path) for s in batch_spec.specs})
        # Skip ``name=...`` so the dry-run-specific shape isn't validated
        # against ``SubmitFlowBatchResult`` (which requires results /
        # n_results and forbids the dry-run-only fields).
        _ok(
            {
                "would_launch": [
                    {"run_id": s.run_id, "tasks": s.total_tasks} for s in batch_spec.specs
                ],
                "shared_targets": [{"ssh_target": t[0], "remote_path": t[1]} for t in targets],
                "n_specs": len(batch_spec.specs),
                "dry_run": True,
            },
            idempotent=True,
        )
        return EXIT_OK

    results = submit_flow_batch(args.experiment_dir, spec=batch_spec)
    _ok(
        {"results": [r.to_envelope_data() for r in results], "n_results": len(results)},
        name="submit-flow-batch",
    )
    return EXIT_OK
