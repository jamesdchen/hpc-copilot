"""Shared fake for the fused server-side ``fetch_task_logs`` probe.

``infra.cluster_logs.fetch_task_logs`` folded its old F×J per-candidate SSH
fan-out into ONE ``ssh_run`` carrying a POSIX-``sh`` script that walks every
task's newest-first candidate list and emits a nonce-sentinel-framed section
per task (latency-elimination F5). :func:`fused_remote_fs` is the test-side
remote filesystem: it parses that single script, evaluates each task's
``[ -f <path> ]`` arms against a ``path -> content`` map, and returns the
framed stdout the real :func:`~hpc_agent.infra.ssh_validation.split_ack` +
section parser consume — the fused analogue of the old ``_fake_remote_fs``.

:func:`severed_remote_fs` is the truncation variant: it frames only the first
*intact* tasks and then clips the stream (no successor marker, no closing ack)
so the severed-frame honesty path fires (files after the cut read SEVERED, not
"no log").
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

_FakeSSH = Callable[..., subprocess.CompletedProcess[str]]


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _parse_script(cmd: str) -> tuple[str, list[tuple[int, list[tuple[str, str]]]]]:
    """Return ``(nonce, [(tid, [(path, job_id), ...]), ...])`` from *cmd*.

    Mirrors the real remote's view: each section printf names a ``tid``; the
    body line right after it is the ``if [ -f P ]; then ... elif ... fi`` arm
    chain, from which the ordered candidate ``(path, job_id)`` pairs are read.
    """
    nonce_m = re.search(r"__HPC_LOGSEC_([0-9a-f]+)", cmd)
    assert nonce_m, f"no fused-log section marker in script:\n{cmd}"
    nonce = nonce_m.group(1)
    sec = f"__HPC_LOGSEC_{nonce}"
    hit = f"__HPC_LOGHIT_{nonce}"
    lines = cmd.split("\n")
    tasks: list[tuple[int, list[tuple[str, str]]]] = []
    for idx, line in enumerate(lines):
        sm = re.search(rf"'{re.escape(sec)} (\d+)'", line)
        if not sm or "printf" not in line:
            continue
        tid = int(sm.group(1))
        body = lines[idx + 1] if idx + 1 < len(lines) else ""
        # Each arm: ``[ -f <path> ]; then printf '<hit> %s\n' <job>; tail ...``
        pairs = re.findall(
            rf"\[ -f (\S+) \]; then printf '{re.escape(hit)} %s\\n' (\S+); tail",
            body,
        )
        tasks.append((tid, [(p, j) for p, j in pairs]))
    return nonce, tasks


def fused_remote_fs(files: dict[str, str]) -> tuple[_FakeSSH, list[str]]:
    """A fake ``remote.ssh_run`` answering the fused probe from *files*.

    *files* maps a remote path to the bytes ``tail`` would emit for it. Records
    every probed path (in evaluation order) on the returned ``probed`` list so a
    test can assert the cross-task-collision guard skipped the wrong wave's job.
    """
    probed: list[str] = []

    def fake(cmd: str, *, ssh_target: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        nonce, tasks = _parse_script(cmd)
        sec = f"__HPC_LOGSEC_{nonce}"
        hit = f"__HPC_LOGHIT_{nonce}"
        miss = f"__HPC_LOGMISS_{nonce}"
        ack = "__HPC_LOGTAIL_ACK__="
        out: list[str] = []
        for tid, cands in tasks:
            out.append(f"\n{sec} {tid}\n")
            chosen: tuple[str, str] | None = None
            for path, job in cands:
                probed.append(path)
                if path in files:
                    chosen = (path, job)
                    break
            if chosen is not None:
                out.append(f"{hit} {chosen[1]}\n")
                out.append(files[chosen[0]])
            else:
                out.append(f"{miss}\n")
        out.append("\n")
        out.append(f"{ack}0\n")
        return _completed(stdout="".join(out))

    return fake, probed


def severed_remote_fs(files: dict[str, str], *, intact_tasks: int) -> tuple[_FakeSSH, list[str]]:
    """A fake that frames the first *intact_tasks* sections then CLIPS the stream.

    Models an NAT idle-drop / reaped channel that severs the read part-way,
    cutting mid-way through the section that follows the last intact one: the
    first *intact_tasks* sections are fully framed AND the next task's section
    marker is emitted (positively evidencing the intact ones as complete), then
    the stream is truncated — that task's content and the closing ack never
    arrive. So the real parser trusts the first *intact_tasks* tasks and reports
    every task from the cut onward SEVERED (``ssh_error``), never "no log".
    """
    probed: list[str] = []

    def fake(cmd: str, *, ssh_target: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        nonce, tasks = _parse_script(cmd)
        sec = f"__HPC_LOGSEC_{nonce}"
        hit = f"__HPC_LOGHIT_{nonce}"
        miss = f"__HPC_LOGMISS_{nonce}"
        out: list[str] = []
        for tid, cands in tasks[:intact_tasks]:
            out.append(f"\n{sec} {tid}\n")
            chosen: tuple[str, str] | None = None
            for path, job in cands:
                probed.append(path)
                if path in files:
                    chosen = (path, job)
                    break
            if chosen is not None:
                out.append(f"{hit} {chosen[1]}\n")
                out.append(files[chosen[0]])
            else:
                out.append(f"{miss}\n")
        # The next task's marker IS emitted (so the intact sections are proven
        # complete), then the channel is severed HERE: no verdict for it, no
        # markers for the tasks after it, no closing ack.
        if intact_tasks < len(tasks):
            dangling_tid = tasks[intact_tasks][0]
            out.append(f"\n{sec} {dangling_tid}\n")
        return _completed(stdout="".join(out))

    return fake, probed
