"""F49 (bug-sweep #25, a never-landed ledger row): the SubmitFlowSpec wire model
must refuse the two MPI shapes that silently misrun on the direct submit-flow
surface — SGE + mpi with no ``pe_name`` (N ranks oversubscribing one slot), and
an mpi block with ``total_tasks > 1`` (the array path runs the mpi template as
task 0 in every element, clobbering output). The equivalent guards exist on
``BuildSubmitSpecInput`` but the public ``submit_flow.input.json`` entry reached
``_engine.py`` — whose comment ASSERTS the wire guard makes ``pe_name`` present —
with no check. These fire-path tests hold the two resurrected guards.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _spec(**overrides):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    base = dict(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/r",
        job_name="j",
        run_id="rX",
        total_tasks=1,
        backend="sge",
        script="run.sh",
        job_env={"EXECUTOR": "python run.py"},
        result_dir_template="results/{run_id}/task_{task_id}",
    )
    base.update(overrides)
    return SubmitFlowSpec(**base)


def _mpi(**overrides):
    from hpc_agent._wire.workflows.submit_flow import MpiSpec, SubmitResources

    kw = dict(ranks=16, launcher="mpirun")
    kw.update(overrides)
    return SubmitResources(mpi=MpiSpec(**kw))


def test_sge_mpi_without_pe_name_is_refused() -> None:
    """FIRE: backend='sge' + mpi block + no pe_name → refused at the wire, naming
    pe_name (SGE routes ranks through a parallel environment; without it qsub
    emits no slot request and N ranks land on one core)."""
    with pytest.raises(ValidationError, match="pe_name"):
        _spec(resources=_mpi())


def test_sge_mpi_with_pe_name_is_accepted() -> None:
    """BOUNDARY: a pe_name present is the resolved happy path — accepted."""
    spec = _spec(resources=_mpi(pe_name="mpi"))
    assert spec.resources.mpi.pe_name == "mpi"


def test_slurm_mpi_without_pe_name_is_accepted() -> None:
    """BOUNDARY: only SGE needs pe_name (SLURM derives layout from --ntasks), so a
    SLURM mpi spec with no pe_name must NOT be refused."""
    spec = _spec(backend="slurm", resources=_mpi())
    assert spec.resources.mpi.pe_name is None


def test_mpi_with_total_tasks_gt_one_is_refused() -> None:
    """FIRE: an mpi block with total_tasks > 1 would take the array path and run
    the mpi template as task 0 in every element (clobbered output) — refused,
    naming total_tasks=1."""
    with pytest.raises(ValidationError, match="total_tasks=1"):
        _spec(total_tasks=4, resources=_mpi(pe_name="mpi"))


def test_mpi_with_total_tasks_one_is_accepted() -> None:
    """BOUNDARY: a single multi-rank unit (total_tasks=1) is the legitimate MPI
    shape — accepted."""
    spec = _spec(total_tasks=1, resources=_mpi(pe_name="mpi"))
    assert spec.total_tasks == 1


def test_non_mpi_spec_is_unaffected() -> None:
    """BOUNDARY: an ordinary array (no mpi block) with total_tasks>1 on SGE is
    unaffected by either guard."""
    spec = _spec(backend="sge", total_tasks=100)
    assert spec.resources is None
