"""Pack ops — the bind / receipt / status surface of the domain-pack substrate.

Design origin: ``docs/design/domain-packs.md`` (Wave B). Three leaf modules ride
the T1/T2 state substrate (``state/pack.py``, ``state/pack_receipts.py``):

* ``bind_op`` (T4) — the ``pack-bind`` mutate verb (the bind event).
* ``record_receipt_op`` (T5) — the ``pack-record-receipt`` mutate verb
  (server-side recompute; the parse IS the recompute).
* ``status_op`` (T6) — the ``pack-status`` read-only query.

Docstring-only by the subject-init lint: import symbols from the leaf modules
directly.
"""
