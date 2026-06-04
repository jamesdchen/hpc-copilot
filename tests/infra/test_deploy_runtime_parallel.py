"""deploy_runtime fires its scp copies concurrently (#245).

The copies are independent and all target the same host (reusing one ssh
ControlMaster), so they run in a bounded thread pool rather than serially.
We patch the scp subprocess to sleep a fixed slice and assert the wall-clock
reflects ``ceil(N / pool) × slice``, not ``N × slice``.
"""

from __future__ import annotations

import math
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hpc_agent.infra import transport

_SLICE = 0.2  # seconds each fake scp sleeps


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")


def _sleepy_scp(*_args, **_kwargs):
    time.sleep(_SLICE)
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_scps_run_concurrently_not_serially():
    # Disable the cache so all files deploy (and no manifest read/write skews
    # the count): a single-family deploy ships 8 files. With a pool of 4 and a
    # 0.2s slice, wall-clock ≈ ceil(8/4) × 0.2 = 0.4s, far under the 1.6s a
    # serial loop would take.
    n_files = len(transport._build_deploy_items(scheduler="sge"))
    assert n_files == 8  # 2 stubs + dispatch + combiner + 2 templates + 2 preambles

    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ),
        patch("hpc_agent.infra.transport.subprocess.run", side_effect=_sleepy_scp),
    ):
        start = time.monotonic()
        transport.deploy_runtime(
            ssh_target="u@c", remote_path="/p", scheduler="sge", use_cache=False
        )
        elapsed = time.monotonic() - start

    serial = n_files * _SLICE
    waves = math.ceil(n_files / transport._DEPLOY_PARALLELISM)
    parallel_floor = waves * _SLICE
    # Comfortably below serial; a generous ceiling absorbs scheduling jitter
    # while still failing if the copies secretly run one-at-a-time.
    assert elapsed < serial * 0.75, f"{elapsed:.3f}s ~ serial {serial:.3f}s (not parallel)"
    assert elapsed >= parallel_floor * 0.5


def test_first_scp_failure_propagates_out_of_the_pool():
    # A failing copy must surface as the deploy's exception, exactly as the
    # old serial path raised the first _with_ssh_backoff failure.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("scp exploded")

    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ),
        patch("hpc_agent.infra.transport.subprocess.run", side_effect=_boom),
        pytest.raises(RuntimeError, match="scp exploded"),
    ):
        transport.deploy_runtime(
            ssh_target="u@c", remote_path="/p", scheduler="sge", use_cache=False
        )
