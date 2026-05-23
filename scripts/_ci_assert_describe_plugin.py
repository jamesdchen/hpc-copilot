#!/usr/bin/env python3
"""CI helper: assert ``hpc-agent describe <plugin-primitive>`` resolved.

Reads the envelope JSON written by the previous CI step and fails if it
isn't a successful ``primitive`` envelope pointing at a ``hpc_agent_pro.``
implementation. Lives as a standalone script (not inlined into ci.yml)
to keep the workflow's quoting hygienic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(path: str) -> int:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not payload.get("ok"):
        print(f"FAIL: describe envelope ok=false: {payload}")
        return 1
    data = payload.get("data") or {}
    if data.get("kind") != "primitive":
        print(f"FAIL: describe data.kind != 'primitive': {data}")
        return 1
    python_path = (data.get("content") or {}).get("python") or ""
    if not python_path.startswith("hpc_agent_pro."):
        print(f"FAIL: describe resolved a plugin primitive to a non-plugin module: {python_path!r}")
        return 1
    print("OK: describe resolves a plugin-owned primitive to its plugin module.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
