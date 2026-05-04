"""Subprocess-invokes ``scripts/lint_primitive_modules.py``.

The CI lint catches the C′ "registered but invisible" failure mode —
a new module that decorates a primitive but is missing from
``_PRIMITIVE_MODULES``. Running it as a test as well ensures the gate
fires on every ``pytest -q`` invocation, not only in CI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_lint_primitive_modules_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "lint_primitive_modules.py")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"lint_primitive_modules failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
