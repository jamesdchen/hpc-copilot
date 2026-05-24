"""CI lint: ``hpc_agent.runner`` re-exports only ``@primitive``-decorated
symbols.

The package-root ``runner.py`` lives outside any subject and is therefore
permitted by ``lint_subject_imports`` as the cross-subject primitive
bridge — atoms in one subject can ``from hpc_agent.runner import X`` to
call a primitive that lives in another subject. The bridge is honest
only if everything re-exported through it is itself a wire-callable
``@primitive``; if helpers / dataclasses / constants accumulate there
the shim becomes a generic cross-subject leak instead.

This lint walks the ``from hpc_agent.<subject>.<module> import …``
statements in ``runner.py`` and asserts every imported name is a
``@primitive``-decorated function in its source module.

History: the previous incarnation of this lint maintained a
``_BACK_COMPAT_NONPRIMITIVES`` allow-list for legacy helpers re-exported
through ``runner.py``. P1 + the post-P4 cleanup migrated every helper
caller to canonical-home imports; the allow-list was retired in the
same change. New cross-subject primitive surfaces should add themselves
to ``runner.py`` by writing the ``@primitive`` decorator on the function
in its canonical home — never as bare helpers.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNNER_PY = REPO / "src" / "hpc_agent" / "runner.py"


def main() -> int:
    sys.path.insert(0, str(REPO / "src"))
    src = RUNNER_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("hpc_agent."):
            continue
        try:
            mod = importlib.import_module(node.module)
        except ImportError as exc:
            violations.append(f"runner.py L{node.lineno}: cannot import {node.module}: {exc}")
            continue
        for alias in node.names:
            sym = alias.name
            obj = getattr(mod, sym, None)
            if obj is None:
                violations.append(f"runner.py L{node.lineno}: {node.module}.{sym} resolves to None")
                continue
            if getattr(obj, "_primitive_meta", None) is not None:
                continue
            violations.append(
                f"runner.py L{node.lineno}: {node.module}.{sym} is not "
                "an @primitive. Either decorate it as a primitive in its "
                "canonical home, or move it to infra/ if it's a shared "
                "helper. Bare back-compat re-exports are no longer permitted."
            )

    if violations:
        print("ERROR: hpc_agent.runner re-exports non-primitive symbols:")
        for v in violations:
            print(f"  {v}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
