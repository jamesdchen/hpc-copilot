"""Skeleton ``HPCBackend`` for the Vast.ai GPU marketplace.

Every compute method raises ``NotImplementedError`` whose message says
what the real implementation must do — this file is a typed map from
the host's backend capability hooks onto a crowd-compute platform, not
working code. Vast.ai is the first target because its rented instances
are SSH-able, so it sits closest to the existing remote machinery;
pure-API platforms (SaladCloud, Akash) implement the same hooks
against their job APIs.

The conceptual mapping ("scheduler" vocabulary -> marketplace):

======================  =============================================
Scheduler concept       Vast.ai equivalent
======================  =============================================
array job of N tasks    N rented instances (or a work-queue over a
                        smaller pool), one container per task
job id                  instance/contract id from the create call
qstat alive check       instances list API, filtered to our label
scheduler state token   instance status (e.g. created/loading/
                        running/exited) — verify against the API
stderr log path         instance logs API call, not a filesystem path
preemption              interruptible-instance outbid -> map to the
                        host's ``preempted`` error code
==========================================================================
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent.infra.backends import BackendBuildContext, HPCBackend, register

if TYPE_CHECKING:
    from hpc_agent.infra.throughput import SubmissionPlan

#: Env vars the backend reads its configuration from — the
#: marketplace-shaped replacement for the SSH ssh_run/remote_repo pair
#: the built-in backends take (see ``from_build_context``).
API_KEY_ENV = "VAST_API_KEY"
IMAGE_ENV = "HPC_VASTAI_IMAGE"


@register("vastai")
class VastAIBackend(HPCBackend):
    """Vast.ai marketplace backend — skeleton.

    Parameters
    ----------
    image:
        Container image reference for the executor (see
        ``examples/crowd-compute-executor/``). The image is the
        deployment unit; there is no ``deploy_runtime`` rsync step.
    api_key:
        Marketplace API key; defaults to ``$VAST_API_KEY``.
    label:
        Marketplace-side label stamped on every instance this run
        creates, so the alive/state queries can filter to our jobs.
    """

    scheduler_name = "vastai"
    template_ext = ".sh"
    supports_test_only_eta = False

    def __init__(
        self,
        image: str | None = None,
        api_key: str | None = None,
        label: str = "hpc-agent",
    ) -> None:
        self.image = image
        self.api_key = api_key or os.environ.get(API_KEY_ENV)
        self.label = label
        # Required base attribute. There is no remote log *directory* on
        # a marketplace — logs come from the instances API — so the
        # local path only holds fetched copies.
        self.log_dir = os.path.join(".hpc", "vastai-logs")

    @classmethod
    def from_build_context(cls, ctx: BackendBuildContext) -> VastAIBackend:
        """Construct from the host's submit-flow build context.

        The host's construction seam hands every registered non-built-in
        backend the full :class:`BackendBuildContext`; this backend is
        marketplace-shaped, so it deliberately ignores the SSH fields
        (``ctx.ssh_target`` / ``ctx.ssh_run`` / ``ctx.remote_path``) and
        reads its configuration from the environment instead
        (``$VAST_API_KEY``, ``$HPC_VASTAI_IMAGE``).
        """
        return cls(
            image=os.environ.get(IMAGE_ENV),
            label=f"hpc-agent-{ctx.backend_name}",
        )

    # ------------------------------------------------------------------
    # Submission. A marketplace has no shell submit command, so the
    # shell-command pipeline (_build_command -> _execute_command) is
    # bypassed: submit_plan is overridden wholesale.
    # ------------------------------------------------------------------

    def _build_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        raise NotImplementedError("vastai submits via API, not a shell command; use submit_plan")

    def submit_plan(
        self,
        plan: SubmissionPlan,
        job_name: str,
        job_env: dict[str, str],
        *,
        cwd: Path | None = None,
    ) -> list[tuple[int, str, str]]:
        """Rent instances and launch one executor container per batch.

        Overrides the host's surviving submission primitive
        (:meth:`HPCBackend.submit_plan`) wholesale, since a marketplace has
        no shell submit command for the base loop's ``_build_command ->
        _execute_command`` pipeline to drive. The real implementation:
        walk ``plan.batches`` in wave order, search offers (GPU type/price
        from the submit spec), and for each batch create one interruptible
        instance per task — or a work-queue over the batch's
        ``array_size`` — passing ``self.image`` with per-task env
        (``HPC_TASK_ID``, ``HPC_KW_*`` from *job_env*, ``RESULT_DIR=/out``)
        and ``self.label``; return ``(batch.wave, batch.task_range,
        instance_id)`` triples, the same shape the base loop yields from
        qsub/sbatch stdout. Inter-wave dependencies (the base loop's
        scheduler ``afterok`` chain) map to gating each wave's
        instance-create on the prior wave's instances reaching a terminal
        state via the API.
        """
        raise NotImplementedError("vastai instance-create call not implemented")

    # ------------------------------------------------------------------
    # Liveness / state capability hooks (host polls these from status,
    # reconcile, and the abandoned-run detector).
    # ------------------------------------------------------------------

    def alive_job_ids(self, job_ids: list[str]) -> list[str]:
        """Subset of *job_ids* still known to the marketplace.

        Real implementation: list instances filtered by ``self.label``
        and intersect with *job_ids*. An id absent from the listing is
        gone — the host marks it ``abandoned`` on reconcile, which is
        also how an outbid interruptible instance surfaces if the
        state query missed the transition.
        """
        raise NotImplementedError("vastai instances-list call not implemented")

    @staticmethod
    def parse_scheduler_states(stdout: str, job_ids: list[str]) -> dict[str, str]:
        """Map instance ids to raw status tokens from the API response."""
        raise NotImplementedError("vastai state query not implemented")

    @staticmethod
    def classify_scheduler_state(state: str) -> str:
        """Bucket a raw instance status into ``alive`` / ``error`` / ``held``.

        Real implementation: verify the platform's status vocabulary
        against its API docs before encoding it here, and map an
        outbid/interrupted status to the host's ``preempted`` handling
        in the failures pipeline.
        """
        raise NotImplementedError("vastai status vocabulary not encoded yet")

    @staticmethod
    def stderr_log_path(remote_path: str, job_name: str, job_id: str, task_id: int) -> str:
        """Marketplace logs are an API call, not a path.

        Real implementation: fetch via the logs endpoint into
        ``log_dir`` and return that local copy's path, preserving the
        host's read-a-path contract.
        """
        raise NotImplementedError("vastai logs fetch not implemented")
