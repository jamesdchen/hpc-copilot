"""Hermetic tests for the audit net (notebook-audit 6a) — the transitive
import closure with per-module tiers.

``ops/notebook/linked_sources.py::resolve_audit_net`` BFS-es the full import
cone seeded from a module's direct imports (where ``resolve_linked_sources``
stopped at the first hop), classifying every module into one of four tiers:
INHERITED (template-identical OR ledger-attested), EXTERNAL (stdlib /
site-packages), UNRESOLVED (resolves nowhere — a real finding, never silent),
NEW_DRIFTED (resolved under a source root but neither inherited leg). ``lint.py``
runs the net over the declared ``source_roots`` and emits a section-attributed
``audit_net_unresolved`` finding (flipping the zero-flags tier leg to
human_required) plus a disclosed cap marker.

Every fixture is a tmp_path module tree; the resolver only ever ``ast.parse``s a
module under test — it NEVER imports/execs one (the 6a never-exec boundary). No
third-party dependency: EXTERNAL is exercised via the stdlib (``os``) and an
installed module (``pydantic`` — a hard runtime dep of hpc-agent, so present in
every test env; numpy is NOT — CI runners lack it, only local quant envs have
it). pandas is deliberately NOT imported (absent here).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from hpc_agent._wire.actions.notebook_lint import (
    LinkedSource,
    NotebookLintFinding,
    NotebookLintInput,
    NotebookLintResult,
)
from hpc_agent._wire.queries.notebook_status import AuditNetSummary, NotebookStatusResult
from hpc_agent.ops.notebook.audit_view import AUTO_CLEARED, HUMAN_REQUIRED, build_audit_view
from hpc_agent.ops.notebook.linked_sources import (
    _CLOSURE_MAX_MODULES,
    AuditNetTier,
    _is_installed_module,
    resolve_audit_net,
)
from hpc_agent.ops.notebook.lint import notebook_lint
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized

# ── BFS correctness ──────────────────────────────────────────────────────────


def test_bfs_diamond_collapses_to_one_entry(tmp_path: Path) -> None:
    # a imports b and c; both import d → d is characterized ONCE (visited set is
    # keyed on the resolved Path), and the first discovery (via b, sorted before
    # c) wins its via chain.
    (tmp_path / "a.py").write_text("import b\nimport c\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import d\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("import d\n", encoding="utf-8")
    (tmp_path / "d.py").write_text("X = 1\n", encoding="utf-8")
    entries, cap_hit = resolve_audit_net(["a"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["a", "b", "c", "d"]  # sorted, deduped
    assert sum(1 for e in entries if e.module == "d") == 1
    assert {e.module: e.via for e in entries}["d"] == ("a", "b", "d")
    assert cap_hit is False


def test_bfs_cycle_terminates(tmp_path: Path) -> None:
    # a <-> b: the resolved-Path visited set breaks the cycle.
    (tmp_path / "a.py").write_text("import b\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")
    entries, cap_hit = resolve_audit_net(["a"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["a", "b"]
    assert cap_hit is False


def test_bfs_self_import_terminates(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import a\n", encoding="utf-8")
    entries, cap_hit = resolve_audit_net(["a"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["a"]
    assert cap_hit is False


def test_symbol_in_resolved_parent_is_covered_not_unresolved(tmp_path: Path) -> None:
    # `from engine import train` offers BOTH `engine` and `engine.train`; the
    # dotted name is a SYMBOL inside the resolved parent, not a module file of
    # its own — the parent's entry covers it, so the net carries engine ONCE
    # and NO UNRESOLVED engine.train (run 2026-07-19: the wave3 fixtures broke
    # on a spurious engine.train UNRESOLVED charge in module attention).
    (tmp_path / "engine.py").write_text("def train(x):\n    return x\n", encoding="utf-8")
    entries, cap_hit = resolve_audit_net(["engine", "engine.train"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["engine"]
    assert all(e.tier is not AuditNetTier.UNRESOLVED for e in entries)
    assert cap_hit is False


def test_symbol_missing_from_parent_stays_unresolved(tmp_path: Path) -> None:
    # The dual: a dotted name the resolved parent does NOT define is an import
    # that fails at runtime — honestly UNRESOLVED, never papered over.
    (tmp_path / "engine.py").write_text("def train(x):\n    return x\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["engine.no_such_symbol"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["engine.no_such_symbol"]
    assert entries[0].tier is AuditNetTier.UNRESOLVED


def test_symbol_in_parent_assignment_is_covered(tmp_path: Path) -> None:
    # A module-level ASSIGNMENT binds the name at runtime exactly like a def:
    # engine.py = `CONSTANT = 42` covers `from engine import CONSTANT` — never
    # a spurious UNRESOLVED (defect: def/class-only coverage flipped sections
    # to human_required on plain constants).
    (tmp_path / "engine.py").write_text("CONSTANT = 42\n", encoding="utf-8")
    entries, cap_hit = resolve_audit_net(["engine", "engine.CONSTANT"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["engine"]
    assert all(e.tier is not AuditNetTier.UNRESOLVED for e in entries)
    assert cap_hit is False


def test_symbol_in_parent_dunder_assignment_is_covered(tmp_path: Path) -> None:
    # The same for a dunder constant: `__version__ = "1.0"` covers
    # `from engine import __version__`.
    (tmp_path / "engine.py").write_text('__version__ = "1.0"\n', encoding="utf-8")
    entries, _ = resolve_audit_net(["engine", "engine.__version__"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["engine"]
    assert all(e.tier is not AuditNetTier.UNRESOLVED for e in entries)


def test_symbol_in_parent_reexport_from_import_is_covered(tmp_path: Path) -> None:
    # A RE-EXPORT binds the name in the parent: engine.py = `from other import
    # train` makes `from engine import train` importable at runtime — covered,
    # and the net still walks through to the real origin (`other`).
    (tmp_path / "engine.py").write_text("from other import train\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("def train(x):\n    return x\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["engine", "engine.train"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["engine", "other"]
    assert all(e.tier is not AuditNetTier.UNRESOLVED for e in entries)


def test_symbol_in_parent_reexport_plain_import_is_covered(tmp_path: Path) -> None:
    # engine.py = `import other` binds `other` at module scope, so
    # `from engine import other` is importable at runtime — covered.
    (tmp_path / "engine.py").write_text("import other\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("X = 1\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["engine", "engine.other"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["engine", "other"]
    assert all(e.tier is not AuditNetTier.UNRESOLVED for e in entries)


def test_symbol_in_parent_annassign_with_value_is_covered(tmp_path: Path) -> None:
    # An annotated assignment WITH a value binds at runtime exactly like a
    # plain assignment: `LIMIT: int = 10` covers `from engine import LIMIT`.
    (tmp_path / "engine.py").write_text("LIMIT: int = 10\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["engine", "engine.LIMIT"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["engine"]
    assert all(e.tier is not AuditNetTier.UNRESOLVED for e in entries)


def test_symbol_in_parent_bare_annotation_stays_unresolved(tmp_path: Path) -> None:
    # The unsafe-direction dual (verifier finding, 2026-07-19): a BARE
    # annotation `y: int` binds NOTHING at runtime — `from engine import y`
    # raises ImportError — so it must stay UNRESOLVED, never masked as covered.
    (tmp_path / "engine.py").write_text("y: int\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["engine.y"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["engine.y"]
    assert entries[0].tier is AuditNetTier.UNRESOLVED


def test_symbol_unbound_despite_new_binding_forms_stays_unresolved(tmp_path: Path) -> None:
    # The honest negative over the broadened forms: names the parent binds via
    # assignment / re-export do NOT cover a different, unbound name — an import
    # that would fail at runtime stays UNRESOLVED.
    (tmp_path / "engine.py").write_text(
        "CONSTANT = 42\nfrom other import train\n", encoding="utf-8"
    )
    (tmp_path / "other.py").write_text("def train(x):\n    return x\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["engine", "engine.no_such"], tmp_path, [tmp_path])
    by_module = {e.module: e for e in entries}
    assert by_module["engine.no_such"].tier is AuditNetTier.UNRESOLVED


# ── tier classification per branch ───────────────────────────────────────────


def test_tier_classification_per_branch(tmp_path: Path) -> None:
    (tmp_path / "eng.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "signed_mod.py").write_text("Y = 2\n", encoding="utf-8")
    (tmp_path / "newmod.py").write_text("Z = 3\n", encoding="utf-8")
    signed_sha = sha256_normalized("Y = 2\n")
    entries, cap_hit = resolve_audit_net(
        ["eng", "signed_mod", "newmod", "os", "pydantic", "zz_missing_xyz"],
        tmp_path,
        [tmp_path],
        template_modules={"eng"},
        sha_is_signed=lambda sha: sha == signed_sha,
    )
    by_module = {e.module: e for e in entries}
    assert by_module["eng"].tier is AuditNetTier.INHERITED  # template-identical
    assert by_module["signed_mod"].tier is AuditNetTier.INHERITED  # ledger-attested
    assert by_module["newmod"].tier is AuditNetTier.NEW_DRIFTED  # resolved, neither leg
    assert by_module["os"].tier is AuditNetTier.EXTERNAL  # stdlib
    assert by_module["pydantic"].tier is AuditNetTier.EXTERNAL  # installed site-packages
    assert by_module["zz_missing_xyz"].tier is AuditNetTier.UNRESOLVED  # nowhere
    assert cap_hit is False
    # Resolved-under-roots modules carry a file + sha; external/unresolved do not.
    assert by_module["eng"].file is not None
    assert by_module["eng"].module_sha == sha256_normalized("X = 1\n")
    for name in ("os", "pydantic", "zz_missing_xyz"):
        assert by_module[name].file is None
        assert by_module[name].module_sha is None


def test_namespace_prefix_is_not_unresolved(tmp_path: Path) -> None:
    # `from lib import helper` offers BOTH `lib` and `lib.helper`; when
    # `lib.helper` resolves under a root, `lib` is its namespace prefix — filtered
    # out of UNRESOLVED rather than flagged.
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "helper.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["lib", "lib.helper"], tmp_path, [tmp_path])
    modules = {e.module for e in entries}
    assert "lib.helper" in modules
    assert "lib" not in modules
    assert all(e.tier is not AuditNetTier.UNRESOLVED for e in entries)


# ── exec-free, crash-free installed-module classification ────────────────────


def _fake_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: str, init: str) -> Path:
    """Install a fake ``package`` (with *init* as its ``__init__.py``) on sys.path."""
    pkg = tmp_path / "site" / package
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    roots = tmp_path / "roots"
    roots.mkdir(exist_ok=True)
    return roots


def test_installed_pkg_submodule_external_without_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fake installed package with a submodule classifies EXTERNAL — and its
    # __init__.py is NEVER executed (the 6a never-exec boundary): the sentinel
    # the __init__ would write on exec must NOT exist after the call. (Defect:
    # find_spec on the DOTTED name imported the parent package.)
    roots = _fake_site(
        tmp_path,
        monkeypatch,
        "execpkg",
        "from pathlib import Path\nPath(__file__).with_name('SENTINEL').write_text('x')\n",
    )
    (tmp_path / "site" / "execpkg" / "sub.py").write_text("X = 1\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["execpkg", "execpkg.sub"], tmp_path, [roots])
    by_module = {e.module: e for e in entries}
    assert by_module["execpkg"].tier is AuditNetTier.EXTERNAL
    assert by_module["execpkg.sub"].tier is AuditNetTier.EXTERNAL
    assert not (tmp_path / "site" / "execpkg" / "SENTINEL").exists()


def test_installed_pkg_raising_init_classifies_unresolved_no_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # boompkg's __init__ raises RuntimeError — the dotted classification
    # COMPLETES (no raise) and the name honestly reads UNRESOLVED: without
    # exec (never performed) the env authority cannot prove ownership of a
    # submodule that exists only behind the raising __init__.
    roots = _fake_site(tmp_path, monkeypatch, "boompkg", "raise RuntimeError('boom')\n")
    entries, _ = resolve_audit_net(["boompkg.sub"], tmp_path, [roots])
    assert [e.module for e in entries] == ["boompkg.sub"]
    assert entries[0].tier is AuditNetTier.UNRESOLVED


def test_lint_survives_raising_parent_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The original crash repro end-to-end: the source does `import boompkg.sub`
    # and the parent __init__ raises RuntimeError — notebook_lint NEVER raises;
    # it reports the honest section-attributed UNRESOLVED finding.
    _fake_site(tmp_path, monkeypatch, "boompkg", "raise RuntimeError('boom')\n")
    source = "# %%\n# hpc-audit-section: load\nimport boompkg.sub\n"
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(source, encoding="utf-8")
    spec = NotebookLintInput(source="source.py", template="template.py", source_roots=["."])
    result = notebook_lint(experiment_dir=tmp_path, spec=spec)
    unresolved = [
        f
        for f in result.findings
        if f.rule == "audit_net_unresolved" and f.evidence.get("kind") == "unresolved"
    ]
    assert [f.evidence["module"] for f in unresolved] == ["boompkg.sub"]
    assert unresolved[0].section == "load"


def test_find_spec_exception_means_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A find_spec raising ANYTHING (here ValueError: a sys.modules entry whose
    # __spec__ is None) reads as not-installed — the honest UNRESOLVED, never a
    # crash (the broad-except ruling, the boompkg lesson generalized).
    bogus = types.ModuleType("zz_bogus_spec")
    bogus.__spec__ = None
    monkeypatch.setitem(sys.modules, "zz_bogus_spec", bogus)
    entries, _ = resolve_audit_net(["zz_bogus_spec.sub"], tmp_path, [tmp_path])
    assert [e.module for e in entries] == ["zz_bogus_spec.sub"]
    assert entries[0].tier is AuditNetTier.UNRESOLVED


def test_installed_classification_unchanged_for_stdlib_and_sitepackages(tmp_path: Path) -> None:
    # The pre-fix EXTERNAL posture is unchanged: stdlib (incl. a dotted stdlib
    # name) and an installed top-level package classify EXTERNAL; a nowhere
    # name does not. Direct pins on the helper for the top-level fast paths.
    entries, _ = resolve_audit_net(["os.path", "pydantic", "zz_missing_xyz"], tmp_path, [tmp_path])
    by_module = {e.module: e for e in entries}
    assert by_module["os.path"].tier is AuditNetTier.EXTERNAL  # stdlib leg (dotted)
    assert by_module["pydantic"].tier is AuditNetTier.EXTERNAL  # installed leg
    assert by_module["zz_missing_xyz"].tier is AuditNetTier.UNRESOLVED
    assert _is_installed_module("pydantic") is True
    assert _is_installed_module("zz_missing_xyz") is False
    assert _is_installed_module("zz_missing_xyz.sub") is False


# ── via-chain shape ──────────────────────────────────────────────────────────


def test_via_chain_shape(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import b\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import c\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("X = 1\n", encoding="utf-8")
    entries, _ = resolve_audit_net(["a"], tmp_path, [tmp_path])
    by_module = {e.module: e for e in entries}
    assert by_module["a"].via == ("a",)
    assert by_module["b"].via == ("a", "b")
    assert by_module["c"].via == ("a", "b", "c")
    for entry in entries:
        assert entry.via[-1] == entry.module  # chain ends at the module
        assert entry.via[0] == "a"  # chain starts at a seed


# ── determinism ──────────────────────────────────────────────────────────────


def _serialize(entries: list) -> list[tuple]:
    return [(e.module, e.file, e.module_sha, e.tier.value, e.via) for e in entries]


def test_determinism_two_runs_byte_identical(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import b\nimport c\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import d\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("import d\n", encoding="utf-8")
    (tmp_path / "d.py").write_text("X = 1\n", encoding="utf-8")
    first, cap1 = resolve_audit_net(["a"], tmp_path, [tmp_path])
    second, cap2 = resolve_audit_net(["a"], tmp_path, [tmp_path])
    assert first == second  # frozen-dataclass equality
    assert _serialize(first) == _serialize(second)  # byte-identical projection
    assert cap1 == cap2 is False


# ── closure cap ──────────────────────────────────────────────────────────────


def test_closure_cap_constant() -> None:
    assert _CLOSURE_MAX_MODULES == 256


def test_cap_overflow_unit(tmp_path: Path) -> None:
    # A 6-link chain with max_modules=3 stops at exactly 3 and discloses the cap.
    for i in range(6):
        body = f"import m{i + 1}\n" if i < 5 else "X = 1\n"
        (tmp_path / f"m{i}.py").write_text(body, encoding="utf-8")
    entries, cap_hit = resolve_audit_net(["m0"], tmp_path, [tmp_path], max_modules=3)
    assert cap_hit is True
    assert len(entries) == 3
    assert [e.module for e in entries] == ["m0", "m1", "m2"]


def test_lint_cap_marker_finding(tmp_path: Path) -> None:
    # A hub importing _CLOSURE_MAX_MODULES+1 leaves overflows the closure; the
    # lint emits ONE disclosed module-scoped cap marker (count + cap), never a
    # silent truncation.
    n = _CLOSURE_MAX_MODULES + 1
    (tmp_path / "hub.py").write_text(
        "\n".join(f"import leaf_{i}" for i in range(n)) + "\n", encoding="utf-8"
    )
    for i in range(n):
        (tmp_path / f"leaf_{i}.py").write_text("X = 1\n", encoding="utf-8")
    source = "# %%\n# hpc-audit-section: load\nimport hub\n"
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(source, encoding="utf-8")
    spec = NotebookLintInput(source="source.py", template="template.py", source_roots=["."])
    result = notebook_lint(experiment_dir=tmp_path, spec=spec)
    cap_findings = [
        f
        for f in result.findings
        if f.rule == "audit_net_unresolved" and f.evidence.get("kind") == "cap_hit"
    ]
    assert len(cap_findings) == 1
    assert cap_findings[0].section is None  # module-scoped disclosure
    assert cap_findings[0].evidence["cap"] == _CLOSURE_MAX_MODULES
    assert cap_findings[0].evidence["module_count"] == _CLOSURE_MAX_MODULES


# ── UNRESOLVED flips the status leg ──────────────────────────────────────────


def test_unresolved_finding_flips_status_leg(tmp_path: Path) -> None:
    source = "# %%\n# hpc-audit-section: load\nimport zz_missing_dep\n"
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(source, encoding="utf-8")
    spec = NotebookLintInput(source="source.py", template="template.py", source_roots=["."])
    result = notebook_lint(experiment_dir=tmp_path, spec=spec)
    unresolved = [
        f
        for f in result.findings
        if f.rule == "audit_net_unresolved" and f.evidence.get("kind") == "unresolved"
    ]
    assert len(unresolved) == 1
    assert unresolved[0].section == "load"  # section-attributed
    assert unresolved[0].evidence["module"] == "zz_missing_dep"

    src_mod = parse_percent_source(source)
    tmpl_mod = parse_percent_source(source)
    # WITH the finding, the otherwise-inherited section is human_required.
    flagged = build_audit_view(src_mod, tmpl_mod, [f.model_dump() for f in result.findings])
    assert {s.slug: s.tier for s in flagged.sections}["load"] == HUMAN_REQUIRED
    # CONTROL: no findings → the same inherited section auto-clears (the flip is
    # the finding, nothing else).
    clean = build_audit_view(src_mod, tmpl_mod, [])
    assert {s.slug: s.tier for s in clean.sections}["load"] == AUTO_CLEARED


def test_unresolved_needs_declared_source_roots(tmp_path: Path) -> None:
    # With NO declared source_roots there is no cone to be "unresolved" against —
    # the net is vacuous (rule 3's never-a-finding posture), so the inherited
    # section stays clean even with an otherwise-unresolvable import.
    source = "# %%\n# hpc-audit-section: load\nimport zz_missing_dep\n"
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(source, encoding="utf-8")
    spec = NotebookLintInput(source="source.py", template="template.py")
    result = notebook_lint(experiment_dir=tmp_path, spec=spec)
    assert [f for f in result.findings if f.rule == "audit_net_unresolved"] == []


# ── lint annotates linked_sources with tier + via ────────────────────────────


def test_lint_annotates_linked_sources_with_tier_and_via(tmp_path: Path) -> None:
    (tmp_path / "engine.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    source = "# %%\n# hpc-audit-section: load\nimport engine\n"
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    (tmp_path / "template.py").write_text(source, encoding="utf-8")  # template imports it too
    spec = NotebookLintInput(source="source.py", template="template.py", source_roots=["."])
    result = notebook_lint(experiment_dir=tmp_path, spec=spec)
    assert len(result.linked_sources) == 1
    link = result.linked_sources[0]
    assert link.module == "engine"
    assert link.tier == "inherited"  # template-identical
    assert link.via == ["engine"]
    # No unresolved findings — the one import resolves under the source root.
    assert [f for f in result.findings if f.rule == "audit_net_unresolved"] == []


# ── wire roundtrip of the new fields ─────────────────────────────────────────


def test_wire_roundtrip_new_fields() -> None:
    sha = "s" * 64
    # LinkedSource.tier / .via round-trip, with additive-optional defaults.
    link = LinkedSource(module="m", file="m.py", module_sha=sha, tier="new_drifted", via=["a", "m"])
    assert LinkedSource.model_validate(link.model_dump()) == link
    bare = LinkedSource(module="m", file="m.py", module_sha=sha)
    assert bare.tier == ""
    assert bare.via == []
    # The audit_net_unresolved finding + annotated link round-trip on the result.
    finding = NotebookLintFinding(
        rule="audit_net_unresolved",
        section="load",
        detail="unresolved",
        evidence={"module": "x", "via": ["x"], "kind": "unresolved"},
    )
    lint_result = NotebookLintResult(findings=[finding], linked_sources=[link])
    assert NotebookLintResult.model_validate(lint_result.model_dump()) == lint_result
    # AuditNetSummary + NotebookStatusResult.audit_net_summary round-trip.
    summary = AuditNetSummary(inherited=2, external=3, unresolved=1, new_drifted=4, cap_hit=True)
    status = NotebookStatusResult(audit_id="a1", passed=False, audit_net_summary=summary)
    assert NotebookStatusResult.model_validate(status.model_dump()) == status
    assert NotebookStatusResult(audit_id="a1", passed=True).audit_net_summary is None
