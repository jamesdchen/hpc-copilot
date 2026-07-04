"""Scheduler write-fence — conduct rule 7 mechanized (curiosity ok, consequences gated)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from hpc_agent._kernel.hooks.scheduler_write_fence import _fenced_in_command

BLOCKED = [
    "qsub job.sh",
    "sbatch --array=1-10 job.slurm",
    "qdel 13910281",
    "scancel 12345",
    "ssh hoffman2 qdel 13910281",
    "ssh -i key jamesdc1@hoffman2.idre.ucla.edu 'qsub /tmp/job.sh'",
    "ssh host \"bash -lc 'cd repo && qsub job.sh'\"",
    "bash -lc 'qsub job.sh'",
    "hpc-agent status --run-id r1 && qdel 99",
    "VAR=1 timeout 30 sbatch job.slurm",
    "nohup qsub job.sh",
    "/u/systems/UGE8.6.4/bin/lx-amd64/qsub job.sh",
]

ALLOWED = [
    "qstat -u jamesdc1",
    "ssh hoffman2 qstat -u jamesdc1",
    "ssh hoffman2 'bash -lc \"qacct -j 13910281\"'",
    "squeue --me",
    "grep qsub cluster.log",
    "echo qdel",
    "hpc-agent submit-s2 --spec spec.json --experiment-dir .",
    "hpc-agent describe submit-flow",
    "hpc-agent wait-detached --spec spec.json",
    "git commit -m 'wire qsub path'",
    "cat docs/qsub-notes.md",
    "python -m pytest tests/ -q",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_blocks_mutating_scheduler_commands(cmd: str) -> None:
    assert _fenced_in_command(cmd) is not None, cmd


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allows_reads_and_innocent_mentions(cmd: str) -> None:
    assert _fenced_in_command(cmd) is None, cmd


def _run_hook(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hpc_agent._kernel.hooks.scheduler_write_fence"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_hook_exit_contract_blocks_with_reason() -> None:
    proc = _run_hook({"tool_input": {"command": "ssh hoffman2 qdel 42"}})
    assert proc.returncode == 2
    assert "scheduler-write-fence" in proc.stderr
    assert "qdel" in proc.stderr


def test_hook_exit_contract_allows_reads() -> None:
    proc = _run_hook({"tool_input": {"command": "ssh hoffman2 qstat -u me"}})
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_hook_never_wedges_on_malformed_payload() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent._kernel.hooks.scheduler_write_fence"],
        input="{not json",
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0
