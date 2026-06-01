"""Re-baseline the gold snapshots of the behavioral-eval corpus.

Two equivalent entry points, mirroring lara's ``--regen-results`` flag:

* ``HPC_EVAL_REGEN=1 pytest -q tests/eval`` — the test itself snapshots each
  case (and still passes) when that env flag is set. Convenient: regen + run
  in one command.
* ``python -m tests.eval.regen [<case_id> ...]`` — this script. Regens all
  cases, or only the named ones. Useful in isolation (CI dry-runs, a single
  case after editing its ``expect`` block).

Both write ``tests/eval/gold/<id>.yaml`` via :func:`tests.eval._gold.write_gold`.
The snapshot is the OFFLINE resolution (``resolve_offline``) — deterministic,
no API key, no network — so regen is reproducible for anyone and a gold diff
in review pinpoints a changed decision.

Run this whenever a deliberate change to the deterministic resolution path (a
planner heuristic, a resource default, a new/edited case) makes the committed
gold stale. NEVER run it to "make a failing test pass" without reading the
diff: the gold is the regression tripwire, and a surprising diff is the suite
doing its job.
"""

from __future__ import annotations

import sys

from tests.eval._gold import write_gold
from tests.eval.cases import CASES, case_by_id
from tests.eval.resolve import resolve_offline


def regen(case_ids: list[str] | None = None) -> int:
    """Snapshot the offline resolution of each (named) case; return the count.

    *case_ids* ``None``/empty → every case in :data:`tests.eval.cases.CASES`.
    """
    cases = CASES if not case_ids else [case_by_id(cid) for cid in case_ids]
    for case in cases:
        resolved = resolve_offline(case)
        write_gold(case.gold_path, resolved)
        print(f"regen {case.id} -> {case.gold_path.relative_to(case.gold_path.parents[2])}")
    return len(cases)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    n = regen(args or None)
    print(f"regenerated {n} gold snapshot(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
