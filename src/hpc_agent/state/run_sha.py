"""Pure SHA computations over the user's ``tasks.py`` and materialized tasks.

Extracted from :mod:`hpc_agent.state.runs` so the run-sidecar lifecycle
module can stay focused on path helpers, sidecar I/O, and lifecycle
(find / prune / update). The two functions here are pure: given a
loaded ``tasks_module`` (or a path), they hash and return.

Callers import these directly from this module (see the pointer comment
in :mod:`hpc_agent.state.runs`).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

__all__ = ["RESERVED_TASK_KEYS", "compose_node_sha", "compute_cmd_sha", "compute_tasks_py_sha"]

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


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def compose_node_sha(cmd_sha: str, parent_node_shas: list[str]) -> str:
    """Compose a run's parameter identity with its parents' identities.

    The recursive-identity invariant for inter-run dependency (the
    ``docs/design/dag-kernel.md`` prototype): when run B consumes run
    A's outputs, B's dedup identity must change whenever A's does —
    otherwise a re-submit of B dedups against a result computed from a
    *different* A, silently. ``compose_node_sha`` is the Merkle step that
    makes identity compose::

        node_sha = H(canonical({"node": cmd_sha, "parents": sorted(set(parents))}))

    Properties (pinned in ``tests/state/test_node_sha_properties.py``):

    * **Zero parents degenerates to ``cmd_sha`` verbatim.** Every existing
      run is a 0-parent node whose identity is unchanged — today's
      dedup/journal keys need no migration.
    * **Parents are a set.** Order-invariant, duplicate-insensitive: an
      edge declares "depends on", not a sequence.
    * **Ancestor changes propagate.** A changed grandparent changes the
      parent's node_sha and therefore this node's — stale-subgraph reuse
      cannot pass a node_sha equality check.

    Like :func:`compute_cmd_sha`, this is parameter identity, not code
    identity (#207): the parent digests fold in the parents' *params*,
    never their executor bytes. Wired in via
    :func:`hpc_agent.state.runs.resolve_node_sha` (submit-side derivation
    from parents' sidecars) and the ``node_sha`` lever on
    :func:`hpc_agent.state.runs.find_run_by_cmd_sha` (effective-identity
    dedup); a submit that declares no ``parents`` never reaches the
    composed branch.

    Raises :class:`ValueError` if *cmd_sha* or any parent is not a 64-char
    lowercase hex digest — these strings are produced by this module's own
    hash functions, so a malformed one is a caller bug worth failing loud on.
    """
    if not _SHA256_HEX.match(cmd_sha):
        raise ValueError(f"compose_node_sha: cmd_sha is not a sha256 hex digest: {cmd_sha!r}")
    parents = sorted(set(parent_node_shas))
    for p in parents:
        if not _SHA256_HEX.match(p):
            raise ValueError(f"compose_node_sha: parent node_sha is not a sha256 hex digest: {p!r}")
    if not parents:
        return cmd_sha
    envelope = json.dumps(
        {"node": cmd_sha, "parents": parents}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(envelope.encode()).hexdigest()
