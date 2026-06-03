"""SLURM backend — a thin profile binding over the profile-driven engine.

All scheduler behaviour (command assembly, state classification, log
paths, script rendering) now lives in
:class:`hpc_agent.infra.backends._engine.ProfileBackend`, driven by
:data:`hpc_agent.infra.backends.profile.SLURM_PROFILE`. This class only
carries the SLURM constructor (account / cluster / log-dir env
fallbacks) and binds the profile.

The wire-facing ``backend`` value ``"slurm"`` resolves to
:class:`hpc_agent.infra.backends.slurm_remote.RemoteSlurmBackend` (the
remote-over-ssh subclass). This local class is not registered — nothing
in src/ or tests/ submits from a local SLURM shell — but it remains the
base class for the remote subclass and for ``get_backend('slurm')``.
"""

from __future__ import annotations

import os

from hpc_agent import errors
from hpc_agent.infra.backends._engine import ProfileBackend
from hpc_agent.infra.backends.profile import SLURM_PROFILE


class SlurmBackend(ProfileBackend):
    """SLURM submission via ``sbatch`` (capability metadata derived from the profile)."""

    profile = SLURM_PROFILE

    def __init__(
        self,
        script: str | None = None,
        account: str | None = None,
        cluster: str | None = None,
        log_dir: str | None = None,
    ):
        if script is None:
            raise errors.SpecInvalid("SlurmBackend requires a 'script' path")
        self.script = script
        self.account = account or os.environ.get("SLURM_ACCOUNT", "")
        self.cluster = cluster or os.environ.get("SLURM_CLUSTER", "")
        self.log_dir = log_dir or os.environ.get("SLURM_LOG_DIR", "logs")
