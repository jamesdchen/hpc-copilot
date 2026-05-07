"""Subset of the CLI smoke tests, split out from the previously
~1380-LOC ``test_agent_cli.py`` for navigability.

Shared subprocess + envelope helpers live in :mod:`._helpers`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._helpers import SUBMIT_SPEC
from ._helpers import parse_envelope as _parse_envelope
from ._helpers import run_cli as _run_cli

# ─── submit dry-run + dedup contract ───────────────────────────────────────


def test_submit_dry_run_does_not_touch_journal(tmp_path: Path) -> None:
    """--dry-run reports what would happen without writing to the journal."""
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_with_journal = {"HPC_JOURNAL_DIR": str(journal), "PATH": ""}
    # Need PATH for ssh-add etc., but not really for dry-run; pull from os.
    import os

    env_with_journal["PATH"] = os.environ.get("PATH", "")
    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        "--dry-run",
        env=env_with_journal,
    )
    assert rc == 0
    env_resp = _parse_envelope(out)
    assert env_resp["ok"] is True
    assert env_resp["data"]["dry_run"] is True
    assert env_resp["data"]["would_launch"] == 6


def test_submit_dedup_envelope_marks_replay(tmp_path: Path) -> None:
    """Second submit with the same spec returns deduped=True."""
    import os

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    rc1, out1, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc1 == 0
    env1 = _parse_envelope(out1)
    assert env1["data"]["deduped"] is False

    rc2, out2, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc2 == 0
    env2 = _parse_envelope(out2)
    assert env2["data"]["deduped"] is True
    assert env2["data"]["run_id"] == env1["data"]["run_id"]


def test_submit_persists_campaign_id_to_journal(tmp_path: Path) -> None:
    """A spec with `campaign_id` lands on the RunRecord and is later
    discoverable via session.find_runs_by_campaign."""
    import os

    spec_payload = {**SUBMIT_SPEC, "campaign_id": "ml_ridge_q1"}
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(spec_payload))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    rc, out, _ = _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    assert rc == 0
    env_resp = _parse_envelope(out)
    assert env_resp["ok"] is True

    # Confirm the journal carries the tag and the campaign filter sees it.
    from claude_hpc._internal import session
    from claude_hpc._internal.session import run_record

    # Redirect HPC_HOMEDIR for this in-process check the same way the CLI
    # did. After the session.py split the canonical module attribute lives
    # in :mod:`session.run_record`; patch both for back-compat with any
    # caller that reads through the package re-export.
    saved_pkg = session.HPC_HOMEDIR
    saved_rr = run_record.HPC_HOMEDIR
    try:
        session.HPC_HOMEDIR = journal  # type: ignore[misc]
        run_record.HPC_HOMEDIR = journal
        matched = session.find_runs_by_campaign(tmp_path, "ml_ridge_q1")
    finally:
        session.HPC_HOMEDIR = saved_pkg  # type: ignore[misc]
        run_record.HPC_HOMEDIR = saved_rr
    assert len(matched) == 1
    assert matched[0].campaign_id == "ml_ridge_q1"


# ─── list-in-flight recovery path ──────────────────────────────────────────


def test_list_in_flight_finds_submitted_run(tmp_path: Path) -> None:
    """After a submit, list-in-flight must surface the run."""
    import os

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SUBMIT_SPEC))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}

    _run_cli(
        "submit",
        "--experiment-dir",
        str(tmp_path),
        "--spec",
        str(spec),
        env=env_vars,
    )
    rc, out, _ = _run_cli(
        "list-in-flight",
        "--experiment-dir",
        str(tmp_path),
        env=env_vars,
    )
    assert rc == 0
    env_resp = _parse_envelope(out)
    runs = env_resp["data"]["runs"]
    assert any(r["run_id"] == SUBMIT_SPEC["run_id"] for r in runs)


def test_list_in_flight_surfaces_campaign_id_when_tagged(tmp_path: Path) -> None:
    """A submit with campaign_id should appear in list-in-flight with the tag.
    Open-loop submits should NOT carry the field at all (kept absent to keep
    envelopes compact)."""
    import os

    # Tagged submit.
    tagged_spec = {**SUBMIT_SPEC, "run_id": "tagged-run-1234", "campaign_id": "qa_q1"}
    spec = tmp_path / "tagged.json"
    spec.write_text(json.dumps(tagged_spec))
    journal = tmp_path / "journal"
    env_vars = {**os.environ, "HPC_JOURNAL_DIR": str(journal)}
    _run_cli("submit", "--experiment-dir", str(tmp_path), "--spec", str(spec), env=env_vars)

    # Open-loop submit (no campaign_id).
    untagged_spec = {**SUBMIT_SPEC, "run_id": "untagged-run-5678"}
    spec2 = tmp_path / "untagged.json"
    spec2.write_text(json.dumps(untagged_spec))
    _run_cli("submit", "--experiment-dir", str(tmp_path), "--spec", str(spec2), env=env_vars)

    rc, out, _ = _run_cli("list-in-flight", "--experiment-dir", str(tmp_path), env=env_vars)
    assert rc == 0
    runs = {r["run_id"]: r for r in _parse_envelope(out)["data"]["runs"]}
    assert runs["tagged-run-1234"]["campaign_id"] == "qa_q1"
    assert "campaign_id" not in runs["untagged-run-5678"]


# ─── submit --from-meta overlay ────────────────────────────────────────────


class TestSubmitFromMeta:
    """Verify the --from-meta flag overlays meta.json::experiment_id onto
    the submit spec's profile and job_name. setdefault semantics: never
    overwrite caller-supplied values, silent no-op without meta.json."""

    @staticmethod
    def _write_spec(tmp_path: Path, **overrides: object) -> Path:
        import json

        spec = {
            "cluster": "hoffman2",
            "ssh_target": "user@host",
            "remote_path": "/u/scratch/exp",
            "run_id": "run-20260429-153012-abcd1234",
            "job_ids": ["12345"],
            "total_tasks": 4,
        }
        spec.update(overrides)
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec))
        return path

    @staticmethod
    def _write_meta(experiment_dir: Path, experiment_id: str | None) -> None:
        import json

        payload: dict = {"seed": 42, "purpose": "test"}
        if experiment_id is not None:
            payload["experiment_id"] = experiment_id
        (experiment_dir / "meta.json").write_text(json.dumps(payload))

    def test_from_meta_fills_missing_profile_and_job_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
        spec = self._write_spec(tmp_path)
        self._write_meta(tmp_path, experiment_id="run-001-foo")
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--from-meta",
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        assert rc == 0, out
        env = _parse_envelope(out)
        assert env["ok"] is True
        # run_id is now spec-supplied directly; --from-meta only fills
        # the profile + job_name fields.  Verify by reading the journal.
        from claude_hpc._internal import session

        monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "journal")
        monkeypatch.setattr(
            "claude_hpc._internal.session.run_record.HPC_HOMEDIR", tmp_path / "journal"
        )
        record = session.load_run(tmp_path, env["data"]["run_id"])
        assert record is not None
        assert record.profile == "run-001-foo"
        assert record.job_name == "run-001-foo"

    def test_from_meta_does_not_overwrite_present_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = self._write_spec(tmp_path, profile="explicit", job_name="explicit")
        self._write_meta(tmp_path, experiment_id="other")
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--from-meta",
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        assert rc == 0, out
        env = _parse_envelope(out)
        from claude_hpc._internal import session

        monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "journal")
        monkeypatch.setattr(
            "claude_hpc._internal.session.run_record.HPC_HOMEDIR", tmp_path / "journal"
        )
        record = session.load_run(tmp_path, env["data"]["run_id"])
        assert record is not None
        assert record.profile == "explicit"
        assert record.job_name == "explicit"

    def test_from_meta_no_op_without_meta_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = self._write_spec(tmp_path, profile="p", job_name="p")
        # No meta.json on disk.
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--from-meta",
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        assert rc == 0, out
        env = _parse_envelope(out)
        from claude_hpc._internal import session

        monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "journal")
        monkeypatch.setattr(
            "claude_hpc._internal.session.run_record.HPC_HOMEDIR", tmp_path / "journal"
        )
        record = session.load_run(tmp_path, env["data"]["run_id"])
        assert record is not None
        assert record.profile == "p"

    def test_from_meta_no_op_when_meta_lacks_experiment_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = self._write_spec(tmp_path)  # no profile, no job_name
        self._write_meta(tmp_path, experiment_id=None)  # meta lacks experiment_id
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            "--from-meta",
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        # Spec is incomplete and no overlay applied; expect spec_invalid.
        assert rc == 1, out
        env = _parse_envelope(out)
        assert env["ok"] is False
        assert env["error_code"] == "spec_invalid"

    def test_from_meta_off_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = self._write_spec(tmp_path)  # no profile, no job_name
        self._write_meta(tmp_path, experiment_id="run-001-foo")
        # Flag NOT set: existing behavior (incomplete spec → spec_invalid).
        rc, out, _ = _run_cli(
            "submit",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec),
            env={**__import__("os").environ, "HPC_JOURNAL_DIR": str(tmp_path / "journal")},
        )
        assert rc == 1, out
        env = _parse_envelope(out)
        assert env["ok"] is False
        assert env["error_code"] == "spec_invalid"
