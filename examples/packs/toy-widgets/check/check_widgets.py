"""Toy-widgets pack CHECK script — runs caller-side, emits a pack receipt.

The F10 first-consumer illustration: a domain check runs ENTIRELY outside core
(DP2 — core never imports or executes a pack file), then records its mechanical
verdict as a sha-bound CODE receipt via ``hpc-agent pack-record-receipt``. The
pack's own CI (or the experiment env) runs this; core only ever weighs the
resulting receipt, which reads stale the instant any checked byte drifts.

Toy-domain vocabulary only — ``widgets``/``widget-audit`` are made-up; a real
pack would name its real check here, and that domain word would live in the
pack's files, never in core.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PACK = "toy-widgets"
_SLOT = "widget-audit"
_TEMPLATE_REL = "packs/toy-widgets/templates/widget_audit.py"


def check_widgets(template: Path) -> bool:
    """A toy domain check: the widget audit template must carry an rmse section.

    Stands in for a real domain assertion (a stats check, a holdout audit). The
    verdict is a mechanical boolean the receipt records — core never re-derives it.
    """
    text = template.read_text(encoding="utf-8")
    return "hpc-audit-section: compute-rmse" in text


def main() -> int:
    experiment_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    passed = check_widgets(experiment_dir / _TEMPLATE_REL)
    spec = {
        "pack": _PACK,
        "slot": _SLOT,
        "checked": [_TEMPLATE_REL],
        "passed": passed,
        "evidence": {"checker": "check_widgets", "rmse_section_present": passed},
    }
    return subprocess.call(  # noqa: S603 — fixed argv, illustrative caller-side call
        [
            "hpc-agent",
            "pack-record-receipt",
            "--experiment-dir",
            str(experiment_dir),
            "--spec",
            json.dumps(spec),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
