"""Wave-3 — src modules as signable attention units (piece 3) + moved-code (5).

Pins the maintainer intent "attention charged per CHANGED PIECE, never per
dependent":

* the module sign-off record binds ``module_sha`` un-fakeably (a hash asserted
  into existence is refused), and the append-time authorship floor refuses a bare
  ack / an un-naming utterance;
* the graduation gate treats a linked module whose CURRENT sha is human-signed as
  current — a module CHANGE revokes every dependent, and ONE module re-sign
  restores them ALL (never per-section);
* an UNSIGNED linked module surfaces ONE module-attention item listing its
  dependents (never one per dependent); a signed module surfaces none;
* the moved-code disclosure names a signed section whose body an unsigned module
  matches — ADVISORY only, it never clears the module.

TOY vocabulary only (widget lineage).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent._wire.queries.notebook_status import NotebookStatusSpec
from hpc_agent.ops.decision.journal import append_decision as ops_append_decision
from hpc_agent.ops.decision.journal.module_signoff import _assert_module_signoff_authorship
from hpc_agent.ops.decision.journal.signoff import _assert_signoff_authorship
from hpc_agent.ops.notebook.status_op import notebook_status
from hpc_agent.ops.notebook_gate import (
    AUDIT_NET_FIELD,
    NET_INHERITED,
    _classify_net_module,
    _template_module_shas,
    assert_source_audited,
)
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized
from hpc_agent.state.decision_journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT = "widget-audit"
_ENGINE_REL = "src/engine.py"

_ENGINE_V1 = "def train(x, y=1):\n    return x + y\n"
_ENGINE_V2 = "def train(x, y=2):\n    return x * y\n"

# Two sections that both import the engine → two dependents of ONE module.
_SOURCE = """# %%
# hpc-audit-section: fit
from engine import train
a = train(1)

# %%
# hpc-audit-section: score
from engine import train
b = train(2)
"""
_TEMPLATE = """# %%
# hpc-audit-section: fit
a = 0

# %%
# hpc-audit-section: score
b = 0
"""


def _sha(source_text: str, slug: str) -> str:
    parsed = parse_percent_source(source_text)
    return next(s.section_sha for s in parsed.sections if s.slug == slug)


def _setup(exp: Path, *, engine: str = _ENGINE_V1, source: str = _SOURCE) -> None:
    (exp / "src").mkdir(exist_ok=True)
    (exp / "src" / "engine.py").write_text(engine, encoding="utf-8")
    (exp / "source.py").write_text(source, encoding="utf-8")
    (exp / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    (exp / "interview.json").write_text(
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


def _sign_section(exp: Path, slug: str, source_text: str, *, engine_sha: str) -> None:
    """Journal a HUMAN section sign-off carrying linked_sources (bypass the gate)."""
    append_decision(
        exp,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response=f"sign {slug}",
        resolved={
            "audit_id": _AUDIT,
            "section": slug,
            "section_sha": _sha(source_text, slug),
            "linked_sources": [{"module": "engine", "file": _ENGINE_REL, "module_sha": engine_sha}],
        },
        ts="2026-05-01T00:00:00Z",
    )


# ── the module sign-off record: the un-fakeable bind + authorship floor ──────


def test_module_signoff_binds_the_recomputed_sha(tmp_path: Path) -> None:
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    rec = nb.record_module_signoff(
        tmp_path, audit_id=_AUDIT, module=_ENGINE_REL, module_sha=sha1, recompute=sha1
    )
    assert rec["block"] == nb.MODULE_SIGN_OFF_BLOCK
    assert nb.module_sha_signed(tmp_path, sha1) is True


def test_module_signoff_refuses_an_asserted_sha(tmp_path: Path) -> None:
    _setup(tmp_path)
    with pytest.raises(errors.SpecInvalid):
        nb.record_module_signoff(
            tmp_path,
            audit_id=_AUDIT,
            module=_ENGINE_REL,
            module_sha="deadbeef",  # not the recomputed sha
            recompute=sha256_normalized(_ENGINE_V1),
        )


def _module_spec(response: str, module_sha: str) -> AppendDecisionInput:
    return AppendDecisionInput.model_validate(
        {
            "scope_kind": "notebook",
            "scope_id": _AUDIT,
            "block": nb.MODULE_SIGN_OFF_BLOCK,
            "response": response,
            "resolved": {"audit_id": _AUDIT, "module": _ENGINE_REL, "module_sha": module_sha},
        }
    )


def test_module_gate_accepts_a_naming_utterance(tmp_path: Path) -> None:
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    # Names the module (its relpath), non-bare, correct sha → passes the gate.
    _assert_module_signoff_authorship(
        tmp_path,
        _module_spec(f"reviewed and signing module {_ENGINE_REL}", sha1),
        {"audit_id": _AUDIT, "module": _ENGINE_REL, "module_sha": sha1},
    )


def test_module_gate_refuses_a_bare_ack(tmp_path: Path) -> None:
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    with pytest.raises(errors.SpecInvalid):
        _assert_module_signoff_authorship(
            tmp_path,
            _module_spec("y", sha1),
            {"audit_id": _AUDIT, "module": _ENGINE_REL, "module_sha": sha1},
        )


def test_module_gate_refuses_a_wrong_sha(tmp_path: Path) -> None:
    _setup(tmp_path)
    with pytest.raises(errors.SpecInvalid):
        _assert_module_signoff_authorship(
            tmp_path,
            _module_spec(f"signing {_ENGINE_REL}", "deadbeef"),
            {"audit_id": _AUDIT, "module": _ENGINE_REL, "module_sha": "deadbeef"},
        )


def test_module_signoff_lands_through_the_full_append_path(tmp_path: Path) -> None:
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    result = ops_append_decision(
        experiment_dir=tmp_path,
        spec=_module_spec(f"I reviewed {_ENGINE_REL}; signing this module", sha1),
    )
    assert result.record.block == nb.MODULE_SIGN_OFF_BLOCK
    assert nb.module_sha_signed(tmp_path, sha1) is True


# ── the MIRROR pin: the module gate + the section gate share ONE discipline ──


def _section_resolved(**over: str) -> dict[str, str]:
    resolved = {
        "audit_id": _AUDIT,
        "section": "fit",
        "section_sha": "deadbeef",
        "view_sha": "cafebabe",
        "source": "source.py",
        "template": "template.py",
    }
    resolved.update(over)
    return resolved


def _section_spec(response: str, resolved: dict[str, str]) -> AppendDecisionInput:
    return AppendDecisionInput.model_validate(
        {
            "scope_kind": "notebook",
            "scope_id": _AUDIT,
            "block": nb.SIGN_OFF_BLOCK,
            "response": response,
            "resolved": resolved,
        }
    )


def test_module_gate_lockstep_with_section_gate(tmp_path: Path) -> None:
    """The module sign-off gate inherits the section gate's record discipline
    (the MIRROR pin), asserted on BOTH sides so a drift in either gate fails
    here — the block convention plus the two locks the module gate reuses:

    * block convention, both directions — the block under a non-notebook
      scope_kind is refused; an unrelated record passes both gates untouched;
    * Lock 3 (authorship floor, friction tier — no utterance log) — a bare
      ack is refused, and a non-bare response that never NAMES the subject
      (the section slug / the module relpath) is refused;
    * Lock 2 (recompute, un-fakeable) — an asserted sha that does not
      recompute from the file on disk is refused through the ONE attestation
      kernel on both sides.
    """
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)

    # Block convention, both directions, on BOTH gates.
    wrong_scope_section = AppendDecisionInput.model_validate(
        {"scope_kind": "run", "scope_id": "r", "block": nb.SIGN_OFF_BLOCK, "response": "x"}
    )
    with pytest.raises(errors.SpecInvalid):
        _assert_signoff_authorship(tmp_path, wrong_scope_section, None)
    wrong_scope_module = AppendDecisionInput.model_validate(
        {"scope_kind": "run", "scope_id": "r", "block": nb.MODULE_SIGN_OFF_BLOCK, "response": "x"}
    )
    with pytest.raises(errors.SpecInvalid):
        _assert_module_signoff_authorship(tmp_path, wrong_scope_module, None)
    unrelated = AppendDecisionInput.model_validate(
        {
            "scope_kind": "notebook",
            "scope_id": _AUDIT,
            "block": "some-other-block",
            "response": "x",
        }
    )
    _assert_signoff_authorship(tmp_path, unrelated, None)  # passes untouched
    _assert_module_signoff_authorship(tmp_path, unrelated, None)  # passes untouched

    # Lock 3 (friction tier): a bare ack is refused on BOTH gates.
    with pytest.raises(errors.SpecInvalid):
        _assert_signoff_authorship(
            tmp_path, _section_spec("y", _section_resolved()), _section_resolved()
        )
    with pytest.raises(errors.SpecInvalid):
        _assert_module_signoff_authorship(
            tmp_path,
            _module_spec("y", sha1),
            {"audit_id": _AUDIT, "module": _ENGINE_REL, "module_sha": sha1},
        )

    # Lock 3: a non-bare response that never NAMES the subject is refused on BOTH.
    with pytest.raises(errors.SpecInvalid):
        _assert_signoff_authorship(
            tmp_path,
            _section_spec("I reviewed the change and it looks correct", _section_resolved()),
            _section_resolved(),
        )
    with pytest.raises(errors.SpecInvalid):
        _assert_module_signoff_authorship(
            tmp_path,
            _module_spec("I reviewed the change and it looks correct", sha1),
            {"audit_id": _AUDIT, "module": _ENGINE_REL, "module_sha": sha1},
        )

    # Lock 2 (recompute): an asserted sha that does not recompute from the file
    # on disk is refused on BOTH gates (the response passes the floor first, so
    # the refusal is the bind, never the authorship tier).
    with pytest.raises(errors.SpecInvalid):
        _assert_signoff_authorship(
            tmp_path,
            _section_spec("reviewed and signing section fit", _section_resolved()),
            _section_resolved(),
        )
    with pytest.raises(errors.SpecInvalid):
        _assert_module_signoff_authorship(
            tmp_path,
            _module_spec(f"reviewed and signing module {_ENGINE_REL}", "deadbeef"),
            {"audit_id": _AUDIT, "module": _ENGINE_REL, "module_sha": "deadbeef"},
        )


# ── the graduation-gate drift exemption: one re-sign clears all dependents ────


def test_signed_module_exempts_drifted_dependents(tmp_path: Path) -> None:
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    # Both sections signed against engine V1.
    _sign_section(tmp_path, "fit", _SOURCE, engine_sha=sha1)
    _sign_section(tmp_path, "score", _SOURCE, engine_sha=sha1)
    assert_source_audited(tmp_path)  # baseline: engine matches, gate passes

    # Change the engine — every dependent now drifts; the gate refuses, naming both.
    (tmp_path / "src" / "engine.py").write_text(_ENGINE_V2, encoding="utf-8")
    with pytest.raises(errors.SourceUnaudited) as exc:
        assert_source_audited(tmp_path)
    assert "fit" in str(exc.value) and "score" in str(exc.value)

    # ONE module re-sign of the NEW sha restores ALL dependents (never per-section).
    sha2 = sha256_normalized(_ENGINE_V2)
    nb.record_module_signoff(
        tmp_path, audit_id=_AUDIT, module=_ENGINE_REL, module_sha=sha2, recompute=sha2
    )
    assert_source_audited(tmp_path)  # both dependents restored by the single re-sign


def test_unsigned_module_change_still_revokes(tmp_path: Path) -> None:
    # The KILL-INVARIANT for modules: a changed module with NO sign-off of the new
    # sha revokes its dependents (attention is owed).
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    _sign_section(tmp_path, "fit", _SOURCE, engine_sha=sha1)
    _sign_section(tmp_path, "score", _SOURCE, engine_sha=sha1)
    (tmp_path / "src" / "engine.py").write_text(_ENGINE_V2, encoding="utf-8")
    with pytest.raises(errors.SourceUnaudited):
        assert_source_audited(tmp_path)


# ── the module-attention surface: ONE item per module, never per dependent ───


def test_unsigned_module_is_one_attention_item_with_all_dependents(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "template": "template.py"}
        ),
    )
    assert len(result.module_attention) == 1, "one item for the module, not one per dependent"
    item = result.module_attention[0]
    assert item.file == _ENGINE_REL
    assert sorted(item.dependents) == ["fit", "score"]
    assert item.module_sha12 == sha256_normalized(_ENGINE_V1)[:12]


def test_signed_module_produces_no_attention_item(tmp_path: Path) -> None:
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    nb.record_module_signoff(
        tmp_path, audit_id=_AUDIT, module=_ENGINE_REL, module_sha=sha1, recompute=sha1
    )
    result = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "template": "template.py"}
        ),
    )
    assert result.module_attention == []


# ── piece 5: moved-code disclosure (advisory, never clears) ──────────────────

# A signed section "helper" whose body is the SAME code that lives in the engine
# module — the extraction the recurrence nudge predicts. The unsigned engine's
# attention item discloses the match but is NOT cleared by it.
_ENGINE_MOVED = "def widgetize(a):\n    z = a + 1\n    q = z * 2\n    return q\n"
_SOURCE_MOVED = """# %%
# hpc-audit-section: helper
def widgetize(a):
    z = a + 1
    q = z * 2
    return q

# %%
# hpc-audit-section: use
from engine import widgetize
r = widgetize(3)
"""


def test_moved_code_disclosed_but_never_clears(tmp_path: Path) -> None:
    _setup(tmp_path, engine=_ENGINE_MOVED, source=_SOURCE_MOVED)
    # "helper" is HUMAN-signed; its body matches the engine module verbatim.
    _sign_section(tmp_path, "helper", _SOURCE_MOVED, engine_sha=sha256_normalized(_ENGINE_MOVED))
    result = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "template": "template.py"}
        ),
    )
    # The module is still UNSIGNED → still an attention item (the fuzzy match
    # cleared NOTHING), and it DISCLOSES the moved-from section.
    assert len(result.module_attention) == 1
    item = result.module_attention[0]
    assert item.moved_from_section == "helper"
    assert item.moved_overlap is not None and item.moved_overlap[0] >= 3


# ── 6a backfill: the audit net rides the module sign-off; the INHERITED proof
# leg (ruling 3) is template-identical OR ledger-attested (module_sha_signed) ──


def _sign_section_no_links(exp: Path, slug: str, source_text: str) -> None:
    """Sign a section WITHOUT linked_sources — so the section path passes cleanly and
    the 6a audit-net path (reached only when every section is signed-current) is the
    gate leg under test, not the per-section linked-source drift check."""
    append_decision(
        exp,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response=f"sign {slug}",
        resolved={
            "audit_id": _AUDIT,
            "section": slug,
            "section_sha": _sha(source_text, slug),
            "view_sha": "view-" + _sha(source_text, slug)[:8],
        },
        ts="2026-05-01T00:00:00Z",
    )


def _sign_module_net(exp: Path, *, module: str, module_sha: str, modules: dict) -> None:
    """Append a net-carrying ``notebook-module-sign-off`` record (RAW — bypasses the
    append-time module gate, the graduation-gate fixture posture). ``modules`` is the
    carried audit net's ``{module: {tier, module_sha}}`` map (6a)."""
    append_decision(
        exp,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block=nb.MODULE_SIGN_OFF_BLOCK,
        response=f"sign module {module}",
        resolved={
            "audit_id": _AUDIT,
            "module": module,
            "module_sha": module_sha,
            AUDIT_NET_FIELD: {"env_hash": "", "modules": modules},
        },
        ts="2026-05-01T00:00:00Z",
    )


def test_net_carried_on_the_module_signoff_recomputes_and_refuses(tmp_path: Path) -> None:
    """The audit net rides the module-sign-off record; the graduation gate RECOMPUTES the
    carried closure and refuses when a module's sha drifts with no re-sign (NEW_DRIFTED)."""
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    _sign_section_no_links(tmp_path, "fit", _SOURCE)
    _sign_section_no_links(tmp_path, "score", _SOURCE)
    _sign_module_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha1,
        modules={"engine": {"tier": NET_INHERITED, "module_sha": sha1}},
    )
    assert_source_audited(tmp_path)  # baseline: engine matches the carried net

    (tmp_path / "src" / "engine.py").write_text(_ENGINE_V2, encoding="utf-8")
    with pytest.raises(errors.SourceUnaudited) as exc:
        assert_source_audited(tmp_path)
    assert "engine" in str(exc.value)  # the drifted closure module is named


def test_net_inherited_ledger_attested_leg_clears_drift(tmp_path: Path) -> None:
    """INHERITED proof leg (ruling 3, ledger-attested): a carried module whose sha moved
    is still INHERITED when its NEW sha carries a human module sign-off
    (``module_sha_signed``) — ONE re-sign of the new sha clears the net drift, exactly
    the wave-3 "one re-sign clears all dependents" flow lifted onto the audit net."""
    _setup(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    _sign_section_no_links(tmp_path, "fit", _SOURCE)
    _sign_section_no_links(tmp_path, "score", _SOURCE)
    _sign_module_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha1,
        modules={"engine": {"tier": NET_INHERITED, "module_sha": sha1}},
    )
    (tmp_path / "src" / "engine.py").write_text(_ENGINE_V2, encoding="utf-8")
    with pytest.raises(errors.SourceUnaudited):
        assert_source_audited(tmp_path)  # drifted, unsigned → refuses

    # Backfill the attestation ledger: ONE module re-sign of the NEW sha (V2).
    sha2 = sha256_normalized(_ENGINE_V2)
    nb.record_module_signoff(
        tmp_path, audit_id=_AUDIT, module=_ENGINE_REL, module_sha=sha2, recompute=sha2
    )
    assert_source_audited(tmp_path)  # the ledger-attested leg clears the drifted module


def test_net_inherited_template_identical_leg(tmp_path: Path) -> None:
    """INHERITED proof leg (ruling 3, template-identical): a module the TEMPLATE itself
    imports is baseline — it reads INHERITED at its current sha even when the carried
    net recorded a STALE sha (the template's closure already vouches for it)."""
    (tmp_path / "src").mkdir(exist_ok=True)
    util = "def u():\n    return 1\n"
    (tmp_path / "src" / "util.py").write_text(util, encoding="utf-8")
    (tmp_path / "template.py").write_text(
        "# %%\n# hpc-audit-section: s\nfrom util import u\n", encoding="utf-8"
    )
    roots = [tmp_path / "src"]
    template_shas = _template_module_shas(tmp_path, "template.py", roots)
    util_sha = sha256_normalized(util)
    assert template_shas.get("util") == util_sha  # the template binds util at its current sha

    # The carried net recorded a STALE sha, but the template-identical leg holds at the
    # current sha → INHERITED (no attention owed), never NEW_DRIFTED.
    tier, sha, _origin = _classify_net_module(
        tmp_path,
        "util",
        recorded_sha="stale" + "0" * 59,
        source_roots=roots,
        template_shas=template_shas,
    )
    assert tier == NET_INHERITED
    assert sha == util_sha
