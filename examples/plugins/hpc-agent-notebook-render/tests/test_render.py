"""notebook-render: determinism, annotation, execution + receipts."""

from __future__ import annotations

from pathlib import Path

import nbformat
import pytest
from hpc_agent_notebook_render._annotate import section_output_sha
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
