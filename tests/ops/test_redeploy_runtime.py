"""``redeploy-runtime`` behaviour (U5 / F1).

This verb is the rank-0 option of the ``combiner_missing`` recovery menu — the
thing every combine refusal tells a human to run, and the thing the 2026-07-30
incident had to substitute with a hand ``scp``. A remedy that is only named and
never exercised is a remedy nobody has checked, so its contract is pinned here:

* it resolves everything from the run's own journal record — no cluster flag,
  no path for the caller to compose;
* it repairs BOTH deploy roots (base + §10.S4 code tree) with the content-hash
  cache BYPASSED, because the cache's presence lie is what the verb exists to
  undo;
* it verifies in ONE exec for N roots, attributing per-root by TAG;
* and it reports ``ok`` only on positive evidence — a severed channel is
  ``unknown``, never a confirmed repair.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from hpc_agent import errors
from hpc_agent.execution.mapreduce import deployed_artifact as D
from hpc_agent.ops.redeploy_runtime import redeploy_runtime
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

_RUN_ID = "hoffman2_7f3ac91b"
_BASE = "/scratch1/u/exp"
_TREE = "/scratch1/u/exp/.hpc/trees/ab12cd34ef56"


@pytest.fixture(autouse=True)
def _journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


@pytest.fixture(autouse=True)
def _no_cluster_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduler lookup must not read the developer's real clusters.yaml."""
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.load_clusters_config", lambda: {"hoffman2": {"scheduler": "sge"}}
    )
    monkeypatch.setattr("hpc_agent.infra.clusters.resolve_ssh_target", lambda _rec: "user@hoffman2")


def _seed(tmp_path: Path, *, remote_path: str = _BASE, job_env: dict[str, str] | None = None):
    rec = RunRecord(
        run_id=_RUN_ID,
        profile="ml",
        cluster="hoffman2",
        ssh_target="stale@olduser",  # deliberately stale — resolve_ssh_target wins
        remote_path=remote_path,
        job_name="ml",
        job_ids=["4815162"],
        total_tasks=4,
        submitted_at="2026-07-30T18:00:00+00:00",
        experiment_dir=str(tmp_path),
        job_env=job_env or {},
    )
    upsert_run(tmp_path, rec)
    return rec


def _probe_stdout(*values: str) -> str:
    """Compose a tagged probe stream: value per root index, in order."""
    return "".join(f"\n{D.COMBINER_PROBE_PREFIX} {i} {v}\n" for i, v in enumerate(values))


class _Harness:
    """Captures the deploy calls and serves a canned verification stream."""

    def __init__(self, probe_stdout: str, *, rc: int = 0) -> None:
        self.deploys: list[dict[str, Any]] = []
        self.probes: list[str] = []
        self._stdout = probe_stdout
        self._rc = rc

    def deploy(self, **kw: Any) -> None:
        self.deploys.append(kw)

    def ssh_run(self, cmd: str, **_kw: Any) -> SimpleNamespace:
        self.probes.append(cmd)
        return SimpleNamespace(returncode=self._rc, stdout=self._stdout, stderr="boom")

    def __enter__(self):  # noqa: ANN204
        self._patches = [
            patch("hpc_agent.infra.transport.deploy_runtime", side_effect=self.deploy),
            patch("hpc_agent.infra.remote.ssh_run", side_effect=self.ssh_run),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        for p in self._patches:
            p.stop()


# ── resolution ──────────────────────────────────────────────────────────────


def test_resolves_ssh_target_and_remote_path_from_the_journal(tmp_path: Path) -> None:
    """No cluster flag, no path argument — the run record is the whole input."""
    _seed(tmp_path)
    with _Harness(_probe_stdout(D.local_combiner_sha())) as h:
        result = redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert result["ssh_target"] == "user@hoffman2"  # resolved live, not the stale record value
    assert result["deploy_roots"] == [_BASE]
    assert [d["remote_path"] for d in h.deploys] == [_BASE]
    assert h.deploys[0]["ssh_target"] == "user@hoffman2"


def test_no_journal_record_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="no journal record"):
        redeploy_runtime(tmp_path, run_id="does-not-exist")


def test_no_remote_path_is_spec_invalid(tmp_path: Path) -> None:
    """A pure-API backend has no tree to deploy into — refuse, don't guess."""
    _seed(tmp_path, remote_path="")
    with pytest.raises(errors.SpecInvalid, match="no remote_path"):
        redeploy_runtime(tmp_path, run_id=_RUN_ID)


def test_empty_run_id_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="run_id is required"):
        redeploy_runtime(tmp_path, run_id="")


# ── the repair ──────────────────────────────────────────────────────────────


def test_both_roots_are_deployed_with_the_cache_bypassed(tmp_path: Path) -> None:
    """The cache's presence lie is the thing being undone — honouring it would
    reproduce the dropout the verb exists to repair."""
    _seed(tmp_path, job_env={"REPO_DIR": _TREE})
    with _Harness(_probe_stdout(D.local_combiner_sha(), D.local_combiner_sha())) as h:
        result = redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert result["deploy_roots"] == [_BASE, _TREE], "base first, then the code tree"
    assert [d["remote_path"] for d in h.deploys] == [_BASE, _TREE]
    assert all(d["use_cache"] is False for d in h.deploys)
    assert all(d["scheduler"] == "sge" for d in h.deploys)
    assert result["ok"] is True


def test_use_cache_true_is_honoured_when_explicitly_asked(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _Harness(_probe_stdout(D.local_combiner_sha())) as h:
        redeploy_runtime(tmp_path, run_id=_RUN_ID, use_cache=True)

    assert h.deploys[0]["use_cache"] is True


def test_a_tree_equal_to_the_base_is_not_deployed_twice(tmp_path: Path) -> None:
    """``repo_dir_for_run`` returns the base for a run that predates §10.S4."""
    _seed(tmp_path, job_env={"REPO_DIR": _BASE})
    with _Harness(_probe_stdout(D.local_combiner_sha())) as h:
        result = redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert result["deploy_roots"] == [_BASE]
    assert len(h.deploys) == 1


# ── verification ────────────────────────────────────────────────────────────


def test_two_roots_are_verified_in_one_exec(tmp_path: Path) -> None:
    """N roots, ONE round-trip: the snippets are concatenated, not serialised."""
    _seed(tmp_path, job_env={"REPO_DIR": _TREE})
    with _Harness(_probe_stdout(D.local_combiner_sha(), D.local_combiner_sha())) as h:
        redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert len(h.probes) == 1, f"expected one verification exec, got {len(h.probes)}"
    assert f"{_BASE}/{D.COMBINER_REL}" in h.probes[0]
    assert f"{_TREE}/{D.COMBINER_REL}" in h.probes[0]


def test_per_root_verdicts_are_attributed_by_tag_not_position(tmp_path: Path) -> None:
    """F9: the map an operator reads must point at the right root.

    The emissions arrive REVERSED here (root 1 before root 0), which a
    positional demux would silently mis-assign — reporting the tree's absence
    against the base and sending the operator to the wrong directory. Tags make
    order irrelevant.
    """
    _seed(tmp_path, job_env={"REPO_DIR": _TREE})
    reversed_stream = (
        f"\n{D.COMBINER_PROBE_PREFIX} 1 absent\n"
        f"\n{D.COMBINER_PROBE_PREFIX} 0 {D.local_combiner_sha()}\n"
    )
    with _Harness(reversed_stream):
        result = redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert result["verified"][_BASE]["state"] == "present"
    assert result["verified"][_TREE]["state"] == "absent"
    assert result["ok"] is False


def test_a_truncated_stream_reports_the_lost_root_unknown_not_the_wrong_root(
    tmp_path: Path,
) -> None:
    """Root 0's line is cut. Positional demux would hand root 1's verdict to
    root 0 and report nothing for root 1; tagged demux loses exactly root 0."""
    _seed(tmp_path, job_env={"REPO_DIR": _TREE})
    with _Harness(f"\n{D.COMBINER_PROBE_PREFIX} 1 {D.local_combiner_sha()}\n"):
        result = redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert result["verified"][_BASE]["state"] == "unknown"
    assert result["verified"][_BASE]["sha"] is None
    assert result["verified"][_TREE]["state"] == "present"
    assert result["ok"] is False, "an unverified root must never green the repair"


def test_an_absent_root_after_the_deploy_is_not_ok(tmp_path: Path) -> None:
    """The deploy ran and the artifact still isn't there — report the failure."""
    _seed(tmp_path)
    with _Harness(_probe_stdout("absent")):
        result = redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert result["ok"] is False
    assert result["verified"][_BASE] == {"state": "absent", "sha": None}


def test_a_severed_channel_is_all_unknown_and_not_ok(tmp_path: Path) -> None:
    """rc 0 with empty stdout — the silence that must never read as success."""
    _seed(tmp_path, job_env={"REPO_DIR": _TREE})
    with _Harness(""):
        result = redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert result["ok"] is False
    assert {v["state"] for v in result["verified"].values()} == {"unknown"}


def test_a_stale_sha_is_reported_and_not_ok(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _Harness(_probe_stdout("0" * 64)):
        result = redeploy_runtime(tmp_path, run_id=_RUN_ID)

    assert result["ok"] is False
    assert result["verified"][_BASE]["state"] == "stale"
    assert result["verified"][_BASE]["sha"] == "0" * 64


def test_a_failed_verification_probe_raises(tmp_path: Path) -> None:
    """A transport failure is not a verdict — it is an error."""
    _seed(tmp_path)
    with _Harness("", rc=255), pytest.raises(errors.RemoteCommandFailed, match="probe failed"):
        redeploy_runtime(tmp_path, run_id=_RUN_ID)


def test_the_verb_is_registered_and_the_menu_names_it(tmp_path: Path) -> None:
    """The remediation must name a command that exists, with the right flags."""
    import hpc_agent
    from hpc_agent._kernel.registry.primitive import get_registry
    from hpc_agent.recovery.registry import menu_for

    hpc_agent.register_primitives()
    assert "redeploy-runtime" in get_registry()

    menu = menu_for("combiner_missing")
    rank0 = min(menu.options, key=lambda o: o.safety_rank).cli_command
    assert rank0.startswith("hpc-agent redeploy-runtime ")
    assert "--run-id <run_id>" in rank0
    assert "--experiment-dir <experiment_dir>" in rank0
    # No option may tell the operator to resubmit tasks: the per-task outputs
    # are fine and resubmitting repairs nothing.
    assert not any("resubmit" in o.cli_command.lower() for o in menu.options)
