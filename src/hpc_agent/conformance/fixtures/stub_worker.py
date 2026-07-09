"""The K6 stub detached worker — a minimal detached-work stand-in.

``docs/design/conformance-kit.md`` (capability 3): the kit supplies a stub
worker the adapter's ``start_background`` / ``await_wake`` drives. It sleeps
briefly, then writes a terminal JSON record at the path the driver handed it —
the durable **journal-namespace rendezvous** a woken driver reads back to
confirm the worker reached a terminal state.

PURE STDLIB by design: no scheduler, no SSH, no network, and NO ``hpc_agent``
import. The driver (the kit module) resolves the rendezvous path through the ONE
canonical journal resolver in-process and hands the worker the fully-resolved
target, so the worker itself stays a bare detached process — exactly the shape a
conforming ``start_background`` must be able to launch.

Invoked as a script::

    python stub_worker.py <terminal_json_path> [sleep_seconds]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    target = Path(argv[1])
    sleep_s = float(argv[2]) if len(argv) > 2 else 0.05
    time.sleep(sleep_s)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"state": "complete", "terminal": True}, sort_keys=True),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
