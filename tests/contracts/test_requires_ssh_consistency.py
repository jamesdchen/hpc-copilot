"""Contract: every SSH-touching primitive declares ``cli.requires_ssh``.

The CLI dispatcher gates the ``SSH_AUTH_SOCK`` precondition based on
``CliShape.requires_ssh``. A primitive whose body invokes
:func:`hpc_agent.infra.remote.ssh_run` / ``rsync_push`` / ``rsync_pull``
(directly OR via the per-subject runner modules) but whose CLI
declaration omits ``requires_ssh=True`` will silently bypass the gate
and fail late with an opaque ``ssh: connect to host`` error instead of
the surface's ``SshUnreachable`` envelope.

The audit found this bypass for ``verify-canary``, ``cluster-reduce``,
``aggregate-flow``, and ``monitor-flow`` — each declares an
``SideEffect("ssh", ...)`` on its ``@primitive`` decorator yet leaves
``requires_ssh=False``. PR A flips the flags; this test pins the
invariant so the drift class cannot recur.

The detection runs in two passes:

1. Inspect the primitive's *declared* ``side_effects`` for any kind in
   ``{"ssh", "rsync_pull", "rsync_push", "sync-pull", "sync-push"}`` —
   the decorator is the unambiguous signal when present.
2. Substring-scan the primitive's source file for any of the canonical
   SSH calls (``ssh_run(``, ``rsync_pull(``, ``rsync_push(``,
   ``_ssh_``). The grep catches primitives whose decorator forgot to
   declare the side effect AT ALL (worse than a wrong ``requires_ssh``;
   the catalog row is also wrong).

Either signal is sufficient. Both signals failing on the same primitive
is the only state we accept as ``cli.requires_ssh=False``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from hpc_agent._kernel.registry.primitive import PrimitiveMeta
from tests._registry_helpers import core_only_registry

# Genuine exceptions go here with a comment explaining why the gate
# does NOT apply. The default policy is "everything that touches SSH
# must declare requires_ssh"; entries here document the rare case
# where the primitive's SSH usage is conditional and the gate would be
# user-hostile (e.g. a dry-run path that doesn't actually ssh).
_ALLOWLIST: set[str] = set()

# Side-effect kinds that imply the dispatcher must gate SSH_AUTH_SOCK
# before invoking the primitive. Matches the labels used across
# :mod:`hpc_agent.ops.*` (recover, submit, monitor, aggregate).
_SSH_SIDE_EFFECT_KINDS: frozenset[str] = frozenset(
    {"ssh", "rsync_pull", "rsync_push", "sync-pull", "sync-push"},
)

# Substring needles checked against each primitive's source file. The
# grep is intentionally loose — a false positive (a primitive that
# *mentions* ``ssh_run`` in a docstring) is far cheaper than a false
# negative (a primitive that calls ``ssh_run`` and silently bypasses
# the gate). Comments in docstrings are responsible for staying honest.
# Note: ``_ssh_`` substring would collide with ``validate_ssh_target``
# (a pure URL-shape validator — does not actually ssh). We anchor on
# the call form so that pattern doesn't matter; the private ssh-helper
# names that DO ssh are listed individually.
_SSH_SOURCE_NEEDLES: tuple[str, ...] = (
    "ssh_run(",
    "rsync_pull(",
    "rsync_push(",
    "_ssh_alive_",
    "_ssh_status_",
    "_ssh_list_",
)


@pytest.fixture(scope="module")
def registry() -> dict[str, PrimitiveMeta]:
    # Filter to core-only: the requires_ssh gate this test pins is a
    # core-CLI concern. Plugins implement their own CLI registration and
    # are responsible for their own SSH-gate consistency.
    return core_only_registry()


def _declares_ssh_side_effect(meta: PrimitiveMeta) -> bool:
    """True iff any declared :class:`SideEffect.kind` matches the SSH set."""
    return any(se.kind in _SSH_SIDE_EFFECT_KINDS for se in meta.side_effects)


def _source_uses_ssh(meta: PrimitiveMeta) -> bool:
    """True iff the primitive's source file mentions a canonical SSH call.

    Reads the module's source file once (no imports beyond what the
    registry already loaded). Returns False on read errors — a missing
    file is a worse problem the lint suite catches elsewhere; this
    test only owns the requires_ssh consistency check.
    """
    module = inspect.getmodule(meta.func)
    if module is None or not getattr(module, "__file__", None):
        return False
    try:
        src = Path(module.__file__).read_text(encoding="utf-8")
    except OSError:
        return False
    return any(needle in src for needle in _SSH_SOURCE_NEEDLES)


def test_ssh_touching_primitives_declare_requires_ssh(
    registry: dict[str, PrimitiveMeta],
) -> None:
    """Every primitive that touches SSH must gate the dispatcher."""
    offenders: list[tuple[str, str]] = []
    for name, meta in registry.items():
        if name in _ALLOWLIST:
            continue
        declared = _declares_ssh_side_effect(meta)
        sourced = _source_uses_ssh(meta)
        if not (declared or sourced):
            continue
        if meta.cli is None:
            # No CLI surface — dispatcher never reaches this primitive
            # through the requires_ssh gate. Composite-internal helpers
            # called from another primitive's body are fine; the parent
            # primitive's gate covers them.
            continue
        if not meta.cli.requires_ssh:
            reason = "declared SSH side_effect" if declared else "source uses ssh_run/rsync_*"
            offenders.append((name, reason))

    assert not offenders, (
        "Primitives touching SSH must declare cli=CliShape(..., requires_ssh=True) "
        "so the dispatcher gates SSH_AUTH_SOCK before invoking them. Offenders:\n  "
        + "\n  ".join(f"{n} ({why})" for n, why in offenders)
    )
