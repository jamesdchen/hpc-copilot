"""Infrastructure modules: remote execution, GPU selection, scheduler backends.

The package-level re-exports (``load_clusters_config``, ``pick_gpu``,
``ssh_run``, ``rsync_push``, ``rsync_pull``, ``deploy_runtime``) are resolved
LAZILY through a module ``__getattr__`` (PEP 562), mirroring the root
``hpc_agent`` B3 pattern. Eager ``from .clusters import ...`` here used to drag
pydantic + yaml (``clusters`` ~0.33s) and the asyncssh/transport chain into the
package's ``__init__`` — so ANY ``from hpc_agent.infra.<submodule> import ...``
(e.g. the CLI's ``_helpers`` importing ``ssh_agent``) paid that tax before the
submodule even loaded. Deferring the re-exports keeps a submodule import cheap;
callers that want the heavy symbol still get it on first attribute access, and
every submodule import (the common case) pays nothing.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# Static-checker mirror of the runtime-lazy surface (PEP 562). mypy cannot see
# through a module ``__getattr__``; this block gives it the real types for
# ``hpc_agent.infra.<name>`` attribute access. Load-bearing twin of ``_LAZY``.
# MIRROR: hpc_agent.infra.__init__::_LAZY <-> this TYPE_CHECKING import block
#   pinned-by tests/contracts/test_eager_import_smoke.py::test_infra_reexport_lazy
if TYPE_CHECKING:
    from hpc_agent.infra.clusters import load_clusters_config
    from hpc_agent.infra.gpu import pick_gpu
    from hpc_agent.infra.remote import ssh_run
    from hpc_agent.infra.transport import deploy_runtime, rsync_pull, rsync_push

__all__ = [
    "load_clusters_config",
    "pick_gpu",
    "ssh_run",
    "rsync_push",
    "rsync_pull",
    "deploy_runtime",
]

# name -> "module.path.attr". Resolved on first access so importing a sibling
# submodule (or the package itself) does not eagerly pull pydantic/yaml/asyncssh.
_LAZY: dict[str, str] = {
    "load_clusters_config": "hpc_agent.infra.clusters.load_clusters_config",
    "pick_gpu": "hpc_agent.infra.gpu.pick_gpu",
    "ssh_run": "hpc_agent.infra.remote.ssh_run",
    "rsync_push": "hpc_agent.infra.transport.rsync_push",
    "rsync_pull": "hpc_agent.infra.transport.rsync_pull",
    "deploy_runtime": "hpc_agent.infra.transport.deploy_runtime",
}


def __getattr__(name: str) -> Any:
    """Resolve a lazily-deferred package re-export (PEP 562).

    Any other name raises an honest :class:`AttributeError`; a broken target
    path surfaces the underlying :class:`ImportError`/:class:`AttributeError`
    unswallowed.
    """
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'hpc_agent.infra' has no attribute {name!r}")
    module_path, _, attr = target.rpartition(".")
    return getattr(importlib.import_module(module_path), attr)
