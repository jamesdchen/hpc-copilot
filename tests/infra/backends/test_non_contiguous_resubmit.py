"""Non-contiguous resubmit array ranges, family-aware (#6).

recover-flow's ``compact_task_ids`` packs the exact failed ids into a
comma-bearing expression (``"4,8,13-15"``). SLURM ``--array`` and TORQUE
``-t`` accept a comma LIST verbatim; SGE/UGE ``qsub -t`` and PBS Pro
``qsub -J`` accept only a SINGLE ``n[-m[:s]]`` range. Two layers under test:

* the ``_build_*_command`` builders raise a loud, diagnosable ``SpecInvalid``
  when a comma-bearing range reaches a single-range family (instead of
  emitting an invalid qsub the scheduler rejects opaquely); and
* :meth:`ProfileBackend.submit_non_contiguous` splits the expression into one
  array job per contiguous run for single-range families (accumulating every
  job id) while comma-capable families submit in one shot.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends import get_backend
from hpc_agent.infra.backends.sge import SGEBackend
from hpc_agent.infra.backends.slurm import SlurmBackend


def _noop_ssh(_cmd):
    return SimpleNamespace(stdout="", stderr="", returncode=0)


def _pbs_backend(family, **over):
    kw = dict(script="cpu.pbs", ssh_run=_noop_ssh, remote_repo="/r", pass_env_keys=("K",))
    kw.update(over)
    return get_backend(family, **kw)


# ---------------------------------------------------------------------------
# Builder backstop: single-range families reject a comma list loudly (#6)
# ---------------------------------------------------------------------------


def test_sge_builder_rejects_comma_range(tmp_path):
    backend = SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))
    with pytest.raises(errors.SpecInvalid, match="single 'n\\[-m\\[:s\\]\\]' range"):
        backend._build_command("4,8,13-15", "job", {})


def test_pbspro_builder_rejects_comma_range():
    backend = _pbs_backend("pbspro")
    with pytest.raises(errors.SpecInvalid, match="single 'X-Y\\[:Z\\]' range"):
        backend._build_command("4,8,13-15", "job", {"K": "V"})


def test_sge_builder_allows_single_contiguous_range(tmp_path):
    # A single contiguous run (no comma) is unaffected by the backstop.
    backend = SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))
    cmd = backend._build_command("13-15", "job", {})
    assert cmd[cmd.index("-t") + 1] == "13-15"


def test_torque_builder_allows_comma_range():
    # TORQUE ``-t`` accepts comma lists — no backstop, command emits verbatim.
    backend = _pbs_backend("torque")
    cmd = backend._build_command("4,8,13-15", "job", {"K": "V"})
    assert cmd[cmd.index("-t") + 1] == "4,8,13-15"


def test_slurm_builder_allows_comma_range(tmp_path):
    # SLURM ``--array`` accepts comma lists — no backstop.
    backend = SlurmBackend(script=str(tmp_path / "j.slurm"), log_dir=str(tmp_path / "logs"))
    cmd = backend._build_command("4,8,13-15", "job", {})
    assert cmd[cmd.index("--array") + 1] == "4,8,13-15"


# ---------------------------------------------------------------------------
# submit_non_contiguous: split single-range families, one-shot comma families
# ---------------------------------------------------------------------------


def _recording(backend, stdout_for):
    """Stub the SSH edge on *backend*: record each built command and return a
    scheduler-shaped stdout via *stdout_for(cmd)* so the real JOB_ID_REGEX
    parses a distinct id per submission. Keeps the real ``_build_command`` so
    the comma backstop is genuinely exercised.
    """
    commands: list[list[str]] = []

    def _exec(cmd, job_env, cwd):
        commands.append(list(cmd))
        return SimpleNamespace(stdout=stdout_for(cmd), stderr="", returncode=0)

    backend._execute_command = _exec  # type: ignore[method-assign]
    backend._setup_log_dir = lambda: None  # type: ignore[method-assign]
    return commands


def test_sge_splits_comma_range_into_one_array_per_run(tmp_path):
    backend = SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))

    # Return "Your job <N>" where N encodes the -t range so ids are distinct.
    def _stdout(cmd):
        rng = cmd[cmd.index("-t") + 1]
        n = {"4": 100, "8": 200, "13-15": 300}[rng]
        return f"Your job {n} ('job') has been submitted\n"

    commands = _recording(backend, _stdout)
    ids = backend.submit_non_contiguous("4,8,13-15", "job", {})

    # Every contiguous run produced its own job id — none dropped.
    assert ids == ["100", "200", "300"]
    ranges = [c[c.index("-t") + 1] for c in commands]
    assert ranges == ["4", "8", "13-15"]


def test_pbspro_splits_comma_range_into_one_array_per_run():
    backend = _pbs_backend("pbspro")

    def _stdout(cmd):
        rng = cmd[cmd.index("-J") + 1]
        n = {"4": 100, "8": 200, "13-15": 300}[rng]
        return f"{n}.pbsserver\n"

    commands = _recording(backend, _stdout)
    ids = backend.submit_non_contiguous("4,8,13-15", "job", {"K": "V"})

    assert ids == ["100", "200", "300"]
    ranges = [c[c.index("-J") + 1] for c in commands]
    assert ranges == ["4", "8", "13-15"]


def test_slurm_comma_range_submits_once(tmp_path):
    # A comma-capable family keeps the single-submission contract: one array,
    # one job id, comma list intact.
    backend = SlurmBackend(script=str(tmp_path / "j.slurm"), log_dir=str(tmp_path / "logs"))
    commands = _recording(backend, lambda cmd: "Submitted batch job 777\n")
    ids = backend.submit_non_contiguous("4,8,13-15", "job", {})
    assert ids == ["777"]
    assert len(commands) == 1
    assert commands[0][commands[0].index("--array") + 1] == "4,8,13-15"


def test_torque_comma_range_submits_once():
    backend = _pbs_backend("torque")
    commands = _recording(backend, lambda cmd: "888.pbsserver\n")
    ids = backend.submit_non_contiguous("4,8,13-15", "job", {"K": "V"})
    assert ids == ["888"]
    assert len(commands) == 1
    assert commands[0][commands[0].index("-t") + 1] == "4,8,13-15"


def test_sge_single_contiguous_range_submits_once(tmp_path):
    # No comma → one submission even for a single-range family.
    backend = SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))
    commands = _recording(backend, lambda cmd: "Your job 555 ('job') has been submitted\n")
    ids = backend.submit_non_contiguous("13-15", "job", {})
    assert ids == ["555"]
    assert len(commands) == 1
    assert commands[0][commands[0].index("-t") + 1] == "13-15"
