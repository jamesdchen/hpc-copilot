"""Import a user's executor module with the experiment dir on ``sys.path``.

Single owner of one invariant: **local executor-module import replicates
the cluster's ``$REPO_DIR``-on-``PYTHONPATH`` context.**

On the cluster, ``hpc_preamble.sh`` prepends ``$REPO_DIR`` to
``PYTHONPATH`` before launching a task, so ``import executors.X`` (a
PEP 420 namespace package — no ``__init__.py`` required) resolves. During
LOCAL intake/validation the ``hpc-agent`` console-script's ``sys.path``
does NOT include the experiment dir, so the same import false-fails with
``ModuleNotFoundError`` even though the module is perfectly importable on
the cluster (#178). Both local entry points that import a user's executor
module — the ``/submit-hpc`` interview
(:func:`hpc_agent.ops.memory.interview._validate_python_module_entry`) and
the signature validator
(:func:`hpc_agent.ops.validate.executor_signatures.validate_executor_signatures`)
— route through this helper so the path is mirrored in exactly one place.

Lives under ``infra`` (not ``ops``) because it is shared substrate: ops
subjects compose through ``hpc_agent.infra.*`` rather than importing each
other (see ``scripts/lint_subject_imports.py``).
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from pathlib import Path
from types import ModuleType

__all__ = ["import_executor_module"]


def import_executor_module(module_name: str, repo_dir: Path) -> ModuleType:
    """Import ``module_name`` with ``repo_dir`` prepended to ``sys.path``.

    Replicates the cluster's ``$REPO_DIR``-on-``PYTHONPATH`` context: the
    resolved *repo_dir* is prepended to ``sys.path`` for the duration of
    the import (and removed afterwards if we added it, so the long-lived
    framework process's global path is not permanently mutated). ``import
    executors.X`` and ``import src.ml_ridge`` then resolve locally exactly
    as they do on the cluster.

    GENUINE failures still propagate unchanged — a truly-absent module
    raises ``ModuleNotFoundError`` and a real error inside the module
    raises ``ImportError`` (or whatever the module's import-time code
    raised). Callers map those to their own intake/validation findings;
    this helper only fixes the *path*, it never swallows a fault.
    """
    repo_str = str(Path(repo_dir).resolve())
    inserted = repo_str not in sys.path
    if inserted:
        sys.path.insert(0, repo_str)
    try:
        # A previous attempt (before repo_dir was on the path) can leave a
        # negative result in the finder caches; clear them so the
        # freshly-inserted entry is actually consulted on this import.
        importlib.invalidate_caches()
        return importlib.import_module(module_name)
    finally:
        if inserted:
            # We added it, so it is present — suppress defensively in case a
            # nested import already removed it.
            with contextlib.suppress(ValueError):
                sys.path.remove(repo_str)
