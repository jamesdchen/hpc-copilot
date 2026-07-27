"""F5 (context-footprint plan): the ``read-decisions`` digest mode.

Digest serves per-record identity/ordering metadata with the bodies omitted;
the DEFAULT response is byte-identical to the pre-digest wire shape (the
``records_digest`` key is absent, not null — the serializer pin).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hpc_agent._wire.queries.decision_journal import ReadDecisionsInput
from hpc_agent.ops.decision.journal import read_decisions
from hpc_agent.state import decision_journal as sdj


@pytest.fixture
def experiment_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_home"))
    exp = tmp_path / "exp"
    exp.mkdir()
    sdj.append_decision(
        exp,
        scope_kind="run",
        scope_id="widget-run-1",
        block="submit-s1",
        response="proceed with the widget sweep",
        resolved={"n_tasks": 4, "queue": "short"},
    )
    sdj.append_decision(
        exp,
        scope_kind="run",
        scope_id="widget-run-1",
        block="submit-s2",
        response="y" * 60,
        resolved={},
    )
    return exp


def _spec(**kw) -> ReadDecisionsInput:
    return ReadDecisionsInput.model_validate(
        {"scope_kind": "run", "scope_id": "widget-run-1", **kw}
    )


def test_digest_omits_bodies_and_carries_identity(experiment_dir: Path) -> None:
    result = read_decisions(experiment_dir=experiment_dir, spec=_spec(digest=True))
    assert result.records == []  # no bodies
    assert result.count == 2
    assert result.records_digest is not None and len(result.records_digest) == 2
    first = result.records_digest[0]
    assert first.block == "submit-s1"
    assert first.resolved_keys == ["n_tasks", "queue"]
    expected_sha = hashlib.sha256(b"proceed with the widget sweep").hexdigest()[:12]
    assert first.response_sha12 == expected_sha
    assert first.response_chars == len("proceed with the widget sweep")
    # The prose itself appears nowhere in the serialized digest response.
    blob = json.dumps(result.model_dump(mode="json"))
    assert "proceed with the widget sweep" not in blob


def test_default_response_is_byte_identical_pre_digest_shape(experiment_dir: Path) -> None:
    result = read_decisions(experiment_dir=experiment_dir, spec=_spec())
    dumped = result.model_dump(mode="json")
    # The additive digest key is ABSENT (not null) on the default path — the
    # byte-identity pin for every pre-digest consumer.
    assert "records_digest" not in dumped
    assert dumped["count"] == 2
    assert [r["block"] for r in dumped["records"]] == ["submit-s1", "submit-s2"]


def test_digest_false_explicit_matches_default(experiment_dir: Path) -> None:
    default = read_decisions(experiment_dir=experiment_dir, spec=_spec())
    explicit = read_decisions(experiment_dir=experiment_dir, spec=_spec(digest=False))
    assert default.model_dump(mode="json") == explicit.model_dump(mode="json")
