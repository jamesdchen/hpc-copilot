"""B7 fire-path: the #249 canary-skip TTL cache is minted only after the FULL
gate passes — never mid-gate on the first canary's success.

The double canary is default-ON, so ``submit_and_verify`` fires a canary PAIR.
The old mint sat inside ``verify_canary``'s per-canary success path, so the FIRST
canary's success stamped the cache before the SECOND canary was verified. A
failed second canary then blocked the main array ONCE, but the cache entry
stood — and a retry inside the 4h TTL cache-skipped BOTH canaries on a ``cmd_sha``
that never fully validated. The mint now lives in ``submit_and_verify`` past both
verdicts (``_record_canary_gate_validated``).

These drive ``submit_and_verify`` with the transport/scheduler seams mocked
exactly as ``tests/ops/test_double_canary.py`` does, and assert the cache state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

if TYPE_CHECKING:
    from pathlib import Path

_SAV = "hpc_agent.ops.submit_and_verify"
_MAIN = "ml_run_cafef00d"
_CMD_SHA = "sha_gate_249"
_CLUSTER = "hoffman2"


def _submit_spec() -> SubmitFlowSpec:
    return SubmitFlowSpec(
        profile="ml",
        cluster=_CLUSTER,
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml",
        run_id=_MAIN,
        total_tasks=4,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        # HPC_CMD_SHA is the identity the readers (_gated_canary_cache_decision /
        # submit_blocks._assert_canary_verified) key on — the mint must match it.
        job_env={"HPC_CMD_SHA": _CMD_SHA, "K": "v"},
        canary=True,
    )


def _spec() -> SubmitAndVerifySpec:
    return SubmitAndVerifySpec(submit=_submit_spec(), poll_interval_sec=1, wait_budget_sec=5)


def _submit_env() -> object:
    from hpc_agent.ops.submit_flow import SubmitFlowResult

    return SubmitFlowResult(
        run_id=_MAIN,
        job_ids=["12345"],
        total_tasks=4,
        deduped=False,
        canary_done=True,
        canary_run_id=f"{_MAIN}-canary",
        canary_job_ids=["12344"],
    )


def _verify_env(*, ok: bool, failure_kind: str | None = None) -> dict:
    return {
        "ok": ok,
        "failure_kind": failure_kind,
        "details": "happy" if ok else "boom",
        "stderr_tail": "" if ok else "RuntimeError\n",
        "metrics_fingerprint": None,
    }


def _is_cached() -> bool:
    from hpc_agent import __version__ as pkg_version
    from hpc_agent.state import canary_cache

    return canary_cache.is_canary_validated_fresh(
        canary_cache.canary_cache_key(cmd_sha=_CMD_SHA, version=pkg_version or "", cluster=_CLUSTER)
    )


def test_both_canaries_green_mints_the_cache(tmp_path: Path) -> None:
    """The whole gate passed → the #249 skip cache IS minted for this cmd_sha."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    assert _is_cached() is False  # clean start (isolated journal home)
    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()),
        mock.patch(f"{_SAV}.verify_canary", return_value=_verify_env(ok=True)),
        mock.patch(f"{_SAV}.fire_second_canary", return_value=["9999"]),
        mock.patch(f"{_SAV}._mint_double_canary_sample"),
    ):
        result = submit_and_verify(tmp_path, spec=_spec(), stop_after_canary=True)

    assert result.verified is True
    assert _is_cached() is True


def test_failed_second_canary_blocks_and_does_not_mint(tmp_path: Path) -> None:
    """THE B7 REGRESSION: the first canary passes, the SECOND fails. The main is
    blocked (verified=False), and the cmd_sha is NOT cached — so a retry inside
    the 4h TTL re-runs the canary instead of skipping a never-validated pair."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    assert _is_cached() is False
    # First verify_canary call (first canary) ok; second call (‑canary2) fails.
    verdicts = [_verify_env(ok=True), _verify_env(ok=False, failure_kind="traceback")]
    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()),
        mock.patch(f"{_SAV}.verify_canary", side_effect=verdicts),
        mock.patch(f"{_SAV}.fire_second_canary", return_value=["9999"]),
        mock.patch(f"{_SAV}._mint_double_canary_sample") as m_mint,
    ):
        result = submit_and_verify(tmp_path, spec=_spec(), stop_after_canary=True)

    assert result.verified is False  # a failed second canary blocks the main
    m_mint.assert_not_called()  # the n=2 sample never mints on a failed pair
    # The poison: the blocked cmd_sha must NOT ride the #249 TTL cache.
    assert _is_cached() is False


def test_failed_first_canary_does_not_mint(tmp_path: Path) -> None:
    """A failed FIRST canary blocks the main and never mints (the second canary
    verify is skipped entirely — the code is already proven broken)."""
    from hpc_agent.ops.submit_and_verify import submit_and_verify

    assert _is_cached() is False
    with (
        mock.patch(f"{_SAV}.submit_flow", return_value=_submit_env()),
        mock.patch(f"{_SAV}.verify_canary", return_value=_verify_env(ok=False, failure_kind="oom_killed")),  # noqa: E501
        mock.patch(f"{_SAV}.fire_second_canary", return_value=["9999"]),
    ):
        result = submit_and_verify(tmp_path, spec=_spec(), stop_after_canary=True)

    assert result.verified is False
    assert _is_cached() is False
