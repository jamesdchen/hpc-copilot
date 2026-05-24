"""Subset of the CLI smoke tests, split out from the previously
~1380-LOC ``test_agent_cli.py`` for navigability.

Shared subprocess + envelope helpers live in :mod:`._helpers`.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.cli import aggregate as agg_mod
from hpc_agent.cli._helpers import EXIT_INTERNAL
from hpc_agent.cli.aggregate import cmd_aggregate
from hpc_agent.cli.dispatch import main as _cli_main

from ._helpers import SUBMIT_SPEC
from ._helpers import env_without_ssh_agent as _env_without_ssh_agent
from ._helpers import parse_envelope as _parse_envelope
from ._helpers import run_cli as _run_cli

# ─── Bug 6: aggregate no longer advertises a non-functional --output-dir ─


def test_aggregate_help_does_not_mention_output_dir() -> None:
    """The previous CLI accepted ``--output-dir`` and echoed it in the
    response envelope, but never threaded the value into the actual
    combiner call.  Drop the flag rather than ship a misleading one.
    """
    rc, out, _ = _run_cli("aggregate", "--help")
    assert rc == 0
    assert "--output-dir" not in out


# ─── Bug 7: aggregate failure → ok:false envelope with non-zero exit ─────


def test_aggregate_failure_emits_error_envelope(tmp_path: Path, monkeypatch) -> None:
    """When the combiner returns non-zero, the CLI used to emit ``ok:
    true`` on stdout and exit with EXIT_CLUSTER_ERROR — a contract
    violation since callers can drive logic from either field.  The fix
    routes the failure through ``_err`` so both fields agree.
    """
    import argparse
    from unittest.mock import patch

    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")

    # Seed a run so cmd_aggregate gets past the journal lookup.
    rec = RunRecord(
        run_id="r_abcd1234",
        profile="p",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/x",
        job_name="j",
        job_ids=["1"],
        total_tasks=1,
        submitted_at="2026-04-28T00:00:00+00:00",
        experiment_dir=str(tmp_path),
    )
    upsert_run(tmp_path, rec)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="r_abcd1234",
        wave=0,
        force=False,
    )
    captured: list[str] = []

    def fake_emit(payload):
        captured.append(json.dumps(payload))

    with (
        patch(
            "hpc_agent.cli.aggregate.combine_wave",
            return_value=(False, "", "boom: missing metrics"),
        ),
        patch("hpc_agent.cli._helpers._emit", side_effect=fake_emit),
    ):
        rc = cmd_aggregate(args)

    assert rc != 0  # exit code reflects failure
    payload = json.loads(captured[-1])
    assert payload["ok"] is False
    assert payload["error_code"] == "combiner_failed"
    assert payload["category"] == "cluster"


# ─── Bug 8: missing run record surfaces as journal_corrupt, not exec NF ──


def test_main_routes_journal_corrupt_for_missing_run(tmp_path: Path) -> None:
    """``runner.combine_wave`` raises ``JournalCorrupt`` when the run is
    missing.  Previously the CLI's blanket ``FileNotFoundError`` handler
    caught a bare exception and labelled it ``executor_not_found``,
    which steered agents at the wrong remediation.
    """
    import os

    journal = tmp_path / "journal"
    env_vars = {
        **os.environ,
        "HPC_JOURNAL_DIR": str(journal),
        # The SSH gate fires before the journal lookup; pre-populate so
        # this test exercises the journal path it intends to.
        "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", "/tmp/fake-agent.sock"),
    }

    rc, out, _ = _run_cli(
        "aggregate",
        "--experiment-dir",
        str(tmp_path),
        "--run-id",
        "definitely_not_a_run",
        "--wave",
        "0",
        env=env_vars,
    )
    assert rc != 0
    payload = _parse_envelope(out)
    assert payload["ok"] is False
    assert payload["error_code"] == "journal_corrupt"


def test_main_routes_unrelated_exception_to_internal(monkeypatch) -> None:
    """A genuinely unexpected exception (anything not ``HpcError`` /
    ``ValueError`` / ``TimeoutExpired``) should land on ``error_code:
    internal``, not the previous ``executor_not_found`` mislabel.
    """
    from unittest.mock import patch

    def boom(_args):
        raise RuntimeError("kaboom")

    captured: list[dict] = []

    def fake_emit(payload):
        captured.append(payload)

    # ``cmd_capabilities`` now lives in :mod:`hpc_agent.cli.setup`
    # (Tier 3 — no @primitive backing); patch at the canonical home so
    # the argparse parser's ``set_defaults(func=cmd_capabilities)``
    # binding (created in ``setup.register()`` at parser-build time)
    # actually sees the override.
    with (
        patch("hpc_agent.cli._helpers._emit", side_effect=fake_emit),
        patch("hpc_agent.cli.setup.cmd_capabilities", side_effect=boom),
    ):
        rc = _cli_main(["capabilities"])
    assert rc == EXIT_INTERNAL
    assert captured[-1]["error_code"] == "internal"


# ─── aggregate preconditions / postconditions / provenance ─────────────────


def _seed_aggregate_run(tmp_path: Path, run_id: str = "ml_abcd1234"):
    """Helper: seed a journal record so cmd_aggregate gets past lookup."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    rec = RunRecord(
        run_id=run_id,
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/exp",
        job_name="ml",
        job_ids=["12345"],
        total_tasks=2,
        submitted_at="2026-04-28T00:00:00+00:00",
        experiment_dir=str(tmp_path),
    )
    upsert_run(tmp_path, rec)
    return rec


def test_aggregate_precondition_blocks_combine_on_missing_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    """--require-outputs must refuse to combine when any per-task output is
    absent on the cluster, before invoking the user's combiner."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs="results/metrics.{task_id}.json",
        expect_output=None,
    )
    captured: list[dict] = []
    with (
        patch.object(
            agg_mod,
            "verify_per_task_outputs",
            return_value=["results/metrics.1.json"],
        ),
        patch.object(agg_mod, "combine_wave") as combine_mock,
        patch("hpc_agent.cli._helpers._emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cmd_aggregate(args)

    combine_mock.assert_not_called(), "combiner must not run when outputs missing"
    assert rc != 0
    payload = captured[-1]
    assert payload["ok"] is False
    assert payload["error_code"] == "outputs_missing"
    assert payload["retry_safe"] is True


def test_aggregate_postcondition_fails_when_combiner_artifact_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """--expect-output must surface combiner_failed when the combiner exits 0
    but the declared output isn't there or isn't parseable."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs=None,
        expect_output="results/metrics.json",
    )
    captured: list[dict] = []
    with (
        patch.object(agg_mod, "combine_wave", return_value=(True, "ok", "")),
        patch.object(
            agg_mod,
            "verify_combiner_artifact",
            return_value=(False, "is missing at /exp/results/metrics.json"),
        ),
        patch("hpc_agent.cli._helpers._emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cmd_aggregate(args)

    assert rc != 0
    payload = captured[-1]
    assert payload["ok"] is False
    assert payload["error_code"] == "combiner_failed"
    assert "results/metrics.json" in payload["message"]


def test_aggregate_envelope_carries_provenance_on_success(tmp_path: Path, monkeypatch) -> None:
    """Successful aggregate must embed provenance metadata in envelope.data."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs=None,
        expect_output=None,
    )
    captured: list[dict] = []
    with (
        patch.object(agg_mod, "combine_wave", return_value=(True, "ok", "")),
        patch("hpc_agent.cli._helpers._emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cmd_aggregate(args)

    assert rc == 0
    payload = captured[-1]
    assert payload["ok"] is True
    prov = payload["data"]["provenance"]
    assert prov["run_id"] == "ml_abcd1234"
    assert prov["wave"] == 0
    assert prov["profile"] == "ml"
    assert prov["cluster"] == "hoffman2"
    assert "combined_at" in prov


def test_aggregate_reads_sidecar_defaults_for_require_and_expect(
    tmp_path: Path, monkeypatch
) -> None:
    """When the CLI flags are omitted, the sidecar's aggregate_defaults.
    {require_outputs, expect_output} must be honored."""
    import argparse
    from unittest.mock import patch

    from hpc_agent.state.runs import write_run_sidecar

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    write_run_sidecar(
        tmp_path,
        run_id="ml_abcd1234",
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-04-28T00:00:00+00:00",
        executor="python -m ml.train",
        result_dir_template="results/{seed}",
        task_count=2,
        tasks_py_sha="1" * 64,
        profile="ml",
        aggregate_defaults={
            "require_outputs": "results/metrics.{task_id}.json",
            "expect_output": "results/metrics.json",
        },
    )

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs=None,  # no CLI flag — default should apply
        expect_output=None,
    )

    seen_template: list[str] = []
    seen_expect: list[str] = []

    def fake_verify_outputs(*, template, **_kw):
        seen_template.append(template)
        return []  # nothing missing

    def fake_verify_artifact(*, expect_output, **_kw):
        seen_expect.append(expect_output)
        return True, "ok"

    with (
        patch.object(agg_mod, "verify_per_task_outputs", side_effect=fake_verify_outputs),
        patch.object(agg_mod, "verify_combiner_artifact", side_effect=fake_verify_artifact),
        patch.object(agg_mod, "combine_wave", return_value=(True, "ok", "")),
        patch.object(
            agg_mod, "write_remote_provenance", return_value="/exp/results/_provenance.json"
        ),
        patch("hpc_agent.cli._helpers._emit"),
    ):
        rc = cmd_aggregate(args)

    assert rc == 0, "sidecar-defaulted aggregate should succeed"
    assert seen_template == ["results/metrics.{task_id}.json"]
    assert seen_expect == ["results/metrics.json"]


def test_aggregate_writes_sidecar_when_expect_output_set(tmp_path: Path, monkeypatch) -> None:
    """When --expect-output is set, the envelope reports the sidecar path."""
    import argparse
    from unittest.mock import patch

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    _seed_aggregate_run(tmp_path)

    args = argparse.Namespace(
        experiment_dir=tmp_path,
        run_id="ml_abcd1234",
        wave=0,
        force=False,
        require_outputs=None,
        expect_output="results/metrics.json",
    )
    captured: list[dict] = []
    with (
        patch.object(agg_mod, "combine_wave", return_value=(True, "ok", "")),
        patch.object(agg_mod, "verify_combiner_artifact", return_value=(True, "ok")),
        patch.object(
            agg_mod,
            "write_remote_provenance",
            return_value="/exp/results/_provenance.json",
        ),
        patch("hpc_agent.cli._helpers._emit", side_effect=lambda p: captured.append(p)),
    ):
        rc = cmd_aggregate(args)

    assert rc == 0
    payload = captured[-1]
    assert payload["data"]["provenance_sidecar"] == "/exp/results/_provenance.json"


def test_ssh_gate_does_not_block_local_only_subcommands(tmp_path: Path) -> None:
    """`capabilities`, `clusters list`, `submit --dry-run`, and `submit`
    (journal-only) must not be gated by SSH_AUTH_SOCK."""
    env = _env_without_ssh_agent()
    env["HPC_JOURNAL_DIR"] = str(tmp_path / "journal")

    rc, _, _ = _run_cli("capabilities", env=env)
    assert rc == 0

    rc, _, _ = _run_cli("clusters", "list", env=env)
    assert rc == 0

    submit_spec = tmp_path / "spec.json"
    submit_spec.write_text(json.dumps(SUBMIT_SPEC))
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(submit_spec),
        "--dry-run",
        env=env,
    )
    assert rc == 0, _parse_envelope(out)

    # Real submit is journal-only, no SSH — must succeed without agent.
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(submit_spec),
        env=env,
    )
    assert rc == 0, _parse_envelope(out)
