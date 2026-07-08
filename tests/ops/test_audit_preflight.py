"""Tests for the ``audit-preflight`` GO/NO-GO composite query (Phase 1b).

Mirrors ``tests/ops/test_submit_preflight.py``'s shape: one class per check,
the GO path, each NO-GO blocker with its pre-drafted remedy line, the
dirty/untracked-template states via a real toy git repo, the version-skew
blocker (the reused ``doctor`` detector monkeypatched), missing/empty roots,
and resuming-vs-fresh. The verb never blocks anything itself — a NO-GO is a
rendered prediction, so every assertion is over the returned brief + fields.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hpc_agent._wire.queries.audit_preflight import AuditPreflightSpec
from hpc_agent.ops import audit_preflight as ap
from hpc_agent.state import notebook_audit

# A minimal, valid percent-format template with one section.
_TEMPLATE_SRC = "# %%\n# hpc-audit-section: intro\nx = 1\n"


def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in *cwd*, failing loudly (test setup, not fail-open)."""
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    """git init + local identity so commits succeed in a hermetic tmp repo."""
    _git(["init"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)


def _committed_template_repo(tmp_path: Path) -> tuple[Path, str]:
    """A git repo with a COMMITTED-CLEAN template. Returns (experiment_dir, relpath)."""
    _init_repo(tmp_path)
    rel = "template.py"
    (tmp_path / rel).write_text(_TEMPLATE_SRC, encoding="utf-8")
    _git(["add", rel], tmp_path)
    _git(["commit", "-m", "add template"], tmp_path)
    return tmp_path, rel


def _spec(template: str, **kw: object) -> AuditPreflightSpec:
    return AuditPreflightSpec(template=template, **kw)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_skew(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to NO version skew (the reused doctor detector).

    A tmp repo is never the hpc-agent source repo, so the real detector already
    returns None — but pinning it keeps the tests independent of the runner's
    checkout state. The skew-blocker test overrides this.
    """
    monkeypatch.setattr(ap._doctor, "_detect_version_skew", lambda experiment_dir: None)


class TestGoPath:
    """A committed-clean template, no roots, fresh audit → GO."""

    def test_clean_template_go(self, tmp_path: Path) -> None:
        exp, rel = _committed_template_repo(tmp_path)
        result = ap.audit_preflight(experiment_dir=exp, spec=_spec(rel))
        assert result.verdict == "GO"
        assert result.template_state == "clean"
        assert result.blockers == []
        assert result.brief.startswith("# audit-preflight — GO")
        assert result.resuming is False
        # The Phase-1a manifest seam always rides as a disclosure, never a blocker.
        assert any("data-manifest drift" in d for d in result.disclosures)


class TestTemplateCheck:
    """Check 1 — present / parses / git-committed-clean, each NO-GO with remedy."""

    def test_missing_template_no_go(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        result = ap.audit_preflight(experiment_dir=tmp_path, spec=_spec("nope.py"))
        assert result.verdict == "NO-GO"
        assert result.template_state == "missing"
        (b,) = [b for b in result.blockers if b.check == "template"]
        assert "not found" in b.blocker
        assert "commit" in b.remedy.lower()

    def test_unparseable_template_no_go(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        rel = "bad.py"
        # A col-0 marker that is NOT its cell's first non-blank line → SpecInvalid.
        (tmp_path / rel).write_text("# %%\nx = 1\n# hpc-audit-section: intro\n", encoding="utf-8")
        result = ap.audit_preflight(experiment_dir=tmp_path, spec=_spec(rel))
        assert result.verdict == "NO-GO"
        assert result.template_state == "unparseable"
        (b,) = [b for b in result.blockers if b.check == "template"]
        assert "does not parse" in b.blocker

    def test_dirty_template_unsigned_no_go(self, tmp_path: Path) -> None:
        exp, rel = _committed_template_repo(tmp_path)
        # Modify after commit → tracked-but-dirty.
        (exp / rel).write_text(_TEMPLATE_SRC + "\ny = 2\n", encoding="utf-8")
        result = ap.audit_preflight(experiment_dir=exp, spec=_spec(rel))
        assert result.verdict == "NO-GO"
        assert result.template_state == "dirty"
        (b,) = [b for b in result.blockers if b.check == "template"]
        assert "unsigned template" in b.blocker
        assert b.remedy == ap._UNSIGNED_REMEDY

    def test_untracked_template_unsigned_no_go(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        rel = "template.py"
        (tmp_path / rel).write_text(_TEMPLATE_SRC, encoding="utf-8")  # never `git add`
        result = ap.audit_preflight(experiment_dir=tmp_path, spec=_spec(rel))
        assert result.verdict == "NO-GO"
        assert result.template_state == "untracked"
        (b,) = [b for b in result.blockers if b.check == "template"]
        assert "unsigned template" in b.blocker

    def test_no_git_repo_unsigned_no_go(self, tmp_path: Path) -> None:
        # No `git init`: the commit-signature cannot be verified.
        rel = "template.py"
        (tmp_path / rel).write_text(_TEMPLATE_SRC, encoding="utf-8")
        result = ap.audit_preflight(experiment_dir=tmp_path, spec=_spec(rel))
        assert result.verdict == "NO-GO"
        assert result.template_state == "no_git"
        (b,) = [b for b in result.blockers if b.check == "template"]
        assert "no git repo" in b.blocker


class TestVersionSkewCheck:
    """Check 2 — reuses doctor's detector; a resolved skew is a blocker."""

    def test_skew_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        exp, rel = _committed_template_repo(tmp_path)

        class _Skew:
            warning = "version_skew: CLI is stale vs the repo tip"

        monkeypatch.setattr(ap._doctor, "_detect_version_skew", lambda experiment_dir: _Skew())
        result = ap.audit_preflight(experiment_dir=exp, spec=_spec(rel))
        assert result.verdict == "NO-GO"
        (b,) = [b for b in result.blockers if b.check == "version_skew"]
        assert "stale" in b.blocker
        assert "reinstall" in b.remedy.lower()


class TestRootsCheck:
    """Check 3 — declared roots exist and are non-empty."""

    def test_missing_root_no_go(self, tmp_path: Path) -> None:
        exp, rel = _committed_template_repo(tmp_path)
        result = ap.audit_preflight(
            experiment_dir=exp, spec=_spec(rel, source_roots=["nonexistent"])
        )
        assert result.verdict == "NO-GO"
        (b,) = [b for b in result.blockers if b.check == "roots"]
        assert "does not exist" in b.blocker
        assert "correct the declared source_roots" in b.remedy

    def test_empty_root_no_go(self, tmp_path: Path) -> None:
        exp, rel = _committed_template_repo(tmp_path)
        (exp / "emptyroot").mkdir()
        result = ap.audit_preflight(experiment_dir=exp, spec=_spec(rel, input_roots=["emptyroot"]))
        assert result.verdict == "NO-GO"
        (b,) = [b for b in result.blockers if b.check == "roots"]
        assert "is empty" in b.blocker

    def test_populated_root_passes(self, tmp_path: Path) -> None:
        exp, rel = _committed_template_repo(tmp_path)
        root = exp / "src"
        root.mkdir()
        (root / "mod.py").write_text("x = 1\n", encoding="utf-8")
        result = ap.audit_preflight(experiment_dir=exp, spec=_spec(rel, source_roots=["src"]))
        assert result.verdict == "GO"
        assert result.source_roots == ["src"]


class TestPriorAuditState:
    """Check 4 — resuming vs fresh, from the audit journal."""

    def test_fresh_when_no_journal(self, tmp_path: Path) -> None:
        exp, rel = _committed_template_repo(tmp_path)
        result = ap.audit_preflight(experiment_dir=exp, spec=_spec(rel, audit_id="fresh-audit"))
        assert result.resuming is False
        assert result.journal_records == 0

    def test_resuming_when_journal_exists(self, tmp_path: Path) -> None:
        exp, rel = _committed_template_repo(tmp_path)
        # A recorded config makes the audit journal non-empty AND defaults roots.
        notebook_audit.record_audit_config(
            exp, audit_id="live-audit", input_roots=[], source_roots=[]
        )
        result = ap.audit_preflight(experiment_dir=exp, spec=_spec(rel, audit_id="live-audit"))
        assert result.resuming is True
        assert result.journal_records >= 1
        assert result.verdict == "GO"  # journal presence is informational, never a blocker
        assert any("resuming audit" in d for d in result.disclosures)


class TestRootsDefaultFromRecordedConfig:
    """Roots default from the audit's recorded configuration (one-declaration)."""

    def test_recorded_roots_used_when_spec_omits(self, tmp_path: Path) -> None:
        exp, rel = _committed_template_repo(tmp_path)
        (exp / "declared").mkdir()  # empty → a NO-GO if the recorded root is read
        notebook_audit.record_audit_config(
            exp, audit_id="cfg-audit", input_roots=["declared"], source_roots=[]
        )
        # Spec omits input_roots → defaults to the recorded ["declared"].
        result = ap.audit_preflight(experiment_dir=exp, spec=_spec(rel, audit_id="cfg-audit"))
        assert result.input_roots == ["declared"]
        (b,) = [b for b in result.blockers if b.check == "roots"]
        assert "declared" in b.blocker
