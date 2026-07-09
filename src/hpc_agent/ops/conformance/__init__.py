"""Live-conformance ops — the emitter's journaling + query surface (Wave B).

Design origin: ``docs/design/live-conformance.md``. A registration is a
hypothesis; production is the experiment that never stops. These leaf modules
ride the pure T1 kernel (``state/conformance.py``) and the T3 ledger
(``state/conformance_store.py``):

* ``record_op`` (T4) — the ``conformance-record`` mutate verb (the EMITTER's
  journaling surface; ``agent_facing=False``). Its ONLY side effect is one
  ledger append: it validates the registration exists (an absent registration
  is refused loudly — no hypothesis to test), reduces the registration's
  journal to stamp ``status_at_record`` (fail-open — a stale/revoked/superseded
  registration is RECORDED, disclosed, never refused), and binds the
  server-recomputed payload sha.

Naming warning (the plan's C-verbs section): the package word ``conformance``
is ALSO claimed by the HARNESS conformance kit (``src/hpc_agent/conformance/``,
``docs/design/conformance-kit.md``) — a DIFFERENT subject. THIS package is
REGISTRATION conformance (the SPC watchdog). The paths are disjoint and
importable side by side; the collision is cognitive.

Docstring-only by the subject-init lint: import symbols from the leaf modules
directly.
"""
