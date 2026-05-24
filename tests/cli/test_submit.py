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


def test_submit_persists_campaign_id_to_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec with `campaign_id` lands on the RunRecord and is later
    discoverable via ``hpc_agent.state.index.find_runs_by_campaign``."""
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

    # In-process check: ``find_runs_by_campaign`` re-resolves
    # ``HPC_JOURNAL_DIR`` from os.environ on every call (v3 fix), so
    # setting the env var here is sufficient — no module attribute
    # patching needed.
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(journal))
    from hpc_agent.state.index import find_runs_by_campaign

    matched = find_runs_by_campaign(tmp_path, "ml_ridge_q1")
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
