"""Factory for SSH-backed scheduler clients (``RemoteSGEBackend`` /
``RemoteSlurmBackend``).

The submit and recover subjects both need to build the same remote
backend object: same SSH transport, same scheduler dispatch, same
env-key forwarding. Living here means neither subject reaches into
the other's source tree.

The function raises :class:`~hpc_agent.errors.SpecInvalid` on an
unknown scheduler name — the same typed envelope error the callers
surface to the agent. Callers MUST validate ``ssh_target`` before
calling (see :func:`hpc_agent.infra.remote.validate_ssh_target`); the
factory does not double-validate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hpc_agent import errors
from hpc_agent.infra.backends.sge_remote import RemoteSGEBackend
from hpc_agent.infra.backends.slurm_remote import RemoteSlurmBackend
from hpc_agent.infra.remote import ssh_run

if TYPE_CHECKING:
    from hpc_agent.infra.backends import HPCBackend
    from hpc_agent.infra.backends._engine import RemoteProfileBackend
    from hpc_agent.state.run_record import RunRecord

__all__ = ["backend_for_record", "build_remote_backend"]


def build_remote_backend(
    *,
    backend_name: str,
    script: str,
    ssh_target: str,
    remote_path: str,
    pass_env_keys: tuple[str, ...] | None,
    job_env_keys: tuple[str, ...],
    slurm_account: str | None = None,
    slurm_cluster: str | None = None,
    scheduler_profile: dict[str, object] | None = None,
) -> HPCBackend:
    """Construct the right ``HPCBackend`` for the requested scheduler.

    Both SGE and SLURM go through the cluster's login node via SSH —
    the local backends (which assume a local ``qsub``/``sbatch``
    binary) are never used here. ``submit-flow`` and ``resubmit-failed``
    are both laptop-driven.

    When *scheduler_profile* is given (a pinned / resolved
    :class:`~hpc_agent.infra.backends.profile.SchedulerProfile` dict from
    a cluster's ``clusters.yaml`` entry), the backend is built *bound to
    that profile* — the family it declares (``slurm`` / ``sge``) selects
    the command grammar and its data (regex, scripts, error vocabulary)
    overrides the golden default. This is how a non-default cluster's
    customised scheduler reaches the engine. Without it, the golden
    ``slurm`` / ``sge`` backends are used exactly as before.

    A *backend_name* outside the built-in families resolves through the
    backend registry: a plugin-registered backend constructs itself via
    :meth:`HPCBackend.from_build_context` (the SSH-on-a-login-node
    assumption above applies only to the built-in ladder; a plugin
    backend owns its own transport decisions).
    """

    def ssh(cmd: str):
        return ssh_run(cmd, ssh_target=ssh_target)

    if scheduler_profile is not None:
        from hpc_agent.infra.backends import build_backend_class
        from hpc_agent.infra.backends.profile import SchedulerProfile

        profile = SchedulerProfile.from_dict(scheduler_profile)
        # The pin's family selects the command grammar AND dictates the
        # script extension; a `backend` that disagrees with it would emit
        # (say) sbatch flags against a `.sh` script. Refuse the mismatch
        # loudly rather than silently submit a broken job.
        if backend_name and backend_name != profile.family:
            raise errors.SpecInvalid(
                f"backend {backend_name!r} disagrees with the pinned "
                f"scheduler_profile family {profile.family!r}; the spec's "
                "backend must equal the profile's family."
            )
        # build_backend_class(remote=True) yields a RemoteProfileBackend
        # subclass whose __init__ takes these kwargs (the declared return
        # type[HPCBackend] is the structural supertype).
        cls = cast("type[RemoteProfileBackend]", build_backend_class(profile, remote=True))
        # Mirror the SGE env-forwarding rule: `[]`/`None` mean "forward
        # every job_env key"; only used by the sge family but harmless to
        # pass for slurm (which ignores pass_env_keys).
        keys = pass_env_keys if pass_env_keys else job_env_keys
        return cls(
            script=script,
            ssh_run=ssh,
            remote_repo=remote_path,
            account=slurm_account or "",
            cluster=slurm_cluster or "",
            pass_env_keys=tuple(keys),
        )

    if backend_name == "sge":
        # `[]`/`()` and `None` are EQUIVALENT here: both mean "forward every
        # job_env key". A truthiness test (not just `is not None`) is
        # load-bearing — `[] is not None` is True, so the old check let an
        # explicit empty list strip every var from qsub -v, shipping a job with
        # $EXECUTOR/$CONDA_ENV/$REPO_DIR all unset (#192). The wire layer now
        # also refuses `[]` at construction, so this is defense-in-depth for any
        # caller that bypasses the spec validator.
        keys = pass_env_keys if pass_env_keys else job_env_keys
        return RemoteSGEBackend(
            script=script,
            ssh_run=ssh,
            remote_repo=remote_path,
            pass_env_keys=tuple(keys),
        )
    if backend_name == "slurm":
        return RemoteSlurmBackend(
            script=script,
            ssh_run=ssh,
            remote_repo=remote_path,
            account=slurm_account,
            cluster=slurm_cluster,
        )
    if backend_name in ("pbspro", "torque"):
        # PBS forks have no dedicated remote class; build from the golden
        # profile via the engine factory (same path a pinned profile takes).
        from hpc_agent.infra.backends import build_backend_class
        from hpc_agent.infra.backends.profile import PBSPRO_PROFILE, TORQUE_PROFILE

        profile = PBSPRO_PROFILE if backend_name == "pbspro" else TORQUE_PROFILE
        cls = cast("type[RemoteProfileBackend]", build_backend_class(profile, remote=True))
        keys = pass_env_keys if pass_env_keys else job_env_keys
        return cls(
            script=script,
            ssh_run=ssh,
            remote_repo=remote_path,
            pass_env_keys=tuple(keys),
        )

    # Construction seam for plugin-registered backends
    # (docs/proposals/crowd-compute-backend.md, core edit #2). A name the
    # ladder above doesn't know but the registry does — a plugin's
    # ``@register`` ran — constructs itself from the whole build context:
    # the backend, not this factory, decides which fields it needs (a
    # pure-API backend ignores the SSH pair; an SSH-shaped one reuses
    # ``ctx.ssh_run``). ``registered_backend_names`` also imports plugin
    # modules, so the check is registration-order independent. A backend
    # that hasn't overridden ``from_build_context`` raises
    # NotImplementedError loudly, per the capability-hook convention.
    from hpc_agent.infra.backends import (
        BackendBuildContext,
        get_backend_class,
        registered_backend_names,
    )

    if backend_name in registered_backend_names():
        ctx = BackendBuildContext(
            backend_name=backend_name,
            script=script,
            ssh_target=ssh_target,
            remote_path=remote_path,
            pass_env_keys=pass_env_keys,
            job_env_keys=job_env_keys,
            slurm_account=slurm_account,
            slurm_cluster=slurm_cluster,
            ssh_run=ssh,
        )
        return get_backend_class(backend_name).from_build_context(ctx)
    raise errors.SpecInvalid(f"unknown backend: {backend_name!r}")


def backend_for_record(record: RunRecord, *, scheduler: str | None = None) -> HPCBackend:
    """Build the ``HPCBackend`` instance for an in-flight run *record*.

    The construction seam the monitor (#337 Increment 4) and aggregate
    (Increment 5) pure-API transports share: their liveness / logs / results
    go through a backend's *instance* hooks (``alive_job_ids`` / ``fetch_logs``
    / ``fetch_results``), which need a constructed backend — a pure-API backend
    holds an authenticated client the SSH-era ``@staticmethod`` hooks cannot.

    Lives here (``infra.backends``), not under a subject, so any subject builds
    a backend from a record without reaching into another subject's tree — and
    so a built-in SSH family and a registered pure-API plugin backend construct
    identically via :func:`build_remote_backend` → ``from_build_context``. The
    caller branches on ``backend.requires_ssh``, never on the name. *scheduler*
    overrides ``record.backend`` for callers (reconcile / status) that already
    hold the name; otherwise the name recorded on the run is used.
    """
    from hpc_agent.infra.clusters import resolve_ssh_target

    return build_remote_backend(
        backend_name=scheduler or record.backend,
        script=record.script,
        ssh_target=resolve_ssh_target(record),
        remote_path=record.remote_path,
        pass_env_keys=None,
        job_env_keys=tuple(record.job_env or ()),
    )
