"""Read/write the version-controlled gold snapshots of resolved specs.

The corpus has two layers of expectation, on purpose:

* The ``EvalCase.expect`` block (in ``cases/__init__.py``) is the
  HAND-AUTHORED, intent-bearing contract — "this request must resolve to
  hoffman2 with grid_points=6". It is the thing a reviewer reads, and the
  thing ``recursive_compare`` grades against. It states only the fields that
  matter and is tolerant where it should be.

* The gold YAML under ``tests/eval/gold/<id>.yaml`` is a MACHINE SNAPSHOT of
  the *full* resolved spec the offline resolver currently produces. It is the
  regression tripwire: ``--regen`` (``HPC_EVAL_REGEN=1``) rewrites it, and the
  default test asserts the live resolution still equals the committed snapshot
  EXACTLY. So an unintended change to the deterministic resolution (a planner
  tweak, a default flip) shows up as a gold diff in review even if it stays
  inside the hand-authored ``expect`` tolerances.

Why YAML: it diffs cleanly in review and matches the repo's config idiom
(``clusters.yaml`` / ``axes.yaml``). Keys are sorted on write so the snapshot
is byte-stable across regens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def read_gold(path: Path) -> dict[str, Any] | None:
    """Return the snapshot at *path*, or ``None`` if it has not been regen'd."""
    if not path.is_file():
        return None
    loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def write_gold(path: Path, resolved: dict[str, Any]) -> None:
    """Snapshot *resolved* to *path* as sorted YAML (creates the dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# AUTO-GENERATED gold snapshot of the offline-resolved submit spec.\n"
        "# Regenerate with:  HPC_EVAL_REGEN=1 pytest -q tests/eval\n"
        "# (or:  python -m tests.eval.regen [<case_id> ...])\n"
        "# Do not hand-edit — edit the EvalCase.expect block instead and regen.\n"
    )
    body = yaml.safe_dump(resolved, sort_keys=True, default_flow_style=False, allow_unicode=True)
    path.write_text(header + body, encoding="utf-8")
