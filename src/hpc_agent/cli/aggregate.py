"""``aggregate`` (``combine-wave``) argparse adapter — Tier 2 handler.

Hand-written CLI body for the ``combine-wave`` primitive. The CLI verb
is ``aggregate`` (legacy name retained for slash-command compatibility);
the underlying primitive — registered in :mod:`hpc_agent.ops.aggregate.combine`
— is ``combine-wave``.

The combiner pipeline is driven by :func:`hpc_agent.runner.combine_wave`
plus the user-supplied combiner script on the cluster. The CLI wraps it
with three optional, framework-agnostic guarantees:

* ``--require-outputs <template>`` — every per-task output exists before
  the combiner runs (precondition).
* ``--expect-output <path>`` — the combiner produced a parseable artifact
  at ``<path>`` (postcondition).
* provenance — metadata block in ``envelope.data`` and sidecar file when
  ``--expect-output`` is set.

Defaults for require/expect can be set per-run in the sidecar's
``aggregate_defaults`` block, populated by ``/submit``. CLI flags win.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any

from hpc_agent import errors, runner
from hpc_agent._internal import session
from hpc_agent.cli._helpers import EXIT_OK, _err_from_hpc, _ok, _require_ssh_agent

if TYPE_CHECKING:
    from pathlib import Path


def _sidecar_aggregate_defaults(experiment_dir: Path, run_id: str) -> dict[str, str]:
    """Read ``aggregate_defaults.{require_outputs,expect_output}`` from the run sidecar.

    Returns an empty dict when the sidecar is missing, malformed, or has
    no ``aggregate_defaults`` block. Silent failure is intentional —
    config validity is enforced by ``/submit``, not the aggregate path.
    """
    try:
        from hpc_agent.state.runs import read_run_sidecar
    except ImportError:
        return {}
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    block = sidecar.get("aggregate_defaults") or {}
    if not isinstance(block, dict):
        return {}
    return {
        k: block[k] for k in ("require_outputs", "expect_output") if isinstance(block.get(k), str)
    }


def cmd_aggregate(args: argparse.Namespace) -> int:
    # The aggregation pipeline is driven by hpc_agent.runner.combine_wave
    # plus the user-supplied combiner script on the cluster. The CLI wraps it
    # with three optional, framework-agnostic guarantees:
    #   --require-outputs <template>  : every per-task output exists before
    #                                   the combiner runs (precondition)
    #   --expect-output <path>        : the combiner produced a parseable
    #                                   artifact at <path> (postcondition)
    #   provenance                    : metadata block in envelope.data and
    #                                   sidecar file when --expect-output set
    # Defaults for require/expect can be set per-run in the sidecar's
    # ``aggregate_defaults`` block, populated by /submit. CLI flags win.
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for run_id {args.run_id!r}")
    if args.wave is None:
        raise errors.SpecInvalid("aggregate requires --wave <int>")

    # Resolve aggregate flags: explicit CLI > sidecar aggregate_defaults > none.
    # ``getattr`` keeps in-process callers (tests, slash-command wrappers)
    # working even when they hand-build a Namespace without these keys.
    defaults = _sidecar_aggregate_defaults(args.experiment_dir, args.run_id)
    require_outputs = getattr(args, "require_outputs", None) or defaults.get("require_outputs")
    expect_output = getattr(args, "expect_output", None) or defaults.get("expect_output")

    # Precondition: every per-task output must exist before we combine.
    if require_outputs:
        missing = runner.verify_per_task_outputs(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            run_id=args.run_id,
            wave=int(args.wave),
            template=require_outputs,
        )
        if missing:
            preview = missing[:10]
            ellipsis = "..." if len(missing) > 10 else ""
            return _err_from_hpc(
                errors.OutputsMissing(
                    f"{len(missing)} per-task output(s) missing for wave "
                    f"{args.wave}: {preview}{ellipsis}",
                )
            )

    ok, stdout, stderr = runner.combine_wave(
        args.experiment_dir,
        args.run_id,
        wave=int(args.wave),
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        force=args.force,
    )
    if ok:
        # Postcondition: the combiner must have produced the declared file.
        if expect_output:
            artifact_ok, detail = runner.verify_combiner_artifact(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                expect_output=expect_output,
            )
            if not artifact_ok:
                return _err_from_hpc(
                    errors.CombinerFailed(
                        f"combiner returned 0 but expected output {expect_output!r} {detail}",
                    )
                )

        provenance = runner.build_provenance(record, wave=int(args.wave))
        sidecar_path: str | None = None
        if expect_output:
            try:
                sidecar_path = runner.write_remote_provenance(
                    ssh_target=record.ssh_target,
                    remote_path=record.remote_path,
                    expect_output=expect_output,
                    provenance=provenance,
                )
            except errors.RemoteCommandFailed:
                # Best-effort — envelope still carries provenance.
                sidecar_path = None

        data: dict[str, Any] = {
            "run_id": args.run_id,
            "wave": int(args.wave),
            "combined": True,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
            "provenance": provenance,
        }
        if sidecar_path is not None:
            data["provenance_sidecar"] = sidecar_path
        # NOTE: cmd_aggregate has its own envelope shape (run_id + wave +
        # combined + provenance + tails) distinct from the ``combine-wave``
        # primitive's output schema (which mandates output_dir for the
        # cluster-side caller). The validate_output bypass is intentional
        # here; a dedicated ``aggregate-cli`` schema would be the right
        # forward fix, but is out of scope for this audit pass.
        _ok(data, idempotent=True)
        return EXIT_OK
    # Combiner returned non-zero — surface as a typed error so the
    # envelope's ``ok`` field and the exit code stay in sync.  Tail of
    # stderr was already in the success payload; here we put it in the
    # human-readable message so the caller can grep it.
    return _err_from_hpc(
        errors.CombinerFailed(
            f"combiner returned non-zero for wave {args.wave}; stderr tail: {stderr[-500:]!r}"
        )
    )


__all__ = ["cmd_aggregate"]
