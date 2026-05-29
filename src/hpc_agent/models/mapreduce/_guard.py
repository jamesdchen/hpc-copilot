"""Cluster-side import sanity guard (issue #159).

The reduce-phase reporter runs on a login/compute node as
``python -m hpc_agent.models.mapreduce.reduce.status``. That node's import
path can be polluted by a *wrong* ``hpc_agent`` — an ancient
``pip install --user`` under ``~/.local``, a leftover Py2.7 ``.pyc`` beside an
old deployed stub, or a partial/namespace shadow. When such a copy wins the
import the failure is opaque: ``ImportError: bad magic number`` (Py2 bytecode
under Py3), a namespace package with no ``__file__``, or — worst — a silently
wrong result from a stale version. This guard runs at reporter entry and fails
loud, naming the resolved path and ``sys.path`` so the operator knows exactly
which ``hpc_agent`` to remove.

Scope / limits: a guard living *inside* ``hpc_agent`` can only run once the
package imports, so it cannot pre-empt the hard ``bad magic number`` crash where
the package's own ``__init__`` is the bad ``.pyc``. That case is handled at the
source by the deploy-time ``.pyc`` purge (the #159 primary fix) and the
preamble's ``PYTHONDONTWRITEBYTECODE`` + PYTHONPATH hygiene. This guard is
defense-in-depth for the broader "a different ``hpc_agent`` imported
successfully but is the wrong one" class.
"""

from __future__ import annotations

import os
import sys

#: Set this env var to any non-empty value to skip the guard — an escape
#: hatch in case it ever false-positives on a legitimate live-cluster layout.
_DISABLE_ENV = "HPC_DISABLE_IMPORT_GUARD"


class ShadowedImportError(RuntimeError):
    """The imported ``hpc_agent`` is not the expected project install."""


def assert_canonical_import() -> None:
    """Fail loud if the imported ``hpc_agent`` looks shadowed or wrong.

    A no-op on a healthy install. Raises :class:`ShadowedImportError` (a
    clear, actionable message) when the imported package is Python 2, a
    namespace shadow with no ``__file__``, or a user-site (``~/.local``)
    install masking the project environment.
    """
    if os.environ.get(_DISABLE_ENV):
        return

    import hpc_agent

    pkg_file = getattr(hpc_agent, "__file__", None)
    if not pkg_file:
        # No __file__ => a namespace-package shadow: an empty/partial dir
        # earlier on sys.path is masquerading as the package (the #143 class).
        raise ShadowedImportError(
            "hpc_agent imported as a namespace package with no __file__ — a "
            "stale or partial copy is shadowing the real install.\n"
            f"  __path__  = {list(getattr(hpc_agent, '__path__', []))}\n"
            f"  sys.path = {sys.path}"
        )

    resolved = os.path.realpath(pkg_file)
    user_site = os.path.realpath(os.path.expanduser("~/.local"))
    if resolved == user_site or resolved.startswith(user_site + os.sep):
        raise ShadowedImportError(
            f"hpc_agent imported from a user-site install at {resolved} — this "
            "shadows the project's conda environment and is the #159 footgun.\n"
            "Remove it and re-run from the activated env:\n"
            "  pip uninstall -y hpc-agent   # then clear ~/.local/.../hpc_agent\n"
            f"  sys.path = {sys.path}"
        )
