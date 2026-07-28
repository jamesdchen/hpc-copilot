"""``tag-session`` — the devx seam appends opaque records and stays inert.

The verb's whole contract is small on purpose (the 2026-07-28 separation
left the wheel exactly this one dev-loop affordance): stamp, append, echo.
What these tests pin beyond the happy path is the INGESTION posture — the
ledger tolerates torn/foreign lines on read (devx data must never wedge a
product path) and duplicates honestly on retry (append-only, no dedup
scheme the consumer would have to trust blind).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from hpc_agent._wire.actions.tag_session import TagSessionSpec
from hpc_agent.ops.devx_tag import tag_session
from hpc_agent.state.devx_tags import read_session_tags, session_tags_path

if TYPE_CHECKING:
    from pathlib import Path


def test_append_stamps_echoes_and_counts(tmp_path: Path) -> None:
    spec = TagSessionSpec(
        tags=["friction", "aggregate-ux"],
        note="re-read the brief twice before the y",
        session_id="sitting-42",
        run_id="run-001",
    )
    result = tag_session(experiment_dir=tmp_path, spec=spec)

    assert result.path == str(session_tags_path(tmp_path))
    assert result.count == 1
    assert result.record["tags"] == ["friction", "aggregate-ux"]
    assert result.record["note"] == "re-read the brief twice before the y"
    assert result.record["session_id"] == "sitting-42"
    assert result.record["run_id"] == "run-001"
    assert result.record["ts"]  # stamped at append time

    # The echo IS what the ledger holds.
    on_disk = read_session_tags(tmp_path)
    assert on_disk == [result.record]


def test_minimal_spec_defaults_optional_fields_null(tmp_path: Path) -> None:
    result = tag_session(experiment_dir=tmp_path, spec=TagSessionSpec(tags=["specimen"]))
    assert result.record["note"] is None
    assert result.record["session_id"] is None
    assert result.record["run_id"] is None


def test_retry_appends_a_second_record(tmp_path: Path) -> None:
    """Not idempotent, honestly: two calls = two records, in order."""
    spec = TagSessionSpec(tags=["friction"])
    tag_session(experiment_dir=tmp_path, spec=spec)
    result = tag_session(experiment_dir=tmp_path, spec=spec)
    assert result.count == 2
    assert [r["tags"] for r in read_session_tags(tmp_path)] == [["friction"], ["friction"]]


def test_reader_skips_torn_and_foreign_lines(tmp_path: Path) -> None:
    """Ingestion posture: a corrupt ledger degrades to the parseable subset."""
    tag_session(experiment_dir=tmp_path, spec=TagSessionSpec(tags=["good"]))
    path = session_tags_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-07-28T00:00:00+00:00", "tags": ["torn"\n')  # torn line
        fh.write('"just a json string, not a record"\n')  # foreign line
        fh.write(json.dumps({"ts": "2026-07-28T01:00:00+00:00", "tags": ["also-good"]}) + "\n")

    records = read_session_tags(tmp_path)
    assert [r["tags"] for r in records] == [["good"], ["also-good"]]

    # And the verb's count reflects the same degraded read.
    result = tag_session(experiment_dir=tmp_path, spec=TagSessionSpec(tags=["third"]))
    assert result.count == 3


@pytest.mark.parametrize("tags", [[], ["ok", "  "]], ids=["empty-list", "blank-entry"])
def test_spec_refuses_empty_or_blank_tags(tags: list[str]) -> None:
    """An untagged record is unqueryable data — refused at the spec layer."""
    with pytest.raises(ValidationError):
        TagSessionSpec(tags=tags)
