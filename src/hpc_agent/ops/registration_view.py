"""Top-level facade re-exporting the registration subject's gate ingredients.

Mirrors ``ops/notebook_view.py`` / ``ops/field_ownership.py``: a subject file
(``ops/decision/journal.py``'s registration authorship gate, T7) reaches the
``ops/registration`` subject's per-kind chain checker (:func:`check_chain`) and
its deterministic brief renderer (:func:`build_view` — R6's fourth recompute
leg) through this TOP-LEVEL facade. A direct
``from hpc_agent.ops.registration.prereqs import ...`` from inside the
``decision`` subject trips the subject-import lint
(``scripts/lint_subject_imports.py``); this file lives directly under ``ops/``
(a role root, NOT a subject directory), so the lint never scans it and it may
import any subject freely.

One source of truth: this BINDS, never copies. The prerequisite currency logic
lives in exactly one place (``ops/registration/prereqs.py::check_chain``) and
the view_sha projection in exactly one place
(``ops/registration/verify_op.py::build_view``); the T7 gate recomputes over
the SAME renderer the T5 reporter uses, so a witness the gate recomputes is
byte-identical to the one the reporter renders (a witness you can regenerate is
regenerated, never trusted).
"""

from __future__ import annotations

from hpc_agent.ops.registration.prereqs import SlotVerdict, check_chain
from hpc_agent.ops.registration.verify_op import build_view

__all__ = ["SlotVerdict", "check_chain", "build_view"]
