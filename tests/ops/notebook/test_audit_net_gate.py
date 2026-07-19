"""notebook-audit 6a — the transitive import-closure "audit net" gate.

Pins the 6a rulings ("track-total, attend-drift") over the graduation gate
(``ops/notebook_gate.py``):

* a ``notebook-module-sign-off`` record CARRIES the audit net
  (``resolved["audit_net"] = {env_hash, modules: {module: {tier, module_sha}}}``),
  the exact shape :func:`build_audit_net` mints and ``_carried_audit_net`` reads;
* the gate RECOMPUTES each carried module's current tier and REFUSES the submit on a
  drifted closure — :data:`NET_NEW_DRIFTED` / :data:`NET_UNRESOLVED` raise
  ``SourceUnaudited`` NAMING the modules (flip a module's sha → refuse);
* :data:`NET_EXTERNAL` entries are DISCLOSED as ``env_hash``-bound (the record carries
  the local ``env_hash`` the classification rested on), NEVER refused;
* legacy NET-LESS sign-off records are GRANDFATHERED — validated under the old rule,
  never retro-refused by the net path;
* the env-bound EXTERNAL classification uses ``importlib.util.find_spec`` ONLY — a
  module that raises on import still classifies EXTERNAL (metadata, never exec).

TOY vocabulary only. Net-carrying records are appended RAW (bypassing the append-time
module-sign-off gate) exactly as every other graduation-gate fixture appends records.
"""

from __future__ import annotations

import importlib
import json
import sys
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent.ops.notebook_gate import (
    AUDIT_NET_FIELD,
    NET_EXTERNAL,
    NET_INHERITED,
    NET_NEW_DRIFTED,
    NET_UNRESOLVED,
    _carried_audit_net,
    _classify_net_module,
    _compute_env_hash,
    _find_spec_origin,
    assert_source_audited,
    audit_net_disclosures,
    build_audit_net,
)
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized
from hpc_agent.state.decision_journal import append_decision, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT = "net-audit"
_ENGINE_REL = "src/engine.py"

_ENGINE_V1 = "def train(x, y=1):\n    return x + y\n"
_ENGINE_V2 = "def train(x, y=2):\n    return x * y\n"

# A two-section source IDENTICAL to its template — the section path passes once both
# sections are raw-signed, so the audit-net path (reached only when every required
# section is signed-current) is what these tests exercise. The source imports the
# engine; the TEMPLATE does NOT, so the engine is a source-ADDED module (not part of
# the template's baseline closure → never template-identical).
_MODULE = (
    "# %%\n"
    "# hpc-audit-section: setup\n"
    "from engine import train\n"
    "\n"
    "# %%\n"
    "# hpc-audit-section: run\n"
    "result = int(train(1))\n"
)
_TEMPLATE = "# %%\n# hpc-audit-section: setup\na = 0\n\n# %%\n# hpc-audit-section: run\nb = 0\n"


def _section_sha(slug: str, text: str = _MODULE) -> str:
    return next(s.section_sha for s in parse_percent_source(text).sections if s.slug == slug)


def _write_opted_in(exp: Path, *, engine: str = _ENGINE_V1) -> None:
    (exp / "src").mkdir(exist_ok=True)
    (exp / "src" / "engine.py").write_text(engine, encoding="utf-8")
    (exp / "source.py").write_text(_MODULE, encoding="utf-8")
    (exp / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    (exp / "interview.json").write_text(
        json.dumps(
            {
                "goal": "fit",
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": _AUDIT,
                    "source_roots": ["src"],
                },
            }
        ),
        encoding="utf-8",
    )


def _sign_sections(exp: Path) -> None:
    """Raw HUMAN sign-offs for both required sections at their current sha (bypass T8)."""
    for slug in ("setup", "run"):
        append_decision(
            exp,
            scope_kind="notebook",
            scope_id=_AUDIT,
            block="notebook-sign-off",
            response="y",
            resolved={
                "audit_id": _AUDIT,
                "section": slug,
                "section_sha": _section_sha(slug),
                "view_sha": "view-" + _section_sha(slug)[:8],
            },
        )


def _sign_module_with_net(
    exp: Path, *, module: str, module_sha: str | None, net: dict | None
) -> None:
    """Append a ``notebook-module-sign-off`` record carrying *net* (RAW — bypasses the
    append-time module gate). ``net=None`` writes a LEGACY net-less record."""
    resolved: dict[str, Any] = {"audit_id": _AUDIT, "module": module, "module_sha": module_sha}
    if net is not None:
        resolved[AUDIT_NET_FIELD] = net
    append_decision(
        exp,
        scope_kind="notebook",
        scope_id=_AUDIT,
        block="notebook-module-sign-off",
        response=f"sign module {module}",
        resolved=resolved,
    )


def _net(modules: dict[str, Any], *, env_hash: str = "") -> dict[str, Any]:
    return {"env_hash": env_hash, "modules": modules}


# ── the net is carried on the record (and build_audit_net mints the shape) ────


def test_build_audit_net_mints_the_carried_shape(tmp_path: Path) -> None:
    """``build_audit_net`` (via the injected test-double resolver — the A seam is never
    imported under CI) mints the exact ``{env_hash, modules: {tier, module_sha}}`` shape
    a record carries: a local file → INHERITED at its sha; an installed module →
    EXTERNAL (its origin folds into ``env_hash``); an unresolvable name → UNRESOLVED."""
    engine_sha = sha256_normalized(_ENGINE_V1)

    def _resolver(
        _exp: Path, _src: str, _roots: object
    ) -> list[tuple[str, str | None, str | None]]:
        return [
            ("engine", engine_sha, None),  # a local source file
            ("numpy", None, "/site-packages/numpy/__init__.py"),  # installed
            ("ghost", None, None),  # resolves to nothing
        ]

    net = build_audit_net(tmp_path, "source.py", ["src"], _resolver=_resolver)
    assert net["modules"]["engine"] == {"tier": NET_INHERITED, "module_sha": engine_sha}
    assert net["modules"]["numpy"] == {"tier": NET_EXTERNAL, "module_sha": None}
    assert net["modules"]["ghost"] == {"tier": NET_UNRESOLVED, "module_sha": None}
    # env_hash binds ONLY the external set (numpy's origin); deterministic + non-empty.
    assert net["env_hash"] == _compute_env_hash({"numpy": "/site-packages/numpy/__init__.py"})
    assert net["env_hash"] != _compute_env_hash({})


def test_net_carried_on_the_signoff_record_round_trips(tmp_path: Path) -> None:
    """A net-carrying ``notebook-module-sign-off`` record persists the net under
    ``resolved["audit_net"]`` and ``_carried_audit_net`` reads it back verbatim — the
    durable record the gate recomputes."""
    _write_opted_in(tmp_path)
    engine_sha = sha256_normalized(_ENGINE_V1)
    net = _net({"engine": {"tier": NET_INHERITED, "module_sha": engine_sha}}, env_hash="env-1")
    _sign_module_with_net(tmp_path, module=_ENGINE_REL, module_sha=engine_sha, net=net)

    records = [
        r
        for r in read_decisions(tmp_path, "notebook", _AUDIT)
        if r.get("block") == "notebook-module-sign-off"
    ]
    assert len(records) == 1
    assert records[0]["resolved"][AUDIT_NET_FIELD] == net
    assert _carried_audit_net(records[0]) == net


# ── recompute-and-refuse on closure drift (flip a module sha) ─────────────────


def test_gate_refuses_when_a_carried_module_sha_drifts(tmp_path: Path) -> None:
    """Sign the net with engine at V1, then flip engine to V2 (unsigned): the gate
    RECOMPUTES the closure, reads NEW_DRIFTED, and refuses NAMING the module."""
    _write_opted_in(tmp_path, engine=_ENGINE_V1)
    _sign_sections(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    _sign_module_with_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha1,
        net=_net({"engine": {"tier": NET_INHERITED, "module_sha": sha1}}),
    )
    assert_source_audited(tmp_path)  # baseline: engine matches the recorded net → passes

    # Flip the module — its sha moves with no re-sign → NEW_DRIFTED → refuse.
    (tmp_path / "src" / "engine.py").write_text(_ENGINE_V2, encoding="utf-8")
    with pytest.raises(errors.SourceUnaudited) as ei:
        assert_source_audited(tmp_path)
    msg = str(ei.value)
    assert "engine" in msg  # names the drifted module
    assert NET_NEW_DRIFTED in msg
    assert ei.value.error_code == "precondition_failed"
    assert ei.value.retry_safe is False


def test_gate_accepts_a_carried_net_whose_modules_are_current(tmp_path: Path) -> None:
    """The companion: every carried module still at its recorded sha (or re-attested) →
    the net path passes and the submit clears."""
    _write_opted_in(tmp_path, engine=_ENGINE_V1)
    _sign_sections(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    _sign_module_with_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha1,
        net=_net({"engine": {"tier": NET_INHERITED, "module_sha": sha1}}),
    )
    assert_source_audited(tmp_path)  # no raise


# ── NEW_DRIFTED / UNRESOLVED refusals name the modules ────────────────────────


def test_unresolved_carried_module_is_refused_naming_it(tmp_path: Path) -> None:
    """A carried module that no longer resolves (neither a local file nor installed) is
    UNRESOLVED → refused, named in the refusal."""
    _write_opted_in(tmp_path)
    _sign_sections(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    # 'ghost' is in the carried net but resolves to nothing on disk or in the env.
    _sign_module_with_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha1,
        net=_net(
            {
                "engine": {"tier": NET_INHERITED, "module_sha": sha1},
                "ghost": {"tier": NET_INHERITED, "module_sha": "0" * 64},
            }
        ),
    )
    with pytest.raises(errors.SourceUnaudited) as ei:
        assert_source_audited(tmp_path)
    msg = str(ei.value)
    assert "ghost" in msg  # names the unresolved module
    assert NET_UNRESOLVED in msg
    assert "engine" not in msg  # the current module is NOT named


def test_refusal_names_every_drifted_module(tmp_path: Path) -> None:
    """Two drifted carried modules → BOTH named in one refusal (the one-shot posture):
    the engine flipped to an UN-RE-SIGNED sha (NEW_DRIFTED) and a module that resolves to
    nothing (UNRESOLVED)."""
    _write_opted_in(tmp_path, engine=_ENGINE_V1)
    _sign_sections(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    _sign_module_with_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha1,
        net=_net(
            {
                "engine": {"tier": NET_INHERITED, "module_sha": sha1},  # drifts below
                "ghost": {"tier": NET_INHERITED, "module_sha": "0" * 64},  # unresolved
            }
        ),
    )
    # Flip the engine to V2 with NO module re-sign: the record attests sha1, so
    # module_sha_signed(sha2) is False → NEW_DRIFTED (the ledger leg cannot save it).
    (tmp_path / "src" / "engine.py").write_text(_ENGINE_V2, encoding="utf-8")
    with pytest.raises(errors.SourceUnaudited) as ei:
        assert_source_audited(tmp_path)
    msg = str(ei.value)
    assert "engine" in msg and "ghost" in msg


# ── EXTERNAL is disclosed as env_hash-bound, never refused ────────────────────


def test_external_module_is_disclosed_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A carried EXTERNAL module (installed, ``find_spec``-able) does NOT refuse the
    submit; ``audit_net_disclosures`` reports it bound to its ``env_hash``."""
    extdir = tmp_path / "extsite"
    extdir.mkdir()
    (extdir / "extmod.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(extdir))
    importlib.invalidate_caches()

    _write_opted_in(tmp_path)
    _sign_sections(tmp_path)
    origin = _find_spec_origin("extmod")
    assert origin is not None  # installed on the prepended path
    env_hash = _compute_env_hash({"extmod": origin})
    _sign_module_with_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha256_normalized(_ENGINE_V1),
        net=_net({"extmod": {"tier": NET_EXTERNAL, "module_sha": None}}, env_hash=env_hash),
    )

    assert_source_audited(tmp_path)  # EXTERNAL never refuses
    disclosures = audit_net_disclosures(tmp_path)
    assert len(disclosures) == 1
    d = disclosures[0]
    assert d["module"] == "extmod"
    assert d["tier"] == NET_EXTERNAL
    assert d["recorded_env_hash"] == env_hash
    assert d["current_env_hash"] == env_hash
    assert d["env_status"] == "match"


def test_external_env_hash_drift_is_disclosed_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the recorded ``env_hash`` no longer matches the recomputed one (the external
    origin moved), the disclosure reads ``env_status="drifted"`` — still NOT a refusal."""
    extdir = tmp_path / "extsite"
    extdir.mkdir()
    (extdir / "extmod.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(extdir))
    importlib.invalidate_caches()

    _write_opted_in(tmp_path)
    _sign_sections(tmp_path)
    _sign_module_with_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha256_normalized(_ENGINE_V1),
        net=_net({"extmod": {"tier": NET_EXTERNAL, "module_sha": None}}, env_hash="stale-env-hash"),
    )

    assert_source_audited(tmp_path)  # still not a refusal
    disclosures = audit_net_disclosures(tmp_path)
    assert len(disclosures) == 1
    assert disclosures[0]["recorded_env_hash"] == "stale-env-hash"
    assert disclosures[0]["env_status"] == "drifted"


# ── legacy net-less records are GRANDFATHERED (never retro-refused) ───────────


def test_legacy_netless_module_signoff_is_grandfathered(tmp_path: Path) -> None:
    """A pre-6a ``notebook-module-sign-off`` record with NO ``audit_net`` validates
    under the old rule — the net path skips it entirely, so even a CHANGED module under
    a net-less record does not trip the 6a refusal."""
    _write_opted_in(tmp_path, engine=_ENGINE_V1)
    _sign_sections(tmp_path)
    sha1 = sha256_normalized(_ENGINE_V1)
    _sign_module_with_net(tmp_path, module=_ENGINE_REL, module_sha=sha1, net=None)  # net-less

    # Flip the module the net-less record signed — the 6a net path never fires on a
    # net-less record (grandfathered), so the submit still clears.
    (tmp_path / "src" / "engine.py").write_text(_ENGINE_V2, encoding="utf-8")
    assert_source_audited(tmp_path)  # no raise — grandfathered
    assert audit_net_disclosures(tmp_path) == []


def test_malformed_net_is_grandfathered_not_a_refusal(tmp_path: Path) -> None:
    """A present-but-malformed ``audit_net`` (no ``modules`` dict) reads as net-less —
    only a WELL-FORMED net can trigger a refusal, so a hand-forged shape never blocks."""
    _write_opted_in(tmp_path)
    _sign_sections(tmp_path)
    _sign_module_with_net(
        tmp_path,
        module=_ENGINE_REL,
        module_sha=sha256_normalized(_ENGINE_V1),
        net={"env_hash": "x", "modules": "not-a-dict"},  # malformed
    )
    assert_source_audited(tmp_path)  # no raise


# ── the env-bound classification uses find_spec ONLY (never exec) ─────────────


def test_external_classification_uses_find_spec_not_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module whose BODY raises on import still classifies EXTERNAL — the gate uses
    ``importlib.util.find_spec`` (metadata-only), never importing/executing it. Asserted
    via a module that raises on import: no RuntimeError escapes, and the module never
    lands in ``sys.modules``."""
    extdir = tmp_path / "boomsite"
    extdir.mkdir()
    (extdir / "boom_on_import.py").write_text(
        'raise RuntimeError("boom: this module must never be executed by the gate")\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(extdir))
    importlib.invalidate_caches()
    sys.modules.pop("boom_on_import", None)

    # Direct classifier: a carried module that resolves via find_spec (not a local file)
    # → EXTERNAL, WITHOUT executing its body.
    tier, sha, origin = _classify_net_module(
        tmp_path,
        "boom_on_import",
        recorded_sha=None,
        source_roots=[],
        template_shas={},
    )
    assert tier == NET_EXTERNAL
    assert sha is None
    assert origin is not None and "boom_on_import" in origin
    assert "boom_on_import" not in sys.modules  # never imported / executed


def test_find_spec_origin_resolves_without_importing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_find_spec_origin`` returns the origin of an import-raising module without
    importing it; an unresolvable name reads ``None`` (never raises)."""
    extdir = tmp_path / "raisesite"
    extdir.mkdir()
    (extdir / "raises_mod.py").write_text("raise ValueError('nope')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(extdir))
    importlib.invalidate_caches()
    sys.modules.pop("raises_mod", None)

    origin = _find_spec_origin("raises_mod")
    assert origin is not None and "raises_mod" in origin
    assert "raises_mod" not in sys.modules
    # A name that resolves nowhere reads None, never raises.
    assert _find_spec_origin("definitely_not_installed_xyz_123") is None
