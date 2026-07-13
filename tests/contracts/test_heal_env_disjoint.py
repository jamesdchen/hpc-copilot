"""The ∩=∅ contract (overnight-repair.md §9 RULING 2026-07-12 enforcement row).

The env-drift ruling: the enumerated transport-only vars are NOT spec identity —
they are client-side transport selection, PROVABLY never threaded into the job's
environment — so drift on them is the C1 env-pin heal. EVERY other env var that can
reach the executor's environment on the cluster IS spec identity (drift kills the
consent). This contract pins ``healable-set ∩ job-env-threaded-set = ∅`` so a
refactor cannot silently move a healable transport var into the job env.

The job-env-threaded set is derived MECHANICALLY (not hand-listed): from the
``.hpc/``-rooted members of ``transport._build_deploy_items`` — the job-side RUNTIME
the framework ships (the rendered scheduler templates, the shared preambles, the
dispatcher, the combiner). Those are the surfaces that set/consume the job env. The
``hpc_agent/``-rooted deploy members are imported LIBRARY stubs (``errors.py``,
``time.py``, the reporter closure), not job-env threading surfaces, so they are
excluded by the same mechanical ``.hpc/`` filter. If a refactor adds a healable
transport var to any job-side runtime file, its token appears in the derived set and
this test FIRES.
"""

from __future__ import annotations

import re

from hpc_agent.infra.env_flags import HEALABLE_TRANSPORT_ENV_VARS
from hpc_agent.infra.transport import _build_deploy_items

_HPC_TOKEN = re.compile(r"HPC_[A-Z0-9_]+")


def _job_env_threaded_set() -> set[str]:
    """Mechanically derive the HPC_* vars the framework threads into the job env.

    Scans every ``.hpc/``-rooted deploy item's content for ``HPC_*`` tokens — the
    job-side runtime (templates, preambles, dispatcher, combiner). Never a hand list.
    """
    tokens: set[str] = set()
    for item in _build_deploy_items(scheduler=None):
        if not item.dst_rel.startswith(".hpc/"):
            continue  # a ``hpc_agent/`` library stub is imported, not job-env threading
        if item.content is not None:
            text = item.content
        elif item.src_path is not None:
            text = item.src_path.read_text(encoding="utf-8")
        else:  # pragma: no cover — a deploy item always carries content or a src_path
            continue
        tokens.update(_HPC_TOKEN.findall(text))
    return tokens


def test_healable_transport_vars_never_threaded_into_job_env() -> None:
    """The healable transport set is DISJOINT from the mechanically-derived job-env set."""
    job_env = _job_env_threaded_set()
    # Sanity: the derivation actually found the known job-env vars (the test would be
    # vacuous if the scan returned nothing — HPC_RUN_ID / HPC_TASK_ID are always there).
    assert "HPC_RUN_ID" in job_env
    assert "HPC_TASK_ID" in job_env

    overlap = HEALABLE_TRANSPORT_ENV_VARS & job_env
    assert overlap == set(), (
        "a healable transport var is threaded into the job env — the ruling's "
        f"∩=∅ contract is broken: {sorted(overlap)}. A transport-selection var must "
        "never reach the executor's environment on the cluster (it is client-side "
        "only). Either it stopped being transport-only (remove it from "
        "HEALABLE_TRANSPORT_ENV_VARS and treat its drift as spec-identity) or a "
        "refactor wrongly added it to a job-side runtime file."
    )


def test_derivation_is_nonempty_and_mechanical() -> None:
    """The job-env set is derived from the deploy SoT and is non-trivial."""
    job_env = _job_env_threaded_set()
    # The framework threads a healthy handful of job-env vars; pin a floor so a
    # refactor that empties the scan (making the ∩=∅ test vacuous) is caught.
    assert len(job_env) >= 5
    # Every healable var is genuinely absent (the ruling's evidence basis).
    for var in HEALABLE_TRANSPORT_ENV_VARS:
        assert var not in job_env
