"""End-to-end campaign tests with mocked submission/cluster.

These wire the asyncio loop (``run_campaign``) to fake submit/await
callbacks that simulate a cluster: each "submission" writes a sidecar
with the campaign tag and produces a metrics.json in the right
result_dir. ``await_completion`` resolves once that file exists.

This exercises the integration of every piece introduced in workstream
2 — sidecar v2 schema, history.prior(), find_runs_by_campaign,
run_campaign, the CLI inspection commands — without SSH or a real
scheduler.
"""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import sys
from typing import TYPE_CHECKING

from hpc_mapreduce.campaign import run_campaign
from hpc_mapreduce.job.runs import write_run_sidecar
from hpc_mapreduce.reduce.history import find_sidecars_by_campaign, prior

if TYPE_CHECKING:
    from pathlib import Path


def _common_required_kwargs(run_id: str, task_count: int = 1) -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        claude_hpc_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 stub.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=task_count,
        tasks_py_sha="1" * 64,
    )


def _seed_metrics(experiment_dir: Path, run_id: str, payload: dict) -> None:
    """Write a metrics.json into the result_dir for run_id/task_0."""
    rd = experiment_dir / "results" / run_id / "task_0"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "metrics.json").write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# E2E: random-search campaign
# ---------------------------------------------------------------------------


def test_e2e_random_search_converges_below_median(tmp_path: Path) -> None:
    """A 12-iteration random search over lr in [0, 0.1] minimizing
    (lr - 0.05)^2 must produce a best-so-far closer to 0.05 than the median."""
    campaign_id = "random_search_e2e"
    n_iter = 12
    rng = random.Random(0xC0FFEE)
    submitted: list[float] = []
    counter = {"n": 0}

    async def submit_one() -> str:
        counter["n"] += 1
        run_id = f"rs-{counter['n']:04d}"
        lr = rng.uniform(0.0, 0.1)
        submitted.append(lr)
        write_run_sidecar(
            tmp_path,
            **_common_required_kwargs(run_id),
            campaign_id=campaign_id,
        )
        loss = (lr - 0.05) ** 2
        _seed_metrics(tmp_path, run_id, {"loss": loss, "n_samples": 1})
        return run_id

    async def await_completion(_run_id: str) -> None:
        # The fake submission wrote metrics.json synchronously; nothing
        # to wait for.
        return

    def gate() -> bool:
        return counter["n"] < n_iter

    async def driver():
        return await run_campaign(
            concurrency=4,
            submit_one=submit_one,
            await_completion=await_completion,
            should_submit=gate,
        )

    result = asyncio.run(driver())
    assert result.iterations_completed == n_iter

    # Verify the campaign sees every iteration in history, oldest-first.
    history = prior(tmp_path, campaign_id)
    assert len(history) == n_iter
    losses = [h["loss"] for h in history]
    assert all(loss >= 0 for loss in losses)

    # Best-so-far should beat the median (the expected outcome of any
    # random sampler over a unimodal target with this many samples).
    best = min(losses)
    median = sorted(losses)[len(losses) // 2]
    assert best < median, f"best loss {best} not better than median {median}"


# ---------------------------------------------------------------------------
# E2E: walk-forward
# ---------------------------------------------------------------------------


def test_e2e_walk_forward_processes_windows_in_order_and_stops(
    tmp_path: Path,
) -> None:
    """Iteration N submits the Nth window; loop stops when len(_PRIOR) == 10."""
    campaign_id = "walk_forward_e2e"
    n_windows = 10
    counter = {"n": 0}
    submitted_windows: list[int] = []

    async def submit_one() -> str:
        run_id = f"wf-{counter['n']:04d}"
        # The user's tasks.py would index `windows[len(_PRIOR)]`; here
        # we use the same convention via prior().
        history = prior(tmp_path, campaign_id)
        next_idx = len(history)
        submitted_windows.append(next_idx)
        write_run_sidecar(
            tmp_path,
            **_common_required_kwargs(run_id),
            campaign_id=campaign_id,
        )
        _seed_metrics(tmp_path, run_id, {"window": next_idx, "n_samples": 1})
        counter["n"] += 1
        return run_id

    async def await_completion(_run_id: str) -> None:
        return

    def gate() -> bool:
        history = prior(tmp_path, campaign_id)
        return len(history) < n_windows

    async def driver():
        return await run_campaign(
            concurrency=1,  # streaming so prior() is consistent at each tick
            submit_one=submit_one,
            await_completion=await_completion,
            should_submit=gate,
        )

    result = asyncio.run(driver())
    assert result.iterations_completed == n_windows
    assert submitted_windows == list(range(n_windows)), f"windows out of order: {submitted_windows}"

    # After completion, gate() must return False again.
    assert not gate()


# ---------------------------------------------------------------------------
# E2E: resume from partial campaign
# ---------------------------------------------------------------------------


def test_e2e_resume_picks_up_where_loop_left_off(tmp_path: Path) -> None:
    """Plant 4 sidecars for a campaign, then run the loop with a 10-iter
    cap; the loop should add only 6 more (not duplicate the existing 4)
    because gate() reads len(prior())."""
    campaign_id = "resume_e2e"
    cap = 10

    # Plant 4 prior iterations on disk.
    for i in range(4):
        rid = f"existing-{i:04d}"
        write_run_sidecar(tmp_path, **_common_required_kwargs(rid), campaign_id=campaign_id)
        _seed_metrics(tmp_path, rid, {"loss": 0.5 - i * 0.05, "n_samples": 1})

    # Now run the loop. It must see the 4 existing in prior() and only
    # add 6 more.
    counter = {"n": 0}

    async def submit_one() -> str:
        rid = f"new-{counter['n']:04d}"
        write_run_sidecar(tmp_path, **_common_required_kwargs(rid), campaign_id=campaign_id)
        _seed_metrics(tmp_path, rid, {"loss": 0.1, "n_samples": 1})
        counter["n"] += 1
        return rid

    async def await_completion(_run_id: str) -> None:
        return

    def gate() -> bool:
        return len(prior(tmp_path, campaign_id)) < cap

    async def driver():
        return await run_campaign(
            concurrency=1,
            submit_one=submit_one,
            await_completion=await_completion,
            should_submit=gate,
        )

    result = asyncio.run(driver())
    assert result.iterations_completed == 6, (
        f"expected 6 new iterations to reach cap of {cap}; got {result.iterations_completed}"
    )

    # All 10 are now visible; no duplicates.
    sidecars = find_sidecars_by_campaign(tmp_path, campaign_id)
    assert len(sidecars) == cap
    assert len({s["run_id"] for s in sidecars}) == cap


# ---------------------------------------------------------------------------
# E2E: CLI smoke (init/run not implemented as separate subcommands; we
# verify the status + list path that closes the loop with the user)
# ---------------------------------------------------------------------------


def test_e2e_cli_status_after_random_search_returns_full_history(
    tmp_path: Path,
) -> None:
    """After a campaign run, `hpc-mapreduce campaign status` must report
    every iteration's reduced metrics in oldest-first order."""
    # Plant a tiny 3-iteration campaign on disk.
    campaign_id = "smoke"
    for i in range(3):
        rid = f"s-{i:04d}"
        write_run_sidecar(tmp_path, **_common_required_kwargs(rid), campaign_id=campaign_id)
        _seed_metrics(tmp_path, rid, {"loss": 0.5 / (i + 1), "n_samples": 1})

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hpc_mapreduce",
            "campaign",
            "status",
            "--experiment-dir",
            str(tmp_path),
            "--campaign-id",
            campaign_id,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    env = json.loads(proc.stdout.strip().splitlines()[-1])
    assert env["ok"] is True
    data = env["data"]
    assert data["campaign_id"] == campaign_id
    assert data["iterations"] == 3
    losses = [h["loss"] for h in data["history"]]
    assert losses == [0.5, 0.25, pytest_approx_inv(3, 0.5)]


def pytest_approx_inv(i: int, num: float) -> float:
    """0.5 / i — pulled out so the assert reads naturally above."""
    return num / i


def test_e2e_cli_list_after_two_campaigns_groups_correctly(tmp_path: Path) -> None:
    """`campaign list` must surface each campaign's iteration count."""
    for i in range(3):
        write_run_sidecar(
            tmp_path,
            **_common_required_kwargs(f"a-{i:04d}"),
            campaign_id="campaign_A",
        )
    for i in range(2):
        write_run_sidecar(
            tmp_path,
            **_common_required_kwargs(f"b-{i:04d}"),
            campaign_id="campaign_B",
        )
    # An untagged sidecar must NOT appear in the listing.
    write_run_sidecar(tmp_path, **_common_required_kwargs("c-0000"))

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hpc_mapreduce",
            "campaign",
            "list",
            "--experiment-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    env = json.loads(proc.stdout.strip().splitlines()[-1])
    counts = {c["campaign_id"]: c["iterations"] for c in env["data"]["campaigns"]}
    assert counts == {"campaign_A": 3, "campaign_B": 2}
