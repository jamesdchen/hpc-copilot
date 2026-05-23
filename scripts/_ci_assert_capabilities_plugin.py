#!/usr/bin/env python3
"""CI helper: assert plugin operations appear in ``capabilities``.

Reads the envelope JSON written by the previous CI step and fails if the
plugin's headline operations are absent from the catalog the agent reads.
Lives as a standalone script (not inlined into ci.yml) to keep the
workflow's quoting hygienic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Subset of plugin-owned operations that must appear in ``capabilities``
# when ``hpc-agent-pro`` is installed alongside ``hpc-agent``. Kept small
# so the assertion stays meaningful as the plugin's surface evolves.
REQUIRED = (
    "predict-start-time",
    "best-submit-window",
    "walltime-drift",
)


def main(path: str) -> int:
    payload = json.loads(Path(path).read_text())
    data = payload.get("data") or {}
    ops = data.get("operations") or []
    names = {o.get("name") for o in ops}
    missing = [n for n in REQUIRED if n not in names]
    if missing:
        print(
            f"FAIL: plugin operations missing from capabilities catalog: {missing}\n"
            f"Catalog has {len(names)} operations."
        )
        return 1
    print(f"OK: {len(names)} operations in catalog, plugin ops present.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
