"""Registration ops — the deployment-boundary machinery (registration-kernel Wave B).

Design origin: ``docs/design/registration-kernel.md``. Two leaf modules ride the
T1 kernel substrate (``state/registration.py``):

* ``prereqs`` (T4) — the per-kind prerequisite-chain checker dispatch (R3 table);
  ``check_chain`` is PURE DISPATCH, each kind routing through its ONE existing
  definition.
* ``verify_op`` (T5) — the read-only ``verify-registration`` query verb.

Docstring-only by the subject-init lint: import symbols from the leaf modules
directly.
"""
