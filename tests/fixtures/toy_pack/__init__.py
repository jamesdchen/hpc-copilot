"""The TOY-WIDGETS pack fixture — the F10 first-consumer, materialized for tests.

This is the test-side companion to ``examples/packs/toy-widgets/`` (the human-
facing example pack). It does two jobs:

* :func:`rebuild_manifest` — recompute every listed file's raw-bytes sha and write
  ``manifest.json``. This is a PACK AUTHOR'S build step (a pack ships correct shas
  the way ``export_dossier`` seals correct shas); it generated the committed
  example manifest, and the integration test calls it again after editing a pack
  file to model the honest "edit standards -> regenerate manifest -> re-bind" flow.
* :func:`build_toy_pack` — copy the example pack VERBATIM into a writable temp dir
  so a test can bind it, then edit a file to drive drift-revocation. Copying the
  committed example (never regenerating it) means a green integration test also
  proves the shipped example pack is itself self-consistent — binds clean, every
  seam loads.

Toy-domain vocabulary only (``widgets``/``widget-audit``/``widget-jam``) — never a
real domain's words (the toy-domain fixture rule: real domain words in the tree
would smuggle a vocabulary greps and future maintainers mistake for core
knowledge).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

#: The committed example pack this fixture mirrors.
EXAMPLE_PACK_ROOT = Path(__file__).resolve().parents[3] / "examples" / "packs" / "toy-widgets"

PACK_NAME = "toy-widgets"
PACK_VERSION = "1.0.0"
SLOT = "widget-audit"

#: The closed integrity set: every pack file's relpath (manifest.json excepted —
#: a manifest never lists itself; its own sha IS the pack identity). Enumerated
#: explicitly (not globbed) so the manifest is stable and reviewable.
FILE_RELPATHS: tuple[str, ...] = (
    "vocab/readers.json",
    "patterns/failures.json",
    "axes/hints.json",
    "tolerances/tols.json",
    "registration/fields.json",
    "templates/widget_audit.py",
    "check/check_widgets.py",
)

#: seam name -> declaration-file relpath (keys drawn from the closed SEAM_NAMES
#: vocabulary; every seam exercised end to end). ``audit_template`` points at
#: the percent-format .py the notebook gate consumes; the rest are shape-loadable.
SEAMS: dict[str, str] = {
    "reader_calls": "vocab/readers.json",
    "failure_patterns": "patterns/failures.json",
    "axis_hints": "axes/hints.json",
    "tolerances": "tolerances/tols.json",
    "registration_fields": "registration/fields.json",
    "audit_template": "templates/widget_audit.py",
}

FILLS_SLOTS: tuple[str, ...] = (SLOT,)

#: The template relpath a caller-side check covers (the check script's target).
TEMPLATE_RELPATH = "templates/widget_audit.py"


def _raw_sha(path: Path) -> str:
    """Raw-bytes SHA-256 hexdigest of *path* (the pack integrity form)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest_dict(pack_root: Path) -> dict[str, Any]:
    """The manifest dict for the pack rooted at *pack_root* (shas recomputed)."""
    files = [{"path": rel, "sha256": _raw_sha(pack_root / rel)} for rel in sorted(FILE_RELPATHS)]
    return {
        "name": PACK_NAME,
        "version": PACK_VERSION,
        "files": files,
        "seams": dict(SEAMS),
        "fills_slots": list(FILLS_SLOTS),
    }


def rebuild_manifest(pack_root: Path) -> Path:
    """Write ``manifest.json`` under *pack_root* from the on-disk file shas.

    Deterministic bytes (``sort_keys``, trailing newline) so the committed example
    manifest is stable across regenerations. Returns the manifest path.
    """
    manifest = build_manifest_dict(pack_root)
    path = pack_root / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_toy_pack(experiment_dir: Path, *, subdir: str = "packs/toy-widgets") -> str:
    """Copy the example toy pack VERBATIM under *experiment_dir*; return its relpath.

    The returned relpath points at the copied ``manifest.json`` (e.g.
    ``packs/toy-widgets/manifest.json``) — exactly what an interview ``packs``
    opt-in / a ``pack-bind`` spec references. The copy is writable, so a test can
    edit a pack file to drive drift-revocation; the SOURCE example is never
    mutated.
    """
    dest = experiment_dir / subdir
    shutil.copytree(EXAMPLE_PACK_ROOT, dest)
    return f"{subdir}/manifest.json"
