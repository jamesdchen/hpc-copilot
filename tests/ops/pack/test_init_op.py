"""Tests for ``program-init`` (P1a) — materialize/adopt a PROGRAM pack layer.

Covers, with a synthetic TOY domain pack (never a real domain's words):

* **create** end-to-end — generates ``packs/<program>/`` with the template =
  provenance header + verbatim skeleton bytes, a sealed manifest whose
  ``derived_from == {pack, 'audit_template', version, sha256(skeleton bytes)}``,
  and both packs bound;
* a caller-supplied ``check`` runs + journals (failing check reported, not raised);
* refusals FIRE (and the happy path passes): create over an existing dir, a domain
  manifest with no ``audit_template`` seam, a dangling domain manifest, a
  contradicting interview packs block;
* **adopt** end-to-end — the signed template stays BYTE-IDENTICAL, ``derived_from``
  is stamped from the on-disk source seam sha, only the program pack is rebound.

Toy vocabulary only.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.pack_bind import PackBindSpec
from hpc_agent._wire.actions.program_init import ProgramInitSpec
from hpc_agent.ops.pack.bind_op import pack_bind
from hpc_agent.ops.pack.init_op import program_init
from hpc_agent.state import pack, pack_sweep
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.pack_receipts import PACK_SUBJECT_KIND, current_bind

if TYPE_CHECKING:
    from pathlib import Path

_SKELETON = b"# %% domain skeleton\nPINNED = 1\n"


def _write_domain(experiment_dir: Path, *, seam: bool = True, version: str = "0.2.0") -> str:
    """A synthetic domain pack; returns the skeleton's raw-bytes sha."""
    dom = experiment_dir / "packs" / "toy-domain"
    (dom / "templates").mkdir(parents=True, exist_ok=True)
    (dom / "templates" / "skeleton.py").write_bytes(_SKELETON)
    recipe: dict = {
        "name": "toy-domain",
        "version": version,
        "seams": {"audit_template": "templates/skeleton.py"} if seam else {},
        "fills_slots": ["toy-audit"],
        "pack_files": ["templates/skeleton.py"],
        "sweep": [],
    }
    (dom / "sweep.json").write_text(json.dumps(recipe), encoding="utf-8")
    pack_sweep.reseal_manifest(dom / "manifest.json", dom / "sweep.json")
    return hashlib.sha256(_SKELETON).hexdigest()


# --- create mode ------------------------------------------------------------


def test_create_generates_bindable_program_layer(tmp_path: Path) -> None:
    skel_sha = _write_domain(tmp_path)
    res = program_init(
        experiment_dir=tmp_path,
        spec=ProgramInitSpec(program="toy-prog", domain_manifest="packs/toy-domain/manifest.json"),
    )
    assert res.mode == "create"
    # derived_from == {pack, 'audit_template', version, sha256(skeleton bytes)}
    assert (res.derived_from.pack, res.derived_from.seam, res.derived_from.version) == (
        "toy-domain",
        "audit_template",
        "0.2.0",
    )
    assert res.derived_from.sha == skel_sha

    # Template = provenance header + verbatim skeleton bytes.
    tmpl = (tmp_path / "packs" / "toy-prog" / "templates" / "toy-prog_audit.py").read_bytes()
    assert tmpl.endswith(_SKELETON)
    assert b"program-init" in tmpl and b"do NOT hand-edit" in tmpl

    # Manifest sealed in canonical form, carrying derived_from.
    manifest_path = tmp_path / "packs" / "toy-prog" / "manifest.json"
    text = manifest_path.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    pm = pack.load_manifest(manifest_path)
    assert pm.derived_from == pack.DerivedFrom(
        pack="toy-domain", seam="audit_template", version="0.2.0", sha=skel_sha
    )

    # Both packs bound (journal current_bind reads a new sha for each).
    assert {b.pack for b in res.binds} == {"toy-domain", "toy-prog"}
    for name in ("toy-domain", "toy-prog"):
        bind = current_bind(read_decisions(tmp_path, PACK_SUBJECT_KIND, name), pack=name)
        assert bind is not None

    # Opt-in block echoed for the interview to persist (init never writes it).
    assert {e.pack for e in res.packs_optin} == {"toy-domain", "toy-prog"}
    # No caller check → the domain slot reported to-earn.
    assert [(s.slot, s.pack) for s in res.slots_to_earn] == [("toy-audit", "toy-domain")]


def test_create_with_check_runs_and_journals(tmp_path: Path) -> None:
    _write_domain(tmp_path)
    res = program_init(
        experiment_dir=tmp_path,
        spec=ProgramInitSpec(
            program="toy-prog",
            domain_manifest="packs/toy-domain/manifest.json",
            check='python -c "import sys; sys.exit(0)"',
        ),
    )
    assert res.check_ran is True and res.check_ok is True
    log = tmp_path / ".hpc" / "packs" / "toy-prog.checks.jsonl"
    assert log.is_file() and "exit_code" in log.read_text(encoding="utf-8")


def test_create_failing_check_is_reported_not_raised(tmp_path: Path) -> None:
    _write_domain(tmp_path)
    res = program_init(
        experiment_dir=tmp_path,
        spec=ProgramInitSpec(
            program="toy-prog",
            domain_manifest="packs/toy-domain/manifest.json",
            check='python -c "import sys; sys.exit(3)"',
        ),
    )
    assert res.check_ran is True and res.check_ok is False  # reported, not raised


# --- create-mode refusals (fire AND pass) -----------------------------------


def test_create_over_existing_dir_refuses(tmp_path: Path) -> None:
    _write_domain(tmp_path)
    (tmp_path / "packs" / "toy-prog").mkdir(parents=True)
    with pytest.raises(errors.SpecInvalid, match="overwrite|adopt"):
        program_init(
            experiment_dir=tmp_path,
            spec=ProgramInitSpec(
                program="toy-prog", domain_manifest="packs/toy-domain/manifest.json"
            ),
        )


def test_domain_manifest_without_seam_refuses(tmp_path: Path) -> None:
    _write_domain(tmp_path, seam=False)
    with pytest.raises(errors.SpecInvalid, match="audit_template"):
        program_init(
            experiment_dir=tmp_path,
            spec=ProgramInitSpec(
                program="toy-prog", domain_manifest="packs/toy-domain/manifest.json"
            ),
        )


def test_dangling_domain_manifest_refuses(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        program_init(
            experiment_dir=tmp_path,
            spec=ProgramInitSpec(program="toy-prog", domain_manifest="packs/nope/manifest.json"),
        )


def test_contradicting_interview_packs_block_refuses(tmp_path: Path) -> None:
    _write_domain(tmp_path)
    # An interview packs block binds toy-domain to a DIFFERENT manifest relpath.
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "executor_cmd": "run",
                "packs": [{"pack": "toy-domain", "manifest": "packs/elsewhere/manifest.json"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid, match="contradict"):
        program_init(
            experiment_dir=tmp_path,
            spec=ProgramInitSpec(
                program="toy-prog", domain_manifest="packs/toy-domain/manifest.json"
            ),
        )


# --- adopt mode -------------------------------------------------------------


def _write_existing_program(experiment_dir: Path) -> bytes:
    """An existing program pack with a SIGNED template + an unknown recipe key."""
    prog = experiment_dir / "packs" / "toy-prog"
    (prog / "templates").mkdir(parents=True, exist_ok=True)
    signed = b"# SIGNED audit template - do not touch\nRESULT = 42\n"
    (prog / "templates" / "toy-prog_audit.py").write_bytes(signed)
    recipe = {
        "name": "toy-prog",
        "version": "0.2.0",
        "seams": {"audit_template": "templates/toy-prog_audit.py"},
        "fills_slots": [],
        "pack_files": ["templates/toy-prog_audit.py"],
        "sweep": [],
        "lab_extra": "KEEP_ME",
    }
    (prog / "sweep.json").write_text(json.dumps(recipe), encoding="utf-8")
    pack_sweep.reseal_manifest(prog / "manifest.json", prog / "sweep.json")
    return signed


def test_adopt_preserves_signed_template_and_stamps_lineage(tmp_path: Path) -> None:
    skel_sha = _write_domain(tmp_path)
    signed = _write_existing_program(tmp_path)
    signed_path = tmp_path / "packs" / "toy-prog" / "templates" / "toy-prog_audit.py"
    # Bind the program pack first so adopt sees a prior bind (rebound=True).
    pack_bind(
        experiment_dir=tmp_path,
        spec=PackBindSpec(manifest="packs/toy-prog/manifest.json", pack="toy-prog"),
    )
    prior = current_bind(read_decisions(tmp_path, PACK_SUBJECT_KIND, "toy-prog"), pack="toy-prog")
    assert prior is not None

    res = program_init(
        experiment_dir=tmp_path,
        spec=ProgramInitSpec(
            program="toy-prog",
            domain_manifest="packs/toy-domain/manifest.json",
            mode="adopt",
        ),
    )
    assert res.mode == "adopt"
    # The signed template bytes are UNTOUCHED (sign-off preservation guard).
    assert signed_path.read_bytes() == signed
    # derived_from stamped from the on-disk source seam sha.
    assert res.derived_from.sha == skel_sha
    pm = pack.load_manifest(tmp_path / "packs" / "toy-prog" / "manifest.json")
    assert pm.derived_from is not None and pm.derived_from.sha == skel_sha
    # Only the program pack was rebound (domain root untouched — no no-op rebind).
    assert [(b.pack, b.rebound) for b in res.binds] == [("toy-prog", True)]
    new_bind = current_bind(
        read_decisions(tmp_path, PACK_SUBJECT_KIND, "toy-prog"), pack="toy-prog"
    )
    assert new_bind is not None and new_bind.manifest_sha != prior.manifest_sha
    # Unknown recipe key survived the stamp.
    after = json.loads((tmp_path / "packs" / "toy-prog" / "sweep.json").read_text(encoding="utf-8"))
    assert after["lab_extra"] == "KEEP_ME"


def test_adopt_requires_existing_program_dir(tmp_path: Path) -> None:
    _write_domain(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="existing"):
        program_init(
            experiment_dir=tmp_path,
            spec=ProgramInitSpec(
                program="toy-prog",
                domain_manifest="packs/toy-domain/manifest.json",
                mode="adopt",
            ),
        )
