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
    "nohup qdel 13910281",
    "/u/systems/UGE8.6.4/bin/lx-amd64/qsub job.sh",
    # Flagged wrappers and a leading subshell paren must not hide the verb
    # (audit 2026-07-09: all four passed unfenced while `nohup qdel` blocked).
    "nice -n 10 sbatch job.sh",
    "stdbuf -oL sbatch job.sh",
    "timeout -k 5 60 qdel 123",
    "(qdel 123)",
    # Finding #24: exec/command wrappers, eval/xargs indirection, command
    # substitution, and a leading redirection all EXECUTE the fenced verb.
    "exec qsub job.sh",
    "exec sbatch job.sh",
    "command sbatch job.sh",
    "command qsub job.sh",
    'eval "qsub job.sh"',
    "eval 'sbatch job.slurm'",
    "xargs qsub < list",
    "xargs sbatch",
    "echo $(qsub job.sh)",
    "latest=$(sbatch job.slurm)",
    ">log qsub job.sh",
    ">out sbatch job.sh",
    "2>err qsub job.sh",
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
    # Flagged wrappers / subshells around READ-ONLY commands stay allowed.
    "nice -n 10 python train.py",
    "timeout -k 5 60 qstat -u me",
    "stdbuf -oL squeue --me",
    "(grep qsub cluster.log)",
    # Finding #24: the new unwrap paths must not over-block benign prose /
    # read-only indirection that merely MENTIONS a fenced verb.
    'echo "run sbatch later"',
    "echo $(grep qsub cluster.log)",
    "exec python train.py",
    'eval "qstat -u me"',
    ">out echo done",
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


# ── run-#10 quote-aware regression (F-D) ─────────────────────────────────────
# The live false positive: a read-only grep whose QUOTED pattern carried a
# fenced verb and a `|` was regex-split mid-quote, failed shlex, and hit the
# fail-closed fallback. The primary path is now a punctuation_chars lexer, so
# quoted operators stay inside their token.


def test_quoted_alternation_pattern_is_not_fenced() -> None:
    # The exact run-#10 shape: alternation inside a quoted grep pattern.
    assert _fenced_in_command('grep "qsub|sbatch" worker.log') is None


def test_quoted_fenced_word_with_semicolon_is_not_fenced() -> None:
    assert _fenced_in_command("python -c \"print(1); x = 'qsub'\"") is None


def test_embedded_operator_still_blocks() -> None:
    # Unquoted `qsub&&rm` executes qsub — the lexer emits && as its own token.
    assert _fenced_in_command("qsub&&rm x") == "qsub"


def test_multiline_second_line_blocks() -> None:
    assert _fenced_in_command("grep 'qsub' a\nqsub b.sh") == "qsub"


def test_unparseable_line_keeps_fail_closed_fallback() -> None:
    # A genuinely unbalanced quote with a fenced word still fails closed.
    assert _fenced_in_command('ssh host "qdel 123') == "qdel"
