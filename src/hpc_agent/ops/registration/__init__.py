"""The registration kernel's ops layer — the read-only ``verify-registration``
consumer seat (``verify_op.py``) and the per-kind prerequisite-chain checker
dispatch (``prereqs.py``).

Design origin: ``docs/design/registration-kernel.md`` (Wave B). Core ships only
the MECHANISM: it never learns what is being registered, what a field slug means,
or what "ready to deploy" means in any domain (the Q1 boundary posture).
"""
