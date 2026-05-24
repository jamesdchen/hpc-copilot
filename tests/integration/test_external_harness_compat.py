"""Integration coverage for the external-harness wire surface.

The docs/integrations/CONTRACT.md surface (spawn env forwarding, the
``error_code`` enum, the failure → retry-policy mapping) is what every
external orchestrator builds against. Until now the only integration
fixture was ``tests/fixtures/mock_experiment/`` (two stub files) plus
local-only subprocess tests that exercised the dispatcher. Nothing
drove the CLI through a mocked SSH boundary to assert the contract
shape that a third-party harness would actually see.

This module fills that gap. It exercises three slices of the contract:

1. **ENV FORWARDING.** A ``submit-flow --dry-run`` carrying a
   ``job_env`` whose values contain spaces, embedded quotes, and
   non-ASCII unicode round-trips through the CLI / Pydantic spec
   validator without any character corruption or escaping drift. The
   spec is rebuilt from disk and re-validated against
   :class:`SubmitFlowSpec` to confirm byte-stable round-trip.

2. **ERROR CODES.** Each of four contract-pinned ``error_code`` values
   appears verbatim in the CLI's error envelope when the documented
   trigger fires:

   * ``ssh_unreachable`` — strip ``SSH_AUTH_SOCK`` so the fail-fast
     gate raises ``SshUnreachable``.
   * ``spec_invalid`` — malformed JSON in ``--spec``.
   * ``cluster_unknown`` — name not present in ``clusters.yaml``.
   * ``remote_command_failed`` — mocked ``ssh_run`` returns non-zero;
     drives ``record_status`` in-process to assert the envelope shape.

3. **RETRY POLICY.** ``cluster_failures_by_fingerprint`` rolls up a
   synthetic four-mode log set (gpu_oom / ssh_unreachable / walltime /
   unknown). The resulting clusters are annotated with the framework's
   default ``DEFAULT_AUTO_RETRY_POLICY``; the three policy-covered
   categories carry a ``retry_advice`` block (with ``ssh_unreachable``
   tracking eligible task ids) while the policy-less ``unknown``
   bucket is left untouched.

The mock-SSH approach: ``unittest.mock.patch("hpc_agent.infra.remote.ssh_run", ...)``
intercepts every cluster-touching subprocess call (per the seam used
throughout ``tests/runner/test_runner.py`` and
``tests/state/test_submit_cmd_sha_dedup.py``). Subprocess invocations
of ``uv run python -m hpc_agent`` drive the CLI's outer surface for
the envelope-shape assertions; direct Python imports drive the
categorization rollup so this file does not need a second subprocess
boundary just to inspect a list of dicts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from hpc_agent import agent_cli, runner
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops.recover.runner_failures import DEFAULT_AUTO_RETRY_POLICY
from hpc_agent.state import session
from hpc_agent.state.session import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


# ─── shared subprocess helpers ─────────────────────────────────────────────


def _run_cli(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Invoke ``python -m hpc_agent`` and return (rc, stdout, stderr).

    Mirrors :func:`tests.cli._helpers.run_cli` but kept inline so this
    file is fully self-contained — integration tests carry their own
    minimal subprocess wrapper.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_envelope(stdout: str) -> dict:
    """Parse the single-line JSON envelope (per the CLI contract)."""
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line; got {len(lines)}: {lines!r}"
    return json.loads(lines[0])


def _base_submit_flow_spec(run_id: str = "harness-20260523-000000-ab") -> dict:
    """Minimal ``submit-flow`` spec that satisfies every required field."""
    return {
        "profile": "ml",
        "cluster": "hoffman2",
        "ssh_target": "user@hoffman2.idre.ucla.edu",
        "remote_path": "/u/scratch/exp",
        "job_name": "ml",
        "run_id": run_id,
        "total_tasks": 1,
        "backend": "sge",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"K": "v"},
    }


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a ``subprocess.CompletedProcess`` for ``ssh_run`` mocks."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ─── 1. ENV FORWARDING ─────────────────────────────────────────────────────


# Each value exercises a different escape hazard: shell-interpolation
# (``$``), quoting (single + double), whitespace, embedded
# newlines/tabs, and non-BMP unicode (4-byte UTF-8). If any of these
# get mangled between the JSON file on disk, the JSON Schema check
# and the Pydantic model, the round-trip assertion below catches it.
_ENV_ROUNDTRIP_VALUES: dict[str, str] = {
    "PLAIN": "simple-value",
    "WITH_SPACES": "hello world  multiple    spaces",
    "WITH_DOUBLE_QUOTES": 'contains "double" quotes inside',
    "WITH_SINGLE_QUOTES": "it's got an apostrophe",
    "WITH_BACKSLASH": r"path\with\backslashes",
    "WITH_DOLLAR": "$HOME_should_stay_literal",
    "WITH_NEWLINE": "line1\nline2\twith-tab",
    "WITH_UNICODE": "café résumé 日本語 🚀",
}


def test_submit_flow_dry_run_preserves_complex_job_env(tmp_path: Path) -> None:
    """End-to-end CLI invocation: ``submit-flow --dry-run`` must accept
    every shape in :data:`_ENV_ROUNDTRIP_VALUES` and the spec file
    re-loaded from disk must match what we wrote, byte for byte.

    The CLI subprocess passes the value through:
      1. JSON parse (``_load_spec``)
      2. JSON Schema validation (``submit_flow.input.json``)
      3. Pydantic ``SubmitFlowSpec.model_validate``

    Failure on any step would surface as ``spec_invalid``; success
    confirms each layer agrees the strings are valid ``string`` values
    without escape-sequence drift.
    """
    spec_path = tmp_path / "spec.json"
    spec = _base_submit_flow_spec()
    spec["job_env"] = dict(_ENV_ROUNDTRIP_VALUES)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "HPC_JOURNAL_DIR": str(tmp_path / "journal"),
    }
    rc, out, err = _run_cli(
        "submit-flow",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec_path),
        "--dry-run",
        env=env,
    )
    assert rc == 0, f"submit-flow --dry-run failed: rc={rc} stdout={out!r} stderr={err!r}"
    envelope = _parse_envelope(out)
    assert envelope["ok"] is True
    assert envelope["data"]["dry_run"] is True
    assert envelope["data"]["run_id"] == spec["run_id"]


def test_complex_job_env_round_trips_through_pydantic_model(tmp_path: Path) -> None:
    """Direct Pydantic round-trip: load the same spec from disk, validate
    via :class:`SubmitFlowSpec`, and assert every key/value is exactly
    what was written.

    Pairs with the CLI dry-run test above; the dry-run envelope does
    not echo ``job_env`` back, so this asserts the *value* survives
    while the dry-run asserts the CLI surface *accepts* it.
    """
    spec_path = tmp_path / "spec.json"
    spec = _base_submit_flow_spec()
    spec["job_env"] = dict(_ENV_ROUNDTRIP_VALUES)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    loaded = json.loads(spec_path.read_text(encoding="utf-8"))
    validated = SubmitFlowSpec.model_validate(loaded)

    assert validated.job_env == _ENV_ROUNDTRIP_VALUES
    for key, expected in _ENV_ROUNDTRIP_VALUES.items():
        actual = validated.job_env[key]
        assert actual == expected, (
            f"job_env[{key!r}] corrupted in round-trip: expected {expected!r}, got {actual!r}"
        )


# ─── 2. ERROR CODES ────────────────────────────────────────────────────────


def test_error_code_ssh_unreachable_when_ssh_auth_sock_missing(tmp_path: Path) -> None:
    """Cluster-touching subcommands must fail fast with
    ``ssh_unreachable`` (exit 2) when ``SSH_AUTH_SOCK`` isn't in the
    spawn env — per CONTRACT.md, integrators expect this exact code
    instead of a stalled SSH handshake.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "HPC_JOURNAL_DIR": str(tmp_path / "journal"),
        # Deliberately no SSH_AUTH_SOCK.
    }
    rc, out, _ = _run_cli(
        "status",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "any_run_id",
        env=env,
    )
    assert rc == 2, f"ssh_unreachable must exit 2 (network category); got {rc}"
    envelope = _parse_envelope(out)
    assert envelope["ok"] is False
    assert envelope["error_code"] == "ssh_unreachable"
    assert envelope["category"] == "network"
    assert envelope["retry_safe"] is True
    assert "remediation" in envelope


def test_error_code_spec_invalid_on_malformed_json(tmp_path: Path) -> None:
    """A ``--spec`` file that is not parseable JSON must surface as
    ``spec_invalid`` (exit 1, user category) — not as an internal
    crash or a generic exit-3 envelope.
    """
    spec = tmp_path / "bad.json"
    spec.write_text("{this is not valid json", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "HPC_JOURNAL_DIR": str(tmp_path / "journal"),
    }
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        "--dry-run",
        env=env,
    )
    assert rc == 1, f"spec_invalid must exit 1 (user category); got {rc}"
    envelope = _parse_envelope(out)
    assert envelope["ok"] is False
    assert envelope["error_code"] == "spec_invalid"
    assert envelope["category"] == "user"
    assert envelope["retry_safe"] is False


def test_error_code_cluster_unknown_for_undefined_cluster(tmp_path: Path) -> None:
    """``clusters describe <unknown>`` is the canonical
    ``cluster_unknown`` trigger — exit 1, ``ClusterUnknown``-shaped
    envelope with the remediation pointing to ``clusters list``.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    rc, out, _ = _run_cli(
        "clusters",
        "describe",
        "definitely-not-a-real-cluster-name",
        env=env,
    )
    assert rc == 1, f"cluster_unknown must exit 1 (user category); got {rc}"
    envelope = _parse_envelope(out)
    assert envelope["ok"] is False
    assert envelope["error_code"] == "cluster_unknown"
    assert envelope["category"] == "user"
    assert envelope["retry_safe"] is False
    # Remediation must mention the discovery path so a harness can
    # auto-recover by listing the configured clusters.
    assert "clusters list" in envelope["remediation"]


def test_error_code_remote_command_failed_when_ssh_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hpc-agent status`` invokes the cluster-side reporter via
    ``ssh_run``. When that subprocess returns non-zero, the runner
    raises :class:`RemoteCommandFailed`, which the CLI must surface
    verbatim as ``error_code: remote_command_failed`` (cluster
    category, ``retry_safe: false`` — operator must inspect, not
    auto-retry).

    Driven in-process via :func:`cli.main` so we can patch
    ``ssh_run``; subprocess-based mocking would require an extra
    fixture layer for no real signal. Going through ``main`` (not the
    bare ``cmd_status`` adapter) is load-bearing — the
    ``HpcError → envelope`` translation lives in ``main``'s
    ``try/except`` block, not on each individual adapter.
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")

    # Seed a journaled run so cmd_status gets past the JournalCorrupt gate.
    run_id = "remote-fail-test"
    record = RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="j",
        job_ids=["12345"],
        total_tasks=1,
        submitted_at="2026-05-23T00:00:00+00:00",
        experiment_dir=str(tmp_path.resolve()),
    )
    session.upsert_run(tmp_path, record)

    captured: list[dict] = []

    def _capture(payload: dict) -> None:
        captured.append(payload)

    # Mock ssh_run to simulate a remote process that returned non-zero
    # (e.g., the cluster-side reporter blew up on a missing tasks.py).
    fake_ssh = _completed(returncode=2, stdout="", stderr="status reporter: import failed")
    with (
        patch("hpc_agent.infra.remote.ssh_run", return_value=fake_ssh),
        patch("hpc_agent.cli._helpers._emit", side_effect=_capture),
    ):
        rc = agent_cli.main(["status", "--experiment-dir", str(tmp_path), "--run-id", run_id])

    assert rc == 2, f"remote_command_failed maps to cluster category (exit 2); got {rc}"
    assert captured, "no envelope emitted"
    payload = captured[-1]
    assert payload["ok"] is False
    assert payload["error_code"] == "remote_command_failed"
    assert payload["category"] == "cluster"
    assert payload["retry_safe"] is False


# ─── 3. RETRY POLICY ───────────────────────────────────────────────────────


def test_cluster_failures_rollup_covers_all_four_categories_with_retry_advice(
    tmp_path: Path,
) -> None:
    """Synthetic four-mode log set (gpu_oom + ssh_unreachable + walltime
    + unknown) flows through ``cluster_failures_by_fingerprint`` →
    ``annotate_clusters_with_retry_advice`` with the framework's
    :data:`DEFAULT_AUTO_RETRY_POLICY`.

    Asserts:
      * All four categories appear in the rollup.
      * ``retry_advice`` is attached to the three policy-covered
        categories (gpu_oom, walltime, ssh_unreachable).
      * The ``unknown`` bucket has no ``retry_advice`` block (no
        policy entry → leave untouched).
      * ``ssh_unreachable``'s eligibility is tracked: task ids land
        in either ``eligible_task_ids`` or ``blocked_task_ids`` per
        ``record.retries[tid].attempts < max_attempts``.
    """
    logs: list[dict] = [
        # gpu_oom — torch OOM is the canonical CUDA-OOM fingerprint.
        {"task_id": 1, "content": "torch.cuda.OutOfMemoryError: CUDA out of memory."},
        # ssh_unreachable — the transport itself failed; the runner
        # bucket-routes ``missing=True`` + ``ssh_error`` into this
        # category (vs ``log_missing`` for genuinely-empty logs).
        {"task_id": 2, "missing": True, "ssh_error": "ssh: connect to host h port 22: timed out"},
        # walltime — SLURM's documented termination string.
        {
            "task_id": 3,
            "content": "slurmstepd: error: *** JOB 42 ON node CANCELLED DUE TO TIME LIMIT ***",
        },
        # unknown — content the categorizer recognises as nothing
        # (no Traceback prefix, no OOM/walltime/preempt marker).
        {"task_id": 4, "content": "something went sideways but we can't tell what"},
    ]
    clusters = runner.cluster_failures_by_fingerprint(logs)
    categories = {c["category"] for c in clusters}
    assert {"gpu_oom", "ssh_unreachable", "walltime", "unknown"}.issubset(categories), (
        f"missing categories in rollup: {sorted(categories)}"
    )

    # Seed a RunRecord with one prior attempt for task 1 so the
    # eligibility logic has both branches to exercise:
    #   * task 1 (gpu_oom) → attempts=1, max_attempts=1 → blocked
    #   * task 2 (ssh_unreachable) → no prior → eligible
    #   * task 3 (walltime) → no prior → eligible
    record = RunRecord(
        run_id="retry-policy-test",
        profile="p",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/x",
        job_name="j",
        job_ids=["1"],
        total_tasks=4,
        submitted_at="2026-05-23T00:00:00+00:00",
        experiment_dir=str(tmp_path.resolve()),
        retries={"1": {"attempts": 1, "category": "gpu_oom", "overrides": {}}},
    )
    annotated = runner.annotate_clusters_with_retry_advice(
        clusters,
        auto_retry_policy=DEFAULT_AUTO_RETRY_POLICY,
        record=record,
    )

    by_cat = {c["category"]: c for c in annotated}

    # Policy-covered categories carry retry_advice.
    for covered in ("gpu_oom", "ssh_unreachable", "walltime"):
        assert "retry_advice" in by_cat[covered], (
            f"{covered!r} cluster missing retry_advice; covered by DEFAULT_AUTO_RETRY_POLICY"
        )
        advice = by_cat[covered]["retry_advice"]
        # Every advice block has the documented three-key shape.
        assert set(advice) >= {"policy", "eligible_task_ids", "blocked_task_ids"}
        # Echoed policy matches the framework default.
        assert advice["policy"] == DEFAULT_AUTO_RETRY_POLICY[covered]

    # ssh_unreachable's eligibility is tracked: task 2 has no prior
    # attempts, so it lands in eligible (max_attempts=2 per the
    # framework default).
    ssh_advice = by_cat["ssh_unreachable"]["retry_advice"]
    assert 2 in ssh_advice["eligible_task_ids"], (
        f"task 2 should be eligible for ssh_unreachable retry; advice={ssh_advice!r}"
    )
    assert ssh_advice["policy"]["max_attempts"] == 2

    # gpu_oom: task 1 already burned its single attempt → blocked.
    gpu_advice = by_cat["gpu_oom"]["retry_advice"]
    assert 1 in gpu_advice["blocked_task_ids"], (
        f"task 1 (1 prior attempt, max=1) should be blocked; advice={gpu_advice!r}"
    )

    # The policy-less ``unknown`` category is left untouched — the
    # annotator only attaches advice when DEFAULT_AUTO_RETRY_POLICY
    # has a matching entry. A harness sees this as "operator must
    # decide" without any auto-retry hint.
    assert "retry_advice" not in by_cat["unknown"], (
        "unknown category must not gain retry_advice — no policy entry covers it"
    )
