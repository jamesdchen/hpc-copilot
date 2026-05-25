"""Back-compat shim: ``hpc_agent.incorporation.template`` moved to
:mod:`hpc_agent.experiment_kit`.

The researcher-facing surface (``@register_run``, ``DataAxis``,
``plan_tasks``, ``load_series``, ``check_elision``, ``Monoid``, ...)
was lifted out from under the architectural ``incorporation/``
namespace into its own top-level package in the post-reorg cleanup —
researchers asked for it by name, and burying it inside the framework
scaffolding directory obscured the user-facing layer.

This module re-exports the new home so old imports keep working for
one release; new code should import from :mod:`hpc_agent.experiment_kit`.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "hpc_agent.incorporation.template has moved to hpc_agent.experiment_kit; "
    "update your imports.",
    DeprecationWarning,
    stacklevel=2,
)

from hpc_agent.experiment_kit import *  # noqa: F401, F403, E402
