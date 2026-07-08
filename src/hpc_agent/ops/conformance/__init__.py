"""Registration-conformance operation verbs (live-conformance, Wave B).

The ``ops`` seat of ``docs/design/live-conformance.md`` — the SPC watchdog
rebuilt on attestations. A registration is a hypothesis; production is the
experiment that never stops. The two verbs (C-verbs):

* ``conformance-record`` (``record_op.py``, T4) — the ``agent_facing=False``
  mutate verb: it binds and appends ONE ledger observation. The emitter is
  caller machinery, never the driving agent (the receipt-laundering boundary).
* ``conformance-status`` (``status_op.py``, T5) — the read-only ``query`` verb:
  it loads the ledger + the registration + the sealed baseline, calls the ONE
  comparator (``state/conformance.py::judge_window``), and renders a
  deterministic dual-labelled brief. Verdicts are DERIVED on every read — no
  verdict store, nothing marked seen.

The agency boundary is first-class: this subject OBSERVES, JUDGES, and ROUTES —
it never actuates. No verb here reaches a broker, an instrument, or an external
system, and a ``nonconforming`` verdict changes NO registration status. Drift
routes ATTENTION, never action.
"""

from __future__ import annotations
