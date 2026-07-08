"""Registration ops — the deployment-boundary prerequisite machinery (Wave B).

Design origin: ``docs/design/registration-kernel.md``. This package holds the
OPS-layer registration surface that rides the T1 kernel substrate
(``state/registration.py``):

* :mod:`~hpc_agent.ops.registration.prereqs` (T4) — the per-kind prerequisite
  chain checker dispatch (R3 table). :func:`~hpc_agent.ops.registration.prereqs.check_chain`
  is PURE DISPATCH: it never re-implements any member's currency logic; each
  kind routes through its ONE existing definition.

``verify_op`` (T5, the ``verify-registration`` query verb) lands beside it.
"""

from __future__ import annotations

from hpc_agent.ops.registration.prereqs import SlotVerdict, check_chain

__all__ = ["SlotVerdict", "check_chain"]
