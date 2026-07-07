"""Unit + seat tests for the notebook graduation gate (notebook-audit T9 / D8).

:func:`hpc_agent.ops.notebook_gate.assert_source_audited` is the ONE definition
wired at TWO synchronous seats (``ops/resolve_submit_inputs`` pre-sidecar,
``ops/submit_flow`` pre-staging). It is opt-in + fail-safe (the
``ops/scope_gate`` posture): with no ``audited_source`` block it is a
byte-identical no-op; opted in, it refuses a submit whose required sections are
not signed at their current hash (unsigned / drifted / linked-source revoked),
naming the offending sections. The seat tests pin that both seats route through
the gate and fire BEFORE any sidecar-write / staging-SSH work.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent.ops.notebook_gate import assert_source_audited
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized
from hpc_agent.state.decision_journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT_ID = "pi-audit-001"

# A two-section percent-format module. The source is drafted FROM the template,
# so (for these tests) source == template and each section's sha is shared.
_MODULE = (
    "# %%\n"
    "# hpc-audit-section: setup\n"
    "import numpy as np\n"
    "\n"
    "# %%\n"
    "# hpc-audit-section: run\n"
    "result = int(np.array([1]).sum())\n"
    "assert result == 1\n"
)


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _write_source(experiment: Path, text: str = _MODULE, *, name: str = "source.py") -> None:
    (experiment / name).write_text(text, encoding="utf-8")


def _write_template(experiment: Path, text: str = _MODULE, *, name: str = "template.py") -> None:
    (experiment / name).write_text(text, encoding="utf-8")


def _write_interview(
    experiment: Path,
    *,
    audited: bool = True,
    audit_id: str = _AUDIT_ID,
    source: str = "source.py",
    template: str = "template.py",
) -> None:
    doc: dict[str, Any] = {"goal": "estimate pi", "task_count": 1}
    if audited:
        doc["audited_source"] = {"source": source, "audit_id": audit_id, "template": template}
    (experiment / "interview.json").write_text(json.dumps(doc), encoding="utf-8")


def _section_sha(slug: str, text: str = _MODULE) -> str:
    parsed = parse_percent_source(text)
    return next(s.section_sha for s in parsed.sections if s.slug == slug)


def _sign(
    experiment: Path,
    slug: str,
    *,
    text: str = _MODULE,
    audit_id: str = _AUDIT_ID,
    linked_sources: list[dict[str, str]] | None = None,
) -> None:
    """Journal a HUMAN sign-off for *slug* at its CURRENT sha (bypasses the T8
    authorship gate, exactly as the scope-gate tests append lock records via the
    low-level ``decision_journal.append_decision``)."""
    sha = _section_sha(slug, text)
    resolved: dict[str, Any] = {
        "audit_id": audit_id,
        "section": slug,
        "section_sha": sha,
        "view_sha": "view-" + sha[:8],
    }
    if linked_sources is not None:
        resolved["linked_sources"] = linked_sources
    append_decision(
        experiment,
        scope_kind="notebook",
        scope_id=audit_id,
        block="notebook-sign-off",
        response="y",
        resolved=resolved,
    )


def _auto_clear(
    experiment: Path, slug: str, *, text: str = _MODULE, audit_id: str = _AUDIT_ID
) -> None:
    """Journal a CODE auto-clear for *slug* at its current sha."""
    sha = _section_sha(slug, text)
    append_decision(
        experiment,
        scope_kind="notebook",
        scope_id=audit_id,
        block="notebook-auto-clear",
        response="auto_cleared",
        resolved={"audit_id": audit_id, "section": slug, "section_sha": sha, "attestor": "code"},
    )


# ── D7 fail-safe silence ─────────────────────────────────────────────────────


def test_no_interview_json_is_silent_noop(experiment: Path) -> None:
    """No interview.json at all → no-op, no raise (and no source .py needed)."""
    assert_source_audited(experiment)  # no raise


def test_interview_without_audited_source_is_silent_noop(experiment: Path) -> None:
    """interview.json present but no audited_source → byte-identical no-op — even
    with no source/template on disk (the gate must not probe when opted out)."""
    _write_interview(experiment, audited=False)
    assert_source_audited(experiment)  # no raise


# ── opted-in PASS paths ──────────────────────────────────────────────────────


def test_all_sections_signed_current_passes(experiment: Path) -> None:
    """Opted in + every required section signed at its current hash → passes."""
    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)
    _sign(experiment, "setup")
    _sign(experiment, "run")
    assert_source_audited(experiment)  # no raise


def test_auto_cleared_section_passes(experiment: Path) -> None:
    """A CODE auto-clear at the current hash passes the gate like a human sign-off."""
    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)
    _auto_clear(experiment, "setup")
    _sign(experiment, "run")
    assert_source_audited(experiment)  # no raise


def test_clean_linked_source_passes(experiment: Path) -> None:
    """A signed section carrying linked_sources that STILL match on disk passes."""
    (experiment / "helper.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    helper_sha = sha256_normalized((experiment / "helper.py").read_text(encoding="utf-8"))
    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)
    _sign(experiment, "setup")
    _sign(
        experiment,
        "run",
        linked_sources=[{"module": "helper", "file": "helper.py", "module_sha": helper_sha}],
    )
    assert_source_audited(experiment)  # no raise


# ── opted-in REFUSAL paths ───────────────────────────────────────────────────


def test_one_unsigned_section_fires_naming_it(experiment: Path) -> None:
    """A required section with no sign-off → SourceUnaudited naming it + status."""
    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)
    _sign(experiment, "setup")  # 'run' left unsigned

    with pytest.raises(errors.SourceUnaudited) as ei:
        assert_source_audited(experiment)

    msg = str(ei.value)
    assert "run" in msg  # names the offending section
    assert "unsigned" in msg
    assert "setup" not in msg  # the signed section is NOT named
    # Reuses the precondition_failed envelope code (no wire-enum widening).
    assert ei.value.error_code == "precondition_failed"
    assert ei.value.retry_safe is False
    assert "append-decision" in (ei.value.remediation or "")


def test_edit_after_sign_fires(experiment: Path) -> None:
    """Sign both, then EDIT the source: the edited section's hash moves, its
    sign-off reads unsigned by construction (drift = unsigned)."""
    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)
    _sign(experiment, "setup")
    _sign(experiment, "run")
    # Edit ONLY the 'run' section's body — its sha moves; 'setup' stays current.
    edited = _MODULE.replace("result = int(np.array([1]).sum())", "result = 1  # edited")
    _write_source(experiment, edited)

    with pytest.raises(errors.SourceUnaudited) as ei:
        assert_source_audited(experiment)
    assert "run" in str(ei.value)


def test_linked_source_drift_fires(experiment: Path) -> None:
    """A signed section whose recorded linked source CHANGED on disk reads unsigned."""
    (experiment / "helper.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    stale_sha = sha256_normalized((experiment / "helper.py").read_text(encoding="utf-8"))
    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)
    _sign(experiment, "setup")
    _sign(
        experiment,
        "run",
        linked_sources=[{"module": "helper", "file": "helper.py", "module_sha": stale_sha}],
    )
    # The dependency drifts AFTER sign-off.
    (experiment / "helper.py").write_text("def f():\n    return 2\n", encoding="utf-8")

    with pytest.raises(errors.SourceUnaudited) as ei:
        assert_source_audited(experiment)
    msg = str(ei.value)
    assert "run" in msg
    assert "linked-source drift" in msg
    assert "helper.py" in msg


def test_linked_source_missing_fires(experiment: Path) -> None:
    """A recorded linked source that no longer exists revokes the section too."""
    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)
    _sign(experiment, "setup")
    _sign(
        experiment,
        "run",
        linked_sources=[{"module": "gone", "file": "gone.py", "module_sha": "0" * 64}],
    )
    with pytest.raises(errors.SourceUnaudited, match="gone.py missing"):
        assert_source_audited(experiment)


# ── opted-in BROKEN-repo loud refusals (SpecInvalid, not a silent pass) ───────


def test_missing_source_file_is_loud(experiment: Path) -> None:
    """Opted in but the source .py is absent → LOUD SpecInvalid naming the path."""
    _write_template(experiment)
    _write_interview(experiment)  # source.py never written
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_source_audited(experiment)
    assert "source.py" in str(ei.value)


def test_missing_template_file_is_loud(experiment: Path) -> None:
    """Opted in but the template .py is absent → LOUD SpecInvalid naming the path."""
    _write_source(experiment)
    _write_interview(experiment)  # template.py never written
    with pytest.raises(errors.SpecInvalid) as ei:
        assert_source_audited(experiment)
    assert "template.py" in str(ei.value)


def test_unparseable_source_is_loud(experiment: Path) -> None:
    """A misplaced marker in the opted-in source is a loud SpecInvalid, not a pass."""
    bad = "# %%\nx = 1\n# hpc-audit-section: setup\n"  # marker not first non-blank line
    _write_source(experiment, bad)
    _write_template(experiment)
    _write_interview(experiment)
    with pytest.raises(errors.SpecInvalid):
        assert_source_audited(experiment)


# ── SEAT: resolve-submit-inputs (pre-sidecar, S1 human boundary) ──────────────

_RESOLVE_SEAM = "hpc_agent.ops.resolve_submit_inputs"


def _resolve_atom_mocks(tmp_path: Path):
    """Mock the four laptop-side atoms so resolve-submit-inputs reaches its
    pre-sidecar gate seat (mirrors tests/ops/test_resolve_submit_inputs.py)."""
    from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
    from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
    from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec

    (tmp_path / ".hpc").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hpc" / "tasks.py").write_text("# stub\n", encoding="utf-8")

    spec = ResolveSubmitInputsSpec(
        run_name="pi",
        submit=BuildSubmitSpecInput(
            profile="pi",
            cluster="h2",
            ssh_target="me@login.h2",
            remote_path="/scratch/me/exp",
            run_id="pi-abcd1234",
            cmd_sha="a" * 64,
            total_tasks=1,
            backend="sge",
        ),
        sidecar=WriteRunSidecarInput(
            run_id="pi-placeholder",
            cmd_sha="0" * 8,
            executor="python -m src.pi",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=1,
        ),
    )
    cr = {
        "run_id": "pi-abcd1234",
        "cmd_sha": "a" * 64,
        "trial_tokens": None,
        "trial_params": [{"x": 1}],
        "total": 1,
    }
    fp = {"found": False, "is_orphan": False, "status": None, "prior_run_id": None, "cluster": None}
    return spec, cr, fp


def test_resolve_seat_gate_fires_before_sidecar(tmp_path: Path) -> None:
    """Ordering proof (S1 seat): an opted-in UNSIGNED source refuses at resolve
    BEFORE the per-run sidecar is written — the write is never reached."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    spec, cr, fp = _resolve_atom_mocks(tmp_path)
    _write_source(tmp_path)
    _write_template(tmp_path)
    _write_interview(tmp_path)  # opted in, nothing signed

    def _no_sidecar(*_a: Any, **_k: Any) -> None:
        raise AssertionError("write_run_sidecar must not run — the gate is pre-sidecar")

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=cr),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=fp),
        mock.patch(f"{_RESOLVE_SEAM}.build_submit_spec", return_value={"run_id": "pi-abcd1234"}),
        mock.patch(f"{_RESOLVE_SEAM}.write_run_sidecar", _no_sidecar),
        pytest.raises(errors.SourceUnaudited, match="run"),
    ):
        resolve_submit_inputs(tmp_path, spec=spec)


def test_resolve_seat_passes_when_signed(tmp_path: Path) -> None:
    """The companion: a fully-signed opted-in source clears the S1 gate and the
    resolved terminal writes the sidecar (the gate passed → the flow proceeds)."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    spec, cr, fp = _resolve_atom_mocks(tmp_path)
    _write_source(tmp_path)
    _write_template(tmp_path)
    _write_interview(tmp_path)
    _sign(tmp_path, "setup")
    _sign(tmp_path, "run")

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=cr),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=fp),
        mock.patch(f"{_RESOLVE_SEAM}.build_submit_spec", return_value={"run_id": "pi-abcd1234"}),
        mock.patch(
            f"{_RESOLVE_SEAM}.write_run_sidecar", return_value={"path": "/x/pi-abcd1234.json"}
        ) as ws,
    ):
        res = resolve_submit_inputs(tmp_path, spec=spec)

    assert res.stage_reached == "resolved"
    ws.assert_called_once()  # gate passed → the sidecar was written


# ── T14: resolve stamps the audited_source echo onto the sidecar spec ─────────


def test_resolve_stamps_audited_source_echo_when_opted_in(tmp_path: Path) -> None:
    """Opted in + fully signed → resolve stamps {source, template, audit_id}
    (rendered_notebook dropped) onto the sidecar spec at step 5."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    spec, cr, fp = _resolve_atom_mocks(tmp_path)
    _write_source(tmp_path)
    _write_template(tmp_path)
    _write_interview(tmp_path)
    _sign(tmp_path, "setup")
    _sign(tmp_path, "run")

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=cr),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=fp),
        mock.patch(f"{_RESOLVE_SEAM}.build_submit_spec", return_value={"run_id": "pi-abcd1234"}),
        mock.patch(
            f"{_RESOLVE_SEAM}.write_run_sidecar", return_value={"path": "/x/pi-abcd1234.json"}
        ) as ws,
    ):
        resolve_submit_inputs(tmp_path, spec=spec)

    stamped = ws.call_args.kwargs["spec"].audited_source
    assert stamped == {"source": "source.py", "template": "template.py", "audit_id": _AUDIT_ID}


def test_resolve_omits_audited_source_when_not_opted_in(tmp_path: Path) -> None:
    """No audited_source block on interview.json → the echo is None (the field is
    omitted on write, byte-identical sidecar — the D7 posture)."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    spec, cr, fp = _resolve_atom_mocks(tmp_path)  # no interview.json written

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=cr),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=fp),
        mock.patch(f"{_RESOLVE_SEAM}.build_submit_spec", return_value={"run_id": "pi-abcd1234"}),
        mock.patch(
            f"{_RESOLVE_SEAM}.write_run_sidecar", return_value={"path": "/x/pi-abcd1234.json"}
        ) as ws,
    ):
        resolve_submit_inputs(tmp_path, spec=spec)

    assert ws.call_args.kwargs["spec"].audited_source is None


# ── SEAT: submit-flow (pre-staging, before any rsync/SSH) ─────────────────────


def _submit_flow_spec(run_id: str = "pi-abcd1234"):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

    return SubmitFlowSpec(
        profile="pi",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/u/scratch/exp",
        job_name="pi",
        run_id=run_id,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        canary=False,
        job_env={"K": "v"},
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hpc_agent.state import run_record

    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


def test_submit_flow_seat_gate_fires_before_staging(
    experiment: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering proof (pre-staging seat): an opted-in UNSIGNED source refuses in
    submit-flow BEFORE any rsync/deploy — the shared prelude is never reached."""
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.ops.submit_flow import submit_flow

    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)  # opted in, nothing signed

    def _no_staging(*_a: Any, **_k: Any) -> None:
        raise AssertionError("_run_shared_prelude must not run — the gate is pre-staging")

    monkeypatch.setattr(sf, "_run_shared_prelude", _no_staging)

    with pytest.raises(errors.SourceUnaudited, match="run"):
        submit_flow(experiment, spec=_submit_flow_spec())


def test_submit_flow_seat_passes_proceeds(
    experiment: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The companion: a signed opted-in source clears the pre-staging gate and the
    flow proceeds to staging (proved by a sentinel raised from _run_shared_prelude)."""
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.ops.submit_flow import submit_flow
    from hpc_agent.state.runs import write_run_sidecar

    _write_source(experiment)
    _write_template(experiment)
    _write_interview(experiment)
    _sign(experiment, "setup")
    _sign(experiment, "run")
    # A real sidecar with a runnable executor so _ensure_run_sidecar no-ops and
    # the flow reaches the staging prelude.
    write_run_sidecar(
        experiment,
        run_id="pi-abcd1234",
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=10,
        tasks_py_sha="",
        remote_path="/u/scratch/exp",
    )

    class _ReachedStaging(RuntimeError):
        pass

    def _sentinel(*_a: Any, **_k: Any) -> None:
        raise _ReachedStaging("reached staging")

    monkeypatch.setattr(sf, "_run_shared_prelude", _sentinel)

    with pytest.raises(_ReachedStaging):
        submit_flow(experiment, spec=_submit_flow_spec())
