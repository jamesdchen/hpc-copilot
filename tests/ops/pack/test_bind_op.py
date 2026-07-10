"""Direct-atom tests for the ``pack-bind`` mutate primitive (domain-packs T4).

Builds a toy-widgets pack on disk (manifest + listed files), calls the verb, and
asserts:

* a happy bind journals the CODE attestation with the right record shape, the
  attestation validates, and the result echoes what was bound;
* ``response`` is the mechanical ``"bound"`` (never a human-ack token);
* any file sha drift is refused loudly (``spec_invalid``);
* a missing/unreadable manifest is refused loudly (dangling reference);
* a ``pack`` cross-check mismatch is refused loudly;
* a re-bind at a new manifest sha is just a newer record — T2's ``current_bind``
  reduces the OLD bind stale and reports the new one as in force.

Toy-domain vocabulary only (``toy-widgets``, ``widgets.load_widget``) — never a
real domain's, per the fixture rule.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, cast

import pytest

import hpc_agent.state.pack as pack
from hpc_agent import errors
from hpc_agent._wire.actions.pack_bind import PackBindResult, PackBindSpec
from hpc_agent.ops.pack import bind_op
from hpc_agent.ops.pack.bind_op import pack_bind
from hpc_agent.state import attestation
from hpc_agent.state.pack_receipts import (
    PACK_BIND_BLOCK,
    PACK_SUBJECT_KIND,
    current_bind,
)

if TYPE_CHECKING:
    from pathlib import Path

_PACK_NAME = "toy-widgets"
_READERS = '["widgets.load_widget"]'
_MANIFEST_REL = "packs/toy-widgets/manifest.json"


def _sha(text: str) -> str:
    digest: str = pack.sha256_bytes(text.encode("utf-8"))
    return digest


def _write_pack(
    experiment_dir: Path,
    *,
    name: str = _PACK_NAME,
    version: str = "1.2.0",
    readers: str = _READERS,
) -> Path:
    """Materialise a toy pack; return the manifest relpath under *experiment_dir*."""
    pack_dir = experiment_dir / "packs" / "toy-widgets"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "vocab").mkdir(exist_ok=True)
    readers_rel = "vocab/readers.json"
    (pack_dir / readers_rel).write_text(readers, encoding="utf-8")
    manifest = {
        "name": name,
        "version": version,
        "files": [{"path": readers_rel, "sha256": _sha(readers)}],
        "seams": {"reader_calls": readers_rel},
        "fills_slots": ["widget-audit"],
    }
    (pack_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir / "manifest.json"


def _bind(experiment_dir: Path, *, expect_pack: str | None = None) -> PackBindResult:
    return pack_bind(
        experiment_dir=experiment_dir,
        spec=PackBindSpec.model_validate(
            {"manifest": _MANIFEST_REL, **({"pack": expect_pack} if expect_pack else {})}
        ),
    )


def _records(experiment_dir: Path, name: str = _PACK_NAME) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", bind_op._read_pack_records(experiment_dir, name))


# --- happy path -------------------------------------------------------------


def test_happy_bind_records_shape_and_echoes(tmp_path: Path) -> None:
    manifest_path = _write_pack(tmp_path)
    manifest_sha = pack.sha256_file(manifest_path)

    result = _bind(tmp_path)

    # Result echoes what was bound.
    assert result.pack == _PACK_NAME
    assert result.version == "1.2.0"
    assert result.manifest_sha == manifest_sha
    assert [f.path for f in result.files] == ["vocab/readers.json"]
    assert result.files[0].sha256 == _sha(_READERS)
    assert result.seams == ["reader_calls"]

    # One journaled record, with the design's shape.
    records = _records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["scope_kind"] == PACK_SUBJECT_KIND
    assert rec["scope_id"] == _PACK_NAME
    assert rec["block"] == PACK_BIND_BLOCK
    resolved = rec["resolved"]
    assert resolved["pack"] == _PACK_NAME
    assert resolved["version"] == "1.2.0"
    assert resolved["manifest_sha"] == manifest_sha
    assert resolved["files"] == [{"path": "vocab/readers.json", "sha256": _sha(_READERS)}]
    assert resolved["seams"] == ["reader_calls"]


def test_response_is_the_mechanical_bound(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    _bind(tmp_path)
    assert _records(tmp_path)[0]["response"] == "bound"


def test_bind_projects_to_a_valid_code_attestation(tmp_path: Path) -> None:
    manifest_path = _write_pack(tmp_path)
    manifest_sha = pack.sha256_file(manifest_path)
    _bind(tmp_path)

    # The current bind reduces CURRENT against its own manifest sha (routes
    # through the ONE kernel via T2's current_bind).
    cur = current_bind(_records(tmp_path))
    assert cur is not None
    assert cur.pack == _PACK_NAME
    assert cur.manifest_sha == manifest_sha
    assert cur.version == "1.2.0"

    # And the projected attestation validates as a CODE attestation.
    att = attestation.validate(
        {
            "attestor": "code",
            "subject_kind": PACK_SUBJECT_KIND,
            "subject_id": cur.pack,
            "content_sha": cur.manifest_sha,
        }
    )
    assert att.attestor == "code"
    assert att.subject_id == _PACK_NAME


# --- refusals (each fires on a synthetic violation) -------------------------


def test_sha_mismatch_refused(tmp_path: Path) -> None:
    manifest_path = _write_pack(tmp_path)
    # Edit a listed file AFTER the manifest recorded its sha → on-disk drift.
    (manifest_path.parent / "vocab" / "readers.json").write_text(
        '["widgets.load_widget", "widgets.tampered"]', encoding="utf-8"
    )
    with pytest.raises(errors.SpecInvalid, match="sha mismatch"):
        _bind(tmp_path)
    # Nothing journaled on a refused bind.
    assert _records(tmp_path) == []


def test_dangling_manifest_refused(tmp_path: Path) -> None:
    (tmp_path / "packs" / "toy-widgets").mkdir(parents=True)
    # No manifest.json written → dangling reference.
    with pytest.raises(errors.SpecInvalid):
        _bind(tmp_path)


def test_missing_listed_file_refused(tmp_path: Path) -> None:
    manifest_path = _write_pack(tmp_path)
    (manifest_path.parent / "vocab" / "readers.json").unlink()
    with pytest.raises(errors.SpecInvalid):
        _bind(tmp_path)


def test_pack_cross_check_mismatch_refused(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="cross-check"):
        _bind(tmp_path, expect_pack="some-other-pack")
    assert _records(tmp_path) == []


def test_pack_cross_check_match_binds(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    result = _bind(tmp_path, expect_pack=_PACK_NAME)
    assert result.pack == _PACK_NAME


# --- re-bind = drift --------------------------------------------------------


def test_rebind_at_new_sha_is_a_newer_record(tmp_path: Path) -> None:
    _write_pack(tmp_path, version="1.0.0")
    first = _bind(tmp_path)

    # Re-author the pack (new reader vocab + fresh manifest shas) and re-bind.
    manifest_path = _write_pack(
        tmp_path, version="2.0.0", readers='["widgets.load_widget", "widgets.load_gadget"]'
    )
    second_sha = pack.sha256_file(manifest_path)
    second = _bind(tmp_path)

    assert second.manifest_sha != first.manifest_sha
    assert second.manifest_sha == second_sha

    # Two records; the NEWER bind is the one in force (T2 reduces the old stale).
    records = _records(tmp_path)
    assert len(records) == 2
    cur = current_bind(records)
    assert cur is not None
    assert cur.manifest_sha == second_sha
    assert cur.version == "2.0.0"


# --- CRLF-vs-sha-seal translation disclosure --------------------------------
#
# The seal hashes raw bytes; git eol translation on a future checkout would
# silently move those bytes and revoke every clearance. ``pack-bind`` discloses
# the exposure — NEVER blocks (the bytes are sealed as they are now).


_GIT = shutil.which("git")
_requires_git = pytest.mark.skipif(_GIT is None, reason="git binary not on PATH")


def _git_init(root: Path) -> None:
    assert _GIT is not None  # guarded by _requires_git
    for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run([_GIT, *args], cwd=str(root), check=True, capture_output=True)


@_requires_git
def test_disclosure_warns_when_file_exposed_to_translation(tmp_path: Path) -> None:
    """A sealed file git would eol-translate (text attr not pinned `-text`)
    surfaces a NEVER-blocking WARNING naming the file + the `.gitattributes`
    remedy — but the bind still succeeds and journals."""
    _git_init(tmp_path)
    # Force text=set so the file is unambiguously exposed regardless of the
    # host's global git config.
    (tmp_path / ".gitattributes").write_text("* text\n", encoding="utf-8")
    _write_pack(tmp_path)

    result = _bind(tmp_path)

    # The bind still succeeded and journaled (disclosure is never a blocker).
    assert result.pack == _PACK_NAME
    assert len(_records(tmp_path)) == 1

    assert result.translation_exposed_files == ["vocab/readers.json"]
    assert result.translation_disclosure is not None
    assert "vocab/readers.json" in result.translation_disclosure
    assert "-text" in result.translation_disclosure


@_requires_git
def test_disclosure_silent_when_file_pinned(tmp_path: Path) -> None:
    """A file pinned `-text` (raw bytes, no translation) yields no warning."""
    _git_init(tmp_path)
    (tmp_path / ".gitattributes").write_text("* -text\n", encoding="utf-8")
    _write_pack(tmp_path)

    result = _bind(tmp_path)

    assert result.translation_exposed_files == []
    assert result.translation_disclosure is None


def test_disclosure_unknown_when_no_git_repo(tmp_path: Path) -> None:
    """No git repo (or no git binary) → a DISCLOSED 'unknown' line, never
    silence (disclosure-or-refusal). tmp_path is not a git repo."""
    _write_pack(tmp_path)

    result = _bind(tmp_path)

    # Bind succeeds regardless.
    assert result.pack == _PACK_NAME
    assert result.translation_exposed_files == []
    assert result.translation_disclosure is not None
    assert "unknown" in result.translation_disclosure.lower()
