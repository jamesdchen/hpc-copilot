"""Pure SHA computations over the user's ``tasks.py`` and materialized tasks.

Extracted from :mod:`hpc_agent.state.runs` so the run-sidecar lifecycle
module can stay focused on path helpers, sidecar I/O, and lifecycle
(find / prune / update). The two functions here are pure: given a
loaded ``tasks_module`` (or a path), they hash and return.

Re-exported from :mod:`hpc_agent.state.runs` for backwards compatibility
with existing callers (``from hpc_agent.state.runs import compute_cmd_sha``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = ["RESERVED_TASK_KEYS", "compute_cmd_sha", "compute_tasks_py_sha"]

# Per-task keys that ``resolve(i)`` may return but that are EXCLUDED from
# the cmd_sha hash. These carry strategy bookkeeping — an opaque
# ``trial_token`` a closed-loop optimizer round-trips through ``resolve()``
# to reconcile a result back to the proposal that produced it — rather than
# a swept experiment parameter. Folding them into cmd_sha would change the
# experiment's parameter identity and bust dedup (the #207 boundary: cmd_sha
# is parameter identity, not bookkeeping), so they are stripped before
# hashing. Dedup across deliberately-repeated campaign iterations is handled
# separately by the campaign-iteration rejection in
# :func:`hpc_agent.state.runs.find_run_by_cmd_sha`.
RESERVED_TASK_KEYS: frozenset[str] = frozenset({"trial_token"})


def compute_cmd_sha(tasks_module: Any) -> str:
    """Materialize the task list and return a deterministic SHA-256.

    Imports the user's ``tasks.py`` module (already loaded by the caller),
    calls ``total()``, then ``resolve(i)`` for every ``i`` in
    ``range(total())``. Each kwargs dict is normalized to sorted-keys JSON
    and the lines are joined with ``\\n`` before hashing. The resulting
    digest is stable across equivalent task lists and changes whenever any
    kwarg dict changes.

    Returns a 64-char hex string.

    Semantics — ``cmd_sha`` IS THE PARAMETER IDENTITY OF THE EXPERIMENT,
    NOT ITS CODE IDENTITY (#207). The hashed material is exclusively the
    materialized per-task kwargs (``[resolve(i) for i in range(total())]``);
    it deliberately does NOT fold in the executor's source, the rendered
    job command, or ``tasks.py``'s bytes. The design rationale: the swept
    parameters DEFINE the experiment, while the executor body is treated as
    provenance, captured separately as ``tasks_py_sha`` on the run sidecar
    (see :func:`compute_tasks_py_sha`). Two consequences follow directly:

    * Editing an executor's body (a bug fix, a refactor, a changed model
      hyperparameter that is NOT one of the swept ``resolve`` kwargs) while
      leaving every materialized kwargs dict unchanged produces the SAME
      ``cmd_sha``. A re-submit therefore dedups against the prior run and
      runs the OLD code forward — by design, the params say "same
      experiment".
    * To make that code edit force a fresh run, the caller must opt in:
      ``find_run_by_cmd_sha(..., tasks_py_sha=<current>,
      invalidate_on_code_change=True)`` folds the recorded ``tasks_py_sha``
      into the dedup decision for that one submit. The default path is
      unchanged — params alone still key the dedup, and a code edit with
      unchanged params still dedups (with only a drift warning, see
      :func:`find_run_by_cmd_sha`).

    Raises
    ------
    AttributeError
        If *tasks_module* lacks ``total`` or ``resolve``.
    TypeError
        If ``resolve(i)`` does not return a dict.
    """
    n = int(tasks_module.total())
    parts: list[str] = []
    for i in range(n):
        kwargs = tasks_module.resolve(i)
        if not isinstance(kwargs, dict):
            raise TypeError(f"tasks.resolve({i}) must return a dict, got {type(kwargs).__name__}")
        # Strip reserved bookkeeping keys (e.g. ``trial_token``) so they do
        # not change parameter identity / bust dedup. A copy is hashed; the
        # caller's dict is left untouched (the dispatcher still exports the
        # reserved keys as ``HPC_KW_*`` for the executor to read).
        hashable = {k: v for k, v in kwargs.items() if k not in RESERVED_TASK_KEYS}
        parts.append(json.dumps(hashable, sort_keys=True, separators=(",", ":")))
    joined = "\n".join(parts).encode()
    return hashlib.sha256(joined).hexdigest()


def compute_tasks_py_sha(tasks_py_path: Path) -> str:
    """Return SHA-256 of ``tasks.py``'s bytes — diagnostic only."""
    return hashlib.sha256(Path(tasks_py_path).read_bytes()).hexdigest()
