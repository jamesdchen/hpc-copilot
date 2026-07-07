"""notebook-render: determinism, annotation, execution + receipts."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import nbformat
import pytest
from hpc_agent_notebook_render._annotate import (
    CANONICALIZER,
    canonicalizer_version,
    section_output_sha,
)
from hpc_agent_notebook_render._models import NotebookRenderSpec
from hpc_agent_notebook_render.render import notebook_render

from hpc_agent import errors
from hpc_agent.state.decision_journal import read_decisions


def _spec(**kw: object) -> NotebookRenderSpec:
    base: dict[str, object] = {
        "audit_id": "aud-1",
        "source": "source.py",
        "template": "template.py",
    }
    base.update(kw)
    return NotebookRenderSpec(**base)  # type: ignore[arg-type]


def test_render_non_executed_is_byte_deterministic(experiment: Path) -> None:
    r1 = notebook_render(experiment_dir=experiment, spec=_spec(output_path="a.ipynb"))
    first = Path(r1.output_path).read_text(encoding="utf-8")
    # Re-render to a second path from the same inputs.
    r2 = notebook_render(experiment_dir=experiment, spec=_spec(output_path="b.ipynb"))
    second = Path(r2.output_path).read_text(encoding="utf-8")
    assert first == second
    assert not r1.executed


def test_render_carries_annotation_and_signoff_cells(experiment: Path) -> None:
    result = notebook_render(experiment_dir=experiment, spec=_spec(output_path="n.ipynb"))
    nb = nbformat.read(result.output_path, as_version=4)
    sources = [c["source"] for c in nb.cells]
    joined = "\n".join(sources)
    # Header states it is a render, not source of truth.
    assert "NOT the source of truth" in joined
    # An audit cell per section.
    assert "hpc-audit-cell: header" in joined
    assert "hpc-audit-cell: analysis" in joined
    # analysis is human_required (modified) -> sign-off scaffold; header is
    # auto_cleared (inherited, no assertions) -> no scaffold.
    assert "hpc-audit-signoff: analysis" in joined
    assert "hpc-audit-signoff: header" not in joined
    tiers = {s.slug: s.tier for s in result.sections}
    assert tiers["header"] == "auto_cleared"
    assert tiers["analysis"] == "human_required"


def test_record_receipts_requires_execute(experiment: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="requires execute"):
        notebook_render(experiment_dir=experiment, spec=_spec(record_receipts=True))


def test_execute_computes_output_sha_and_error(exec_experiment: Path) -> None:
    result = notebook_render(
        experiment_dir=exec_experiment,
        spec=_spec(source="source.py", execute=True, record_receipts=True),
    )
    assert result.executed
    # Both sections journaled a receipt (both slugs exist in the source).
    assert set(result.receipts_recorded) == {"ok", "boom"}
    assert not result.receipts_skipped
    # The journaled receipts carry a real output_sha and the correct error flag.
    records = read_decisions(exec_experiment, "notebook", "aud-1")
    by_slug = {
        r["resolved"]["section"]: r["resolved"]
        for r in records
        if r["block"] == "notebook-render-receipt"
    }
    assert by_slug["ok"]["error"] is False
    assert by_slug["boom"]["error"] is True
    assert len(by_slug["ok"]["output_sha"]) == 64  # sha256 hexdigest


def test_section_output_sha_is_stable() -> None:
    def _cells(stamp: int) -> list[dict[str, object]]:
        return [
            {
                "cell_type": "code",
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": "hi\n",
                        "metadata": {"t": stamp},  # timing must not enter the hash
                    }
                ],
            }
        ]

    sha_a, err_a = section_output_sha(_cells(123))
    sha_b, err_b = section_output_sha(_cells(999))
    assert sha_a == sha_b
    assert err_a is err_b is False


def test_execute_records_canonicalizer_identity(exec_experiment: Path) -> None:
    """An executed render records {canonicalizer, canonicalizer_version} on the
    result AND in the notebook metadata (core's receipt entry forbids the keys)."""
    result = notebook_render(
        experiment_dir=exec_experiment,
        spec=_spec(source="source.py", execute=True),
    )
    assert result.canonicalizer == CANONICALIZER == "nbdime"
    assert result.canonicalizer_version == importlib.metadata.version("nbdime")
    assert result.canonicalizer_version == canonicalizer_version()

    nb = nbformat.read(result.output_path, as_version=4)
    stamp = nb.metadata["hpc_audit_canonicalizer"]
    assert stamp["canonicalizer"] == "nbdime"
    assert stamp["canonicalizer_version"] == result.canonicalizer_version


def test_non_executed_render_has_no_canonicalizer(experiment: Path) -> None:
    """A non-executed render computes no output_sha, so it stamps no identity —
    and stays byte-deterministic (no version string in the notebook)."""
    result = notebook_render(experiment_dir=experiment, spec=_spec(output_path="n.ipynb"))
    assert result.canonicalizer is None
    assert result.canonicalizer_version is None
    nb = nbformat.read(result.output_path, as_version=4)
    assert "hpc_audit_canonicalizer" not in nb.metadata


def test_identical_code_identical_output_sha_across_two_executions(exec_experiment: Path) -> None:
    """Two independent executions of identical deterministic code yield identical
    per-section output_shas — the nbdime-canonicalized attestation is stable."""
    spec_a = _spec(source="source.py", execute=True, record_receipts=True, output_path="a.ipynb")
    spec_b = _spec(source="source.py", execute=True, record_receipts=True, output_path="b.ipynb")
    notebook_render(experiment_dir=exec_experiment, spec=spec_a)
    notebook_render(experiment_dir=exec_experiment, spec=spec_b)

    receipts: dict[str, list[str]] = {}
    for r in read_decisions(exec_experiment, "notebook", "aud-1"):
        if r["block"] == "notebook-render-receipt":
            receipts.setdefault(r["resolved"]["section"], []).append(r["resolved"]["output_sha"])
    # Each section was recorded twice (two renders) with an identical sha both times.
    assert receipts["ok"][0] == receipts["ok"][1]
    assert receipts["boom"][0] == receipts["boom"][1]
