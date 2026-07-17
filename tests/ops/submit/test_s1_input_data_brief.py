"""U-DATA1 input-data S1 nudge (reproducibility Wave-1, 2026-07-17):
input-data capture is OPT-IN, so a run that declares NO input roots writes a
byte-identical null-data sidecar, silently invisible to all data-drift
attribution — the #1 reproducibility gap. S1's resolved brief — the human
boundary the greenlight crosses BEFORE submit-s2 detaches and spends the whole
compute — gains a CODE-rendered, NEVER-BLOCKING nudge pointing at the ONE input
declaration field (``interview.json``'s ``audited_source.input_roots``, read
through the one declaration reader ``state.data_manifest.declared_input_roots``).

DISCLOSURE only, mirroring the shipped ``reducibility`` / dirty-worktree
disclosures: no gate, the bare ``y`` flow byte-unchanged, a run WITH declared
roots adds NO brief key (the regression pin), a wrong-shaped declaration SAYS
could-not-determine (no silent skip).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import hpc_agent.ops.submit_blocks as blocks
from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsResult
from hpc_agent._wire.workflows.submit_blocks import SubmitS1Spec
from tests.contracts.never_blocking import assert_never_blocking

_RUN_ID = "ridge-abcd1234"


def _declare(experiment_dir: Path, input_roots: Any) -> None:
    """Write an ``interview.json`` whose ``audited_source.input_roots`` is
    *input_roots* verbatim (a list, or a malformed shape for the ambiguous case)."""
    (experiment_dir / "interview.json").write_text(
        json.dumps({"audited_source": {"input_roots": input_roots}}), encoding="utf-8"
    )


def _clean_walk() -> WalkSubmitAmbiguitiesInput:
    return WalkSubmitAmbiguitiesInput.model_validate(
        {
            "cluster": "carc",
            "configured_clusters": ["carc", "hoffman2"],
            "goal": "sweep ridge",
            "tasks_py_present": True,
            "entry_point_resolved": True,
            "data_axis_resolved": True,
            "homogeneous_axes_resolved": True,
        }
    )


def _fake_rr(sidecar_path: Path, *, run_id: str | None = _RUN_ID) -> ResolveSubmitInputsResult:
    return ResolveSubmitInputsResult(
        stage_reached="resolved",
        needs_decision=True,
        reason="plan resolved; stage & canary.",
        run_id=run_id,
        cmd_sha="0" * 64,
        submit_spec={"rsync_excludes": None},
        sidecar_path=str(sidecar_path),
    )


# ── the pure helper ───────────────────────────────────────────────────────────


def test_input_data_brief_no_declaration_carries_the_nudge(tmp_path: Path) -> None:
    """No input declaration at all → the never-blocking ``no_input_data_declared``
    nudge naming the real field (``input_roots``)."""
    brief = blocks._input_data_brief(tmp_path)

    assert brief is not None
    assert brief["checked"] is True
    assert brief["issue"] == "no_input_data_declared"
    assert "input_roots" in brief["line"]  # the REAL field name, not a paraphrase
    assert "fingerprint" in brief["line"]


def test_input_data_brief_declared_roots_returns_none(tmp_path: Path) -> None:
    """A usable ``input_roots`` list → ``None``, so the brief stays byte-identical."""
    _declare(tmp_path, ["data", "configs"])
    assert blocks._input_data_brief(tmp_path) is None


def test_input_data_brief_malformed_declaration_says_could_not_determine(tmp_path: Path) -> None:
    """An ``input_roots`` present but not a list (a bare string path) is a
    wrong-shaped declaration → the honest could-not-determine line, never a
    silent skip and never a false 'nothing declared'."""
    _declare(tmp_path, "data/")  # a string, not a list — ambiguous shape

    brief = blocks._input_data_brief(tmp_path)

    assert brief is not None
    assert brief["checked"] is False
    assert "could not be determined" in brief["reason"]
    assert "input_roots" in brief["reason"]


def test_input_data_brief_empty_list_is_the_nudge(tmp_path: Path) -> None:
    """An explicit empty ``input_roots: []`` is a well-formed 'declared nothing
    usable' shape (not ambiguous) → the definitive nudge, not could-not-determine."""
    _declare(tmp_path, [])

    brief = blocks._input_data_brief(tmp_path)

    assert brief is not None
    assert brief["checked"] is True
    assert brief["issue"] == "no_input_data_declared"


def test_input_data_brief_junk_list_is_the_nudge(tmp_path: Path) -> None:
    """A list with no usable string entries resolves cleanly to 'declared nothing
    usable' (a well-formed list) → the definitive nudge, not could-not-determine."""
    _declare(tmp_path, [123, ""])

    brief = blocks._input_data_brief(tmp_path)

    assert brief is not None
    assert brief["checked"] is True
    assert brief["issue"] == "no_input_data_declared"


def test_input_data_brief_routes_through_the_one_declaration_reader(tmp_path: Path) -> None:
    """The nudge reads input roots through the ONE declaration reader
    (``state.data_manifest.declared_input_roots``), never a re-inlined
    interview-parse (one-definition rule)."""
    import hpc_agent.state.data_manifest as sdm

    _declare(tmp_path, ["data"])
    with mock.patch.object(sdm, "declared_input_roots", wraps=sdm.declared_input_roots) as spy:
        blocks._input_data_brief(tmp_path)

    spy.assert_called_once_with(tmp_path)


def test_input_data_disclosure_path_never_blocks() -> None:
    """No-silent-caps sibling of the data-manifest pin: the disclosure path and its
    ambiguity probe never raise/gate (a future gate trips this)."""
    assert_never_blocking(blocks._input_data_brief)
    assert_never_blocking(blocks._input_roots_declaration_unreadable)


# ── S1 wiring ─────────────────────────────────────────────────────────────────


def test_s1_resolved_brief_carries_input_data_nudge(tmp_path: Path) -> None:
    """Wiring: submit_s1's CLEAN-RESOLVE brief carries the input-data nudge when
    the run declares no input roots, beside the deploy_payload / reducibility blocks."""
    (tmp_path / "tasks.py").write_text("x")
    spec = SubmitS1Spec.model_construct(walk=_clean_walk(), run_preflight=False, resolve=object())
    sidecar_path = tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"

    with mock.patch.object(blocks, "resolve_submit_inputs", return_value=_fake_rr(sidecar_path)):
        result = blocks.submit_s1(tmp_path, spec=spec)

    assert result.stage_reached == "resolved"
    idata = result.brief["input_data"]
    assert idata["checked"] is True
    assert idata["issue"] == "no_input_data_declared"


def test_s1_declared_roots_brief_omits_the_key(tmp_path: Path) -> None:
    """Regression pin: a run WITH declared input roots leaves the S1 brief
    byte-unchanged — no ``input_data`` key at all."""
    (tmp_path / "tasks.py").write_text("x")
    _declare(tmp_path, ["data"])
    spec = SubmitS1Spec.model_construct(walk=_clean_walk(), run_preflight=False, resolve=object())
    sidecar_path = tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"

    with mock.patch.object(blocks, "resolve_submit_inputs", return_value=_fake_rr(sidecar_path)):
        result = blocks.submit_s1(tmp_path, spec=spec)

    assert result.stage_reached == "resolved"
    assert "input_data" not in result.brief


def test_s1_malformed_declaration_says_could_not_determine(tmp_path: Path) -> None:
    """Wiring: a wrong-shaped ``input_roots`` declaration → the brief carries the
    honest could-not-determine line, never a silent skip."""
    (tmp_path / "tasks.py").write_text("x")
    _declare(tmp_path, {"path": "data"})  # a dict — ambiguous shape
    spec = SubmitS1Spec.model_construct(walk=_clean_walk(), run_preflight=False, resolve=object())
    sidecar_path = tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"

    with mock.patch.object(blocks, "resolve_submit_inputs", return_value=_fake_rr(sidecar_path)):
        result = blocks.submit_s1(tmp_path, spec=spec)

    idata = result.brief["input_data"]
    assert idata["checked"] is False
    assert "could not be determined" in idata["reason"]
