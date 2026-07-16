"""``migrate-remainder`` — move a run's UNDONE tasks to another cluster (SPEC).

The read-mostly, gated recovery verb that mechanizes "move the remaining tasks to
another cluster" as ONE `y`-gated step. This package holds the mechanism, split by
SPEC wave/unit:

- :mod:`census` (M-CENSUS) — the authoritative done-set census: the per-task
  announced-id listing, the status-reporter cross-check, and the wave/axis-aligned
  remainder partition.
- :mod:`derive` (M-DERIVE) — mint the derived enumerated run over the undone cells.
- :mod:`ownership` (M-DERIVE) — the cell-ownership map for the two-parent harvest.

Each module actuates nothing (no submit): they compute the census, plan, ownership
map, and derived spec+files in seconds, and the human `y` gates everything
downstream (the scope doctrine — observe/judge/route, never actuate).
"""

from __future__ import annotations
