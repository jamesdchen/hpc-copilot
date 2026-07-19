"""Audit-net presentation (notebook-audit 6a) — the human-facing surface.

Builder-A's machinery (``ops/notebook/linked_sources.py``: ``AuditNetTier`` /
``AuditNetEntry`` / ``resolve_audit_net``) has NOT landed in this worktree, so
these tests run against LOCAL DOUBLES that mirror the pinned interface verbatim
(the enum member NAMES, the frozen ``{module, file, module_sha, tier, via}``
entry, the sorted BFS emission). At merge the real names resolve and the same
assertions bind the real machinery.

Pins, per the 6a rulings:

* UNRESOLVED entries are a REAL finding (``audit_net_unresolved``) — visually
  prominent, grouped FIRST within the net block (each group keeps the
  machinery's sorted order — a stable partition, never a re-sort);
* EXTERNAL renders as env_hash-bound (the gate discloses the env_hash; the
  render only names the binding);
* the net is presentation-only — it moves no ``view_sha`` and is byte-ABSENT
  when absent (every pre-6a render is unchanged);
* digest mode carries COUNTS only, full mode carries the block;
* UNRESOLVED net entries participate in the module-attention ordering (after
  the unsigned-module charges, in net order);
* ``notebook-status`` surfaces the additive ``audit_net_summary`` (counts by
  tier + cap flag), fail-open on the cross-lane seam.

Hermetic: ``tmp_path`` only; the audited modules are never executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from hpc_agent._wire.queries.notebook_status import (
    AuditNetSummary,
    NotebookStatusResult,
    NotebookStatusSpec,
)
from hpc_agent.ops.notebook import status_op
from hpc_agent.ops.notebook.audit_view import (
    AUDIT_NET_CAP,
    UNRESOLVED_TIER_LABEL,
    build_audit_view,
    net_tier_label,
    render_markdown,
    render_summary_markdown,
)
from hpc_agent.ops.notebook.module_attention import build_module_attention
from hpc_agent.state.audit_source import parse_percent_source

_TEMPLATE = "# %%\n# hpc-audit-section: model\nX = 0\n"
_SOURCE = "# %%\n# hpc-audit-section: model\nfrom engine import train\nr = train(3)\n"
_AUDIT = "audit-net"


# ── LOCAL DOUBLES — the pinned interface, mirrored in fixtures only ──────────


class AuditNetTier(Enum):
    """Double of builder A's ``AuditNetTier`` (pinned member NAMES)."""

    INHERITED = "inherited"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"
    NEW_DRIFTED = "new_drifted"


@dataclass(frozen=True)
class AuditNetEntry:
    """Double of builder A's frozen ``AuditNetEntry`` (pinned fields)."""

    module: str
    file: str | None
    module_sha: str | None
    tier: AuditNetTier
    via: tuple[str, ...] = ()


def _entry(
    module: str,
    tier: AuditNetTier,
    *,
    file: str | None = None,
    sha: str | None = "0123456789abcdef",
    via: tuple[str, ...] = (),
) -> AuditNetEntry:
    return AuditNetEntry(module=module, file=file, module_sha=sha, tier=tier, via=via)


#: A mixed net in the machinery's deterministic sorted emission: two UNRESOLVED
#: interleaved with resolved tiers, exercising the stable partition.
_MIXED = (
    _entry("src.engine", AuditNetTier.INHERITED, file="src/engine.py"),
    _entry("missing.mod", AuditNetTier.UNRESOLVED, file=None, sha=None, via=("source", "src.a")),
    _entry("numpy", AuditNetTier.EXTERNAL, file=None),
    _entry("other.bad", AuditNetTier.UNRESOLVED, file=None, sha=None, via=("source",)),
    _entry("src.new", AuditNetTier.NEW_DRIFTED, file="src/new.py", via=("src.engine",)),
)


def _view(*, audit_net: tuple[Any, ...] = (), audit_net_cap_hit: bool = False) -> object:
    src = parse_percent_source(_SOURCE)
    tmpl = parse_percent_source(_TEMPLATE)
    return build_audit_view(src, tmpl, [], audit_net=audit_net, audit_net_cap_hit=audit_net_cap_hit)


# ── audit_view: the net block (full render) ──────────────────────────────────


def test_net_block_byte_absent_without_net() -> None:
    # No net → no block, and the render is byte-identical to an explicit
    # empty-net build (the byte-absent pin).
    body = render_markdown(_view())  # type: ignore[arg-type]
    assert "## audit net" not in body
    assert "audit_net_unresolved" not in body
    assert body == render_markdown(_view(audit_net=()))  # type: ignore[arg-type]


def test_net_block_unresolved_grouped_first() -> None:
    body = render_markdown(_view(audit_net=_MIXED))  # type: ignore[arg-type]
    assert "## audit net" in body
    lines = body.splitlines()
    net_lines = [ln for ln in lines if ln.startswith("- [")]
    assert len(net_lines) == 5
    # UNRESOLVED entries grouped FIRST, each group keeps the machinery's order.
    assert [ln for ln in net_lines if "[UNRESOLVED]" in ln] == net_lines[:2]
    assert "missing.mod" in net_lines[0]
    assert "other.bad" in net_lines[1]
    # The resolved tiers keep the machinery's sorted emission order.
    assert "src/engine.py" in net_lines[2]
    assert "numpy" in net_lines[3]
    assert "src/new.py" in net_lines[4]


def test_net_block_badges_via_and_findings() -> None:
    body = render_markdown(_view(audit_net=_MIXED))  # type: ignore[arg-type]
    # The prominent finding line names the UNRESOLVED count + human_required.
    assert (
        "- ⚠ audit_net_unresolved: 2 module(s) resolve to NO file under the "
        "recorded source_roots — the audit is human_required" in body
    )
    # The via chain renders compactly; the finding names the missing module.
    assert "- [UNRESOLVED] missing.mod — no file under source_roots (via source -> src.a)" in body
    # EXTERNAL renders as env_hash-bound (the gate discloses the hash itself).
    assert "- [EXTERNAL] numpy (env_hash-bound, module_sha 0123456789ab)" in body
    # Resolved entries carry their sha12; NEW_DRIFTED cites its via chain.
    assert "- [INHERITED] src/engine.py (module_sha 0123456789ab)" in body
    assert "- [NEW_DRIFTED] src/new.py (module_sha 0123456789ab, via src.engine)" in body


def test_net_block_deterministic() -> None:
    # Same net → byte-identical render (the machinery's sorted order preserved).
    one = render_markdown(_view(audit_net=_MIXED))  # type: ignore[arg-type]
    two = render_markdown(_view(audit_net=_MIXED))  # type: ignore[arg-type]
    assert one == two


def test_net_moves_no_view_sha() -> None:
    # Presentation-only: the content address is identical with or without a net.
    assert _view().view_sha == _view(audit_net=_MIXED).view_sha  # type: ignore[attr-defined]


def test_net_cap_disclosure() -> None:
    # At the BFS cap WITH the machinery's cap_hit flag carried through, the
    # render discloses truncation (the MIRROR pin for audit_view.AUDIT_NET_CAP).
    # The flag — never len(entries) >= cap — is the authority.
    capped = tuple(
        _entry(f"m{i:03d}", AuditNetTier.UNRESOLVED, file=None, sha=None)
        for i in range(AUDIT_NET_CAP)
    )
    body = render_markdown(_view(audit_net=capped, audit_net_cap_hit=True))  # type: ignore[arg-type]
    assert f"net truncated at {AUDIT_NET_CAP} modules" in body
    digest = render_summary_markdown(_view(audit_net=capped, audit_net_cap_hit=True))  # type: ignore[arg-type]
    assert f"audit net truncated at {AUDIT_NET_CAP} modules (BFS cap)" in digest
    # Below the cap → no disclosure.
    body_one = render_markdown(_view(audit_net=_MIXED))  # type: ignore[arg-type]
    assert "truncated" not in body_one


def test_net_cap_length_alone_claims_no_truncation() -> None:
    # The dual: a closure COMPLETE at exactly the cap is NOT truncated — 256
    # entries WITHOUT the machinery's cap_hit flag render NO truncation line
    # in either mode (a bare length >= cap never claims truncation).
    capped = tuple(
        _entry(f"m{i:03d}", AuditNetTier.UNRESOLVED, file=None, sha=None)
        for i in range(AUDIT_NET_CAP)
    )
    body = render_markdown(_view(audit_net=capped))  # type: ignore[arg-type]
    assert "truncated" not in body
    digest = render_summary_markdown(_view(audit_net=capped))  # type: ignore[arg-type]
    assert "truncated" not in digest


# ── audit_view: the digest (counts only) ─────────────────────────────────────


def test_digest_carries_counts_not_bodies() -> None:
    digest = render_summary_markdown(_view(audit_net=_MIXED))  # type: ignore[arg-type]
    # One per-tier tally line (labels sorted) + the prominent finding line.
    assert (
        "- audit net: 5 module(s) — EXTERNAL 1, INHERITED 1, NEW_DRIFTED 1, UNRESOLVED 2" in digest
    )
    assert (
        "- ⚠ audit_net_unresolved: 2 module(s) resolve to NO file under the "
        "recorded source_roots (human_required)" in digest
    )
    # COUNTS only — no entry bodies (no tier badges, no per-entry lines).
    assert "- [UNRESOLVED]" not in digest
    assert "- [EXTERNAL]" not in digest
    assert "## audit net" not in digest


def test_digest_byte_absent_without_net() -> None:
    digest = render_summary_markdown(_view())  # type: ignore[arg-type]
    assert "audit net:" not in digest
    assert "audit_net_unresolved" not in digest


# ── net_tier_label ───────────────────────────────────────────────────────────


def test_net_tier_label_projects_enum_name() -> None:
    assert net_tier_label(AuditNetTier.UNRESOLVED) == UNRESOLVED_TIER_LABEL
    assert net_tier_label(AuditNetTier.NEW_DRIFTED) == "NEW_DRIFTED"
    # Bare strings render as themselves (caller-shaped entries).
    assert net_tier_label("EXTERNAL") == "EXTERNAL"
    assert net_tier_label(None) == "None"


# ── module_attention: UNRESOLVED participation ───────────────────────────────


def test_unresolved_entries_join_attention_without_roots(tmp_path: Path) -> None:
    # No source_roots → no module items, but the caller-supplied UNRESOLVED net
    # entries still charge attention (net order kept); EXTERNAL never does.
    src = parse_percent_source(_SOURCE)
    items = build_module_attention(
        tmp_path, source=src, source_roots=[], signed_section_bodies={}, audit_net=_MIXED
    )
    assert [it.module for it in items] == ["missing.mod", "other.bad"]
    first = items[0]
    assert first.file == ""  # nothing to sign — the import resolved to no file
    assert first.module_sha12 == ""
    assert first.dependents == ("source", "src.a")  # the via chain
    assert first.last_signed_sha12 is None
    assert first.moved_from_section is None


def test_unresolved_appended_after_unsigned_modules(tmp_path: Path) -> None:
    # An unsigned engine module charges attention FIRST; the UNRESOLVED entry
    # rides after it (after the sign-off-needed charges, before informational).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text("def train(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    src = parse_percent_source(_SOURCE)
    items = build_module_attention(
        tmp_path, source=src, source_roots=["src"], signed_section_bodies={}, audit_net=_MIXED
    )
    modules = [it.module for it in items]
    assert modules[0].endswith("engine.train") or "engine" in modules[0]
    assert modules[-2:] == ["missing.mod", "other.bad"]


def test_attention_byte_identical_without_net(tmp_path: Path) -> None:
    # audit_net=None → exactly the pre-6a item list (additive parameter).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text("def train(x):\n    return x\n", encoding="utf-8")
    src = parse_percent_source(_SOURCE)
    baseline = build_module_attention(
        tmp_path, source=src, source_roots=["src"], signed_section_bodies={}
    )
    with_none = build_module_attention(
        tmp_path, source=src, source_roots=["src"], signed_section_bodies={}, audit_net=None
    )
    assert baseline == with_none


# ── notebook-status: audit_net_summary + fail-open seam ──────────────────────


def _experiment(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text("def train(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "source.py").write_text(_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": _AUDIT,
                    "source_roots": ["src"],
                }
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _run_status(tmp_path: Path) -> NotebookStatusResult:
    return status_op.notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "template": "template.py"}
        ),
    )


def test_status_surfaces_unresolved_attention_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _experiment(tmp_path)
    seen: dict[str, Any] = {}

    def _double(seeds: object, experiment_dir: Path, root_dirs: list[Path], **kwargs: Any) -> tuple:
        seen["seeds"] = seeds
        seen["root_dirs"] = root_dirs
        return _MIXED, False

    monkeypatch.setattr(status_op, "_resolve_audit_net", _double)
    result = _run_status(tmp_path)

    # The resolver rode the RECORDED source_roots (joined under the experiment).
    root_dirs = seen["root_dirs"]
    assert isinstance(root_dirs, list) and len(root_dirs) == 1
    assert Path(str(root_dirs[0])).name == "src"
    # The UNRESOLVED entries charge attention, AFTER any module charges.
    net_items = [m for m in result.module_attention if m.module in ("missing.mod", "other.bad")]
    assert [m.module for m in net_items] == ["missing.mod", "other.bad"]
    assert net_items[0].file == ""
    assert list(net_items[0].dependents) == ["source", "src.a"]
    # The resolver rode the source's direct imports (the _SOURCE fixture);
    # imported_modules offers BOTH `engine` and `engine.train` for the
    # `from engine import train` form (deterministic AST-walk order).
    assert list(seen["seeds"]) == ["engine", "engine.train"]
    # The additive summary rollup: counts by tier + cap flag.
    summary = status_op._audit_net_summary(_MIXED, False)
    assert summary == AuditNetSummary(
        inherited=1, external=1, unresolved=2, new_drifted=1, cap_hit=False
    )
    if "audit_net_summary" in NotebookStatusResult.model_fields:
        # Post-merge: builder A's wire field carries the rollup.
        assert result.audit_net_summary == summary  # type: ignore[attr-defined]


def test_status_seam_fail_open_when_machinery_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cross-lane seam guard fires pre-merge (machinery None) — the status
    # rollup degrades to its pre-6a shape, never fails.
    _experiment(tmp_path)
    monkeypatch.setattr(status_op, "_resolve_audit_net", None)
    result = _run_status(tmp_path)
    assert [m.module for m in result.module_attention] == [] or all(
        m.module not in ("missing.mod", "other.bad") for m in result.module_attention
    )


def test_status_net_resolver_error_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A resolver error yields an empty net — presentation never fails the rollup.
    _experiment(tmp_path)

    def _broken(seeds: object, experiment_dir: Path, root_dirs: list[Path], **kwargs: Any) -> tuple:
        raise RuntimeError("cluster on fire")

    monkeypatch.setattr(status_op, "_resolve_audit_net", _broken)
    result = _run_status(tmp_path)
    assert all(m.module not in ("missing.mod",) for m in result.module_attention)


def test_status_summary_cap_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capped = tuple(
        _entry(f"m{i:03d}", AuditNetTier.INHERITED, file=f"src/m{i:03d}.py")
        for i in range(AUDIT_NET_CAP)
    )
    assert status_op._audit_net_summary(capped, True).cap_hit is True
    assert status_op._audit_net_summary(capped[: AUDIT_NET_CAP - 1], False).cap_hit is False
    assert status_op._audit_net_summary((), False) == AuditNetSummary()
