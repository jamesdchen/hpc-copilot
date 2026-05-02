"""Default callbacks for :func:`run_campaign` that wrap the existing CLI.

Every closed-loop driver needs three async/sync callables: ``submit_one``
(launch one iteration), ``await_completion`` (wait for one to finish),
and ``should_submit`` (decide whether to launch another). Most users
write the same boilerplate — re-import ``tasks.py`` for the predicate,
shell out to ``hpc-mapreduce status`` for the poll, build a spec dict
and shell out to ``hpc-mapreduce submit`` for the submit.

These defaults are strategy-blind. They say nothing about Optuna, random
search, or any specific tuning algorithm. Users who need custom logic
(e.g. SSH'ing the actual qsub themselves rather than going through the
CLI) write their own callables; the defaults are the convenient path
for the common case.

Pair them with :func:`run_campaign`::

    from hpc_mapreduce.campaign import run_campaign
    from hpc_mapreduce.campaign.defaults import (
        poll_until_terminal,
        submit_via_cli,
        tasks_py_total_predicate,
    )

    def build_spec() -> dict:
        return {"profile": "ml_ridge", ...}  # whatever your submit needs

    result = await run_campaign(
        concurrency=4,
        submit_one=submit_via_cli(build_spec),
        await_completion=poll_until_terminal("."),
        should_submit=tasks_py_total_predicate("."),
    )
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_mapreduce import load_tasks_module, tasks_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "poll_until_terminal",
    "submit_via_cli",
    "tasks_py_total_predicate",
]


_TERMINAL_STATES = frozenset({"complete", "failed", "abandoned"})


def tasks_py_total_predicate(
    experiment_dir: Path | str = ".",
) -> Callable[[], bool]:
    """Return a ``should_submit`` predicate that re-imports ``.hpc/tasks.py``
    each call and returns ``tasks.total() > 0``.

    Re-importing per call is intentional: the user's ``tasks.py`` reads
    :func:`hpc_mapreduce.reduce.history.prior` at module load to count
    completed iterations; only a fresh import sees newly-landed sidecars.

    Parameters
    ----------
    experiment_dir:
        Path to the experiment repo (defaults to CWD).

    Returns
    -------
    Callable returning ``bool``. Suitable as the ``should_submit`` arg
    of :func:`run_campaign`.
    """
    exp_dir = Path(experiment_dir)

    def _predicate() -> bool:
        mod = load_tasks_module(tasks_path(exp_dir))
        return int(mod.total()) > 0

    return _predicate


def poll_until_terminal(
    experiment_dir: Path | str = ".",
    *,
    poll_interval_seconds: float = 30.0,
) -> Callable[[str], Awaitable[None]]:
    """Return an ``await_completion`` callable that polls ``hpc-mapreduce
    status --run-id <id>`` every *poll_interval_seconds* until the run
    reaches a terminal lifecycle state.

    Terminal states: ``complete``, ``failed``, ``abandoned`` (matches
    ``slash_commands.session.TERMINAL_STATUSES``).

    The poll uses ``asyncio.to_thread`` to wrap the blocking subprocess
    so multiple in-flight iterations can poll concurrently without
    blocking the event loop. Each poll consumes one SSH-via-CLI call;
    set *poll_interval_seconds* high enough that a campaign with K
    in-flight iterations doesn't exceed your scheduler's query rate
    cap (rule of thumb: K * 1/poll_interval_seconds ≤ 1 query/sec).

    On ``hpc-mapreduce status`` exit codes other than 0, the
    coroutine raises ``RuntimeError`` with stderr in the message —
    surfaced via ``run_campaign``'s ``on_event`` as the iteration's
    ``error`` field, the loop continues.
    """
    exp_dir = Path(experiment_dir)

    def _poll_once_blocking(run_id: str) -> str:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hpc_mapreduce",
                "status",
                "--experiment-dir",
                str(exp_dir),
                "--run-id",
                run_id,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"hpc-mapreduce status --run-id {run_id} exited "
                f"{proc.returncode}: {proc.stderr.strip()[:200]}"
            )
        line = proc.stdout.strip().splitlines()[-1]
        envelope = json.loads(line)
        if not envelope.get("ok"):
            raise RuntimeError(
                f"status returned error envelope for {run_id}: "
                f"{envelope.get('error_code')}: {envelope.get('message')}"
            )
        state: str = envelope["data"]["lifecycle_state"]
        return state

    async def _await(run_id: str) -> None:
        while True:
            state = await asyncio.to_thread(_poll_once_blocking, run_id)
            if state in _TERMINAL_STATES:
                return
            await asyncio.sleep(poll_interval_seconds)

    return _await


def submit_via_cli(
    spec_builder: Callable[[], dict],
    *,
    experiment_dir: Path | str = ".",
) -> Callable[[], Awaitable[str]]:
    """Return a ``submit_one`` callable that:

    1. Calls *spec_builder* to get a fresh submission-spec dict (the
       caller is responsible for whatever per-iteration construction
       their setup requires — fresh ``run_id``, current strategy
       params, etc.).
    2. Writes the spec to ``.hpc/campaigns/<campaign_id>/spec-<run_id>.json``
       (when ``campaign_id`` is in the spec) or to a tempfile.
    3. Shells out to ``hpc-mapreduce submit --spec <path>``.
    4. Returns the spec's ``run_id`` so the loop can track this iteration.

    The spec must include at least ``run_id`` and the fields required
    by ``schemas/submit.input.json``. Errors from
    ``hpc-mapreduce submit`` propagate as ``RuntimeError``.

    Parameters
    ----------
    spec_builder:
        Zero-arg callable returning a complete spec dict. Called once
        per ``submit_one`` invocation; should be cheap and side-effect
        free except for any per-iteration state the caller wants
        (e.g. re-importing ``tasks.py`` so a strategy library proposes
        the next params).
    experiment_dir:
        Path forwarded to ``--experiment-dir``.
    """
    exp_dir = Path(experiment_dir)

    def _submit_blocking(spec: dict) -> None:
        cid = spec.get("campaign_id")
        if cid:
            from hpc_mapreduce.campaign import campaign_dir

            spec_dir = campaign_dir(exp_dir, cid)
            spec_path = spec_dir / f"spec-{spec['run_id']}.json"
        else:
            (exp_dir / ".hpc").mkdir(parents=True, exist_ok=True)
            spec_path = exp_dir / ".hpc" / f"spec-{spec['run_id']}.json"
        spec_path.write_text(json.dumps(spec))

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hpc_mapreduce",
                "submit",
                "--experiment-dir",
                str(exp_dir),
                "--spec",
                str(spec_path),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"hpc-mapreduce submit exited {proc.returncode}: {proc.stderr.strip()[:200]}"
            )

    async def _submit_one() -> str:
        spec = spec_builder()
        if "run_id" not in spec:
            raise ValueError("submit_via_cli: spec_builder() must return a dict with 'run_id'")
        await asyncio.to_thread(_submit_blocking, spec)
        return str(spec["run_id"])

    return _submit_one
