"""SGE (Sun/Univa Grid Engine) backend — a thin profile binding.

All scheduler behaviour now lives in
:class:`hpc_agent.infra.backends._engine.ProfileBackend`, driven by
:data:`hpc_agent.infra.backends.profile.SGE_PROFILE`. This class only
carries the SGE constructor (log-dir env fallback, ``pass_env_keys``)
and binds the profile.

The wire-facing ``backend`` value ``"sge"`` resolves to
:class:`hpc_agent.infra.backends.sge_remote.RemoteSGEBackend` (the
remote-over-ssh subclass). This local class is not registered but
remains the base class for the remote subclass and for
``get_backend('sge')``.
"""

from __future__ import annotations

import os

from hpc_agent import errors
from hpc_agent.infra.backends._engine import ProfileBackend
from hpc_agent.infra.backends.profile import SGE_PROFILE


class SGEBackend(ProfileBackend):
    """SGE submission via ``qsub`` (capability metadata derived from the profile)."""

    profile = SGE_PROFILE

    def __init__(
        self,
        script: str | None = None,
        log_dir: str | None = None,
        pass_env_keys: tuple[str, ...] = (),
    ):
        if script is None:
            raise errors.SpecInvalid("SGEBackend requires a 'script' path")
        self.script = script
        self.log_dir = log_dir or os.environ.get("SGE_LOG_DIR", "logs")
        self.pass_env_keys = pass_env_keys
