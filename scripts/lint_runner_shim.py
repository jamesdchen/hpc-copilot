"""CI lint: ``hpc_agent.runner`` re-exports only ``@primitive``-decorated
symbols.

The package-root ``runner.py`` lives outside any subject and is therefore
permitted by ``lint_subject_imports`` as the cross-subject primitive
bridge — workflows in one subject can ``from hpc_agent.runner import X``
to call a primitive that lives in another subject. The bridge is honest
only if everything re-exported through it is itself a wire-callable
``@primitive``; if helpers / dataclasses / constants accumulate there
the shim becomes a generic cross-subject leak instead.

This lint walks the ``from hpc_agent.<subject>.<module> import …``
statements in ``runner.py`` and asserts every imported name is a
``@primitive``-decorated function in its source module. Constants and
dataclasses re-exported for back-compat with the legacy ``hpc_agent.runner``
package surface (e.g. ``DEFAULT_AUTO_RETRY_POLICY``) are allow-listed
explicitly with a one-line rationale each.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNNER_PY = REPO / "src" / "hpc_agent" / "runner.py"

# Back-compat re-exports that are NOT primitives but were on the legacy
# ``hpc_agent.runner`` package surface. Each entry must carry a rationale
# (why it can't move) so the next reader knows why the exception exists.
_BACK_COMPAT_NONPRIMITIVES: dict[str, str] = {
    # The retry-policy framework default and the two helper functions
    # that operate on it. Used by the failures atom + the resubmit flow;
    # all three live in ops/recover/runner_failures.py but are public-
    # surface enough that the legacy import path is preserved.
    "DEFAULT_AUTO_RETRY_POLICY": "ops.recover.runner_failures default policy dict",
    "annotate_clusters_with_retry_advice": "ops.recover.runner_failures helper",
    "cluster_failures_by_fingerprint": "ops.recover.runner_failures helper",
    "fingerprint_stderr_tail": "ops.recover.runner_failures helper",
    # Provenance / output verification helpers — published surface from
    # ops/aggregate/runner.py used by external aggregate harnesses.
    "build_provenance": "ops.aggregate.runner helper",
    "verify_combiner_artifact": "ops.aggregate.runner helper",
    "verify_per_task_outputs": "ops.aggregate.runner helper",
    "write_remote_provenance": "ops.aggregate.runner helper",
    # Per-task stderr tailer; used by recover/failures_atom AND by
    # external harnesses that diagnose failures.
    "fetch_task_logs": "infra.cluster_logs re-export wrapping the moved helper",
    # Resubmit-request id derivation — pure helper. Surface area is
    # public-ish; lives in ops/recover/runner.py.
    "derive_resubmit_request_id": "ops.recover.runner helper",
    # build_job_env: env-var augmentation helper from ops/submit/runner.py
    "build_job_env": "ops.submit.runner helper (env augmentation)",
}


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
        # Re-import the source module; query each imported name.
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
            is_primitive = getattr(obj, "_primitive_meta", None) is not None
            if is_primitive:
                continue
            if sym in _BACK_COMPAT_NONPRIMITIVES:
                continue
            violations.append(
                f"runner.py L{node.lineno}: {node.module}.{sym} is neither a "
                "@primitive nor a back-compat allow-list entry. "
                "If it's a primitive, decorate it. If it's a helper, "
                "either move it to infra/ (preferred) or add it to "
                "_BACK_COMPAT_NONPRIMITIVES with a rationale."
            )

    if violations:
        print("ERROR: hpc_agent.runner re-exports non-primitive symbols:")
        for v in violations:
            print(f"  {v}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
