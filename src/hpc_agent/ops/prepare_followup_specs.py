"""``prepare-followup-specs``: pre-stage monitor/aggregate specs at submit (#278).

From the state known at submit time, write two small pre-staged spec
files into the experiment directory — ``monitor_spec.json`` and
``aggregate_spec.json`` — so a later ``/monitor-hpc`` and
``/aggregate-hpc`` can honor the pre-staged spec instead of re-running
the operator interview.

Why the specs are small. ``monitor-flow`` and ``aggregate-flow`` only
strictly require ``run_id``: they derive cluster / ssh_target /
remote_path / and the rest from the run sidecar on disk. So each
pre-staged spec carries just ``run_id``, the ``cmd_sha`` staleness gate,
a sentinel ``null`` for the field that is genuinely an operator choice at
followup time (monitor: ``wait_terminal``; aggregate: ``allow_partial``
and ``stage``), and provenance stamps. A ``null`` operator-choice field
means "not decided at submit — the followup skill prompts for it"; it is
deliberately NOT defaulted here, so pre-staging never silently picks a
blocking-vs-snapshot monitor or a partial-vs-complete aggregate on the
operator's behalf.

The ``cmd_sha`` is the staleness gate. The consuming skill validates the
pre-staged ``cmd_sha`` against the journal before honoring the spec — if
the code changed since submit, the spec is stale and the skill falls back
to the interview. That gate is wired separately (by the parent); this
primitive only WRITES the files.

This verb performs pure LOCAL writes (two JSON files under the experiment
directory), so it does NOT touch the cluster — ``requires_ssh`` is
``False``.

I/O contracts:

* Input: see ``hpc_agent/schemas/prepare_followup_specs.input.json``.
* Output: a ``dict`` matching ``schemas/prepare_followup_specs.output.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.io import atomic_write_json
from hpc_agent.infra.time import utcnow_iso

__all__ = ["prepare_followup_specs"]

_PREPARED_BY = "prepare-followup-specs"


@primitive(
    name="prepare-followup-specs",
    verb="scaffold",
    side_effects=[
        SideEffect(
            "writes-followup-specs",
            "<experiment_dir>/monitor_spec.json + aggregate_spec.json",
        ),
    ],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Pre-stage monitor_spec.json + aggregate_spec.json into the "
            "experiment dir from submit-time state, so a later /monitor-hpc "
            "and /aggregate-hpc can skip the operator interview. Each spec "
            "carries run_id + a cmd_sha staleness gate + a null sentinel for "
            "the operator-choice field (monitor wait_terminal; aggregate "
            "allow_partial/stage). The consuming skill validates cmd_sha "
            "against the journal before honoring the pre-staged spec (#278)."
        ),
        verb="prepare-followup-specs",
        args=(
            CliArg(
                "--experiment-dir",
                type=str,
                required=True,
                help="Experiment directory to write the two pre-staged spec files into.",
            ),
            CliArg(
                "--run-id",
                type=str,
                required=True,
                help="Run id both followup specs target (the field monitor/aggregate require).",
            ),
            CliArg(
                "--cmd-sha",
                type=str,
                default=None,
                help="cmd_sha staleness gate; the consumer checks it against the journal first.",
            ),
            CliArg(
                "--profile",
                type=str,
                default=None,
                help="Optional run profile (run_name) recorded in the aggregate spec.",
            ),
        ),
        # Pure local writes (two JSON files under experiment_dir). No SSH.
        requires_ssh=False,
    ),
    agent_facing=True,
)
def prepare_followup_specs(
    *,
    experiment_dir: str,
    run_id: str,
    cmd_sha: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Write the pre-staged ``monitor_spec.json`` + ``aggregate_spec.json``.

    Writes two JSON files under *experiment_dir*:

    * ``monitor_spec.json`` — ``run_id`` + ``cmd_sha`` gate + a ``null``
      ``wait_terminal`` sentinel (the operator's blocking-vs-snapshot
      choice, left undecided) + provenance.
    * ``aggregate_spec.json`` — ``run_id`` + ``profile`` + ``cmd_sha``
      gate + ``null`` ``stage`` and ``allow_partial`` sentinels (the
      operator's complete-vs-partial choices, left undecided) +
      provenance.

    Returns a dict matching ``schemas/prepare_followup_specs.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. The returned paths
    are the absolute paths of the two written files.

    Idempotent: keyed on ``run_id``, a re-run for the same run overwrites
    both files atomically with equivalent content (only the
    ``prepared_at`` timestamp refreshes).
    """
    exp_dir = Path(experiment_dir)
    prepared_at = utcnow_iso()

    monitor_path = exp_dir / "monitor_spec.json"
    aggregate_path = exp_dir / "aggregate_spec.json"

    monitor_spec = {
        "run_id": run_id,
        "cmd_sha": cmd_sha,
        # Operator choice at followup time (block until terminal vs. take a
        # snapshot). Left null at submit so pre-staging never silently picks.
        "wait_terminal": None,
        "prepared_by": _PREPARED_BY,
        "prepared_at": prepared_at,
    }
    aggregate_spec = {
        "run_id": run_id,
        "profile": profile,
        "cmd_sha": cmd_sha,
        # Operator choices at followup time (which stage to aggregate;
        # whether a partial result set is acceptable). Left null at submit.
        "stage": None,
        "allow_partial": None,
        "prepared_by": _PREPARED_BY,
        "prepared_at": prepared_at,
    }

    atomic_write_json(monitor_path, monitor_spec)
    atomic_write_json(aggregate_path, aggregate_spec)

    return {
        "monitor_spec_path": str(monitor_path),
        "aggregate_spec_path": str(aggregate_path),
        "run_id": run_id,
        "cmd_sha": cmd_sha,
    }
