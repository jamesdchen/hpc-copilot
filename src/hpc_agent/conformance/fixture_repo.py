"""The fixture-repo builder — a temp experiment dir with a CLAIMED journal
namespace, honoring ``HPC_JOURNAL_DIR`` (D-K1 / K1).

Every capability assertion needs a repo the reader
(``state/utterances.py::read_utterances``, the authorship gate) treats as an
hpc-agent experiment: a directory whose journal namespace
(``<journal home>/<repo_hash>/``) ALREADY EXISTS. The write API is no-scaffold
by contract — ``append_utterance`` is a clean no-op against an unclaimed repo —
so a fixture repo must claim the namespace first.

The idiom is the one every test uses (``tests/_kernel/hooks/test_utterance_capture.py``
``_scaffold_namespace`` + ``tests/conftest.py``'s ``HPC_JOURNAL_DIR`` redirect):
point ``HPC_JOURNAL_DIR`` at an isolated home, then call
``state/run_record.py::journal_dir`` — the sole real claim
(``mkdir`` + ``repo.json`` + ``runs/``) that ``_current_homedir`` resolves
through the env var. Stdlib-only (pytest-free); the ``fixture_repo`` pytest
fixture in ``conftest.py`` wraps this.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["claim_fixture_repo", "journal_home"]


def journal_home() -> Path:
    """The journal home the kit will write under — honors ``HPC_JOURNAL_DIR``.

    Delegates to the ONE canonical resolver
    (``state/run_record.py::_current_homedir``): ``HPC_JOURNAL_DIR`` when
    set-and-non-empty, else the ``HPC_HOMEDIR`` attribute, else
    ``~/.claude/hpc``. Never re-derives the lookup.
    """
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir()


def claim_fixture_repo(experiment_dir: Path) -> Path:
    """Make *experiment_dir* an hpc-agent repo and return it.

    Creates the directory if needed, then claims its journal namespace under
    the resolved :func:`journal_home` exactly as a real state write does — so a
    subsequent ``append_utterance`` / ``read_utterances`` round-trip lands in
    the namespace the reader looks up. The namespace is keyed by
    ``repo_hash(experiment_dir)``, so two distinct dirs are ISOLATED (distinct
    ``<repo_hash>/`` namespaces) even under one ``HPC_JOURNAL_DIR``.
    """
    from hpc_agent.state.run_record import journal_dir

    experiment_dir.mkdir(parents=True, exist_ok=True)
    journal_dir(experiment_dir)  # the real claim: mkdir + repo.json + runs/
    return experiment_dir


def _honor_or_set_journal_dir(default_home: Path) -> None:
    """Point ``HPC_JOURNAL_DIR`` at *default_home* UNLESS already set.

    Honors an inherited ``HPC_JOURNAL_DIR`` (a caller's explicit choice wins);
    otherwise pins an isolated home so the kit never writes the developer's
    real ``~/.claude/hpc``. Used by the ``fixture_repo`` conftest fixture.
    """
    existing = os.environ.get("HPC_JOURNAL_DIR")
    if not existing:
        os.environ["HPC_JOURNAL_DIR"] = str(default_home)
