"""Domain-pack manifest model + shape-only seam-declaration loaders.

A *domain pack* is the layer ABOVE core that NAMES what opaque content means
(which callables read data, what a failure pattern implies, which axis a
parameter is, what tolerance a metric deserves). Core never learns any of that
meaning. This module is the bind-as-data substrate (``docs/design/domain-packs.md``,
DP1-DP4): it reads a caller-referenced manifest by relpath + sha, validates the
STRUCTURE of a pack's declarations, and hands typed lists/mappings up — never a
value's meaning.

The boundary this module holds (the Q1 watch list for packs):

* **Shape only, never meaning.** A seam loader checks that a list is a list of
  strings, that a mapping's keys are slugs, that a regex compiles, that an axis
  literal is one of core's EXISTING closed ``DataAxis`` names. It NEVER checks a
  declared value against a semantic ("is this a recognized reader?", "is this
  the ``oom`` pattern?"). The moment a branch reads a declared VALUE for meaning,
  the line is crossed.
* **No default pack, ever.** No manifest, seam file, or vocabulary constant
  ships in core package data or core source. An experiment with no pack behaves
  byte-identically to today. (The clusters.yaml package-data leak is the
  cautionary precedent.)
* **Distribution is invisible.** Core never imports, executes, or interprets a
  manifest-named file beyond these shape-only loaders — no ``importlib``, no
  ``entry_points``, no ``exec``/``eval``. How the pack bytes arrived (pip, git
  submodule, vendored folder, tarball) is DP3-invisible: core resolves a relpath
  and hashes bytes, nothing more.

Pure I/O + structure validation: no ``_wire`` import (the ``ops`` layer owns the
Pydantic boundary), no SSH, no scheduler — the ``state/scopes.py`` posture.

Sha discipline (``docs/internals/harness-contract.md``): the manifest ``files``
integrity set hashes as **raw bytes** (SHA-256, lowercase hex) for EVERY listed
file, templates included. Percent-format ``.py`` audit templates ALSO carry a
normalized-source sha, but that is the notebook gate's separate recompute (S4's
consumer concern); this module's integrity check is always raw-bytes.
"""

from __future__ import annotations

import hashlib
import re
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.experiment_kit.axis import DataAxis
from hpc_agent.state.scopes import validate_tag

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "SEAM_NAMES",
    "AXIS_LITERALS",
    "PackFile",
    "DerivedFrom",
    "PackManifest",
    "sha256_bytes",
    "sha256_file",
    "parse_derived_from",
    "parse_manifest",
    "load_manifest",
    "verify_manifest_integrity",
    "load_reader_calls",
    "load_failure_patterns",
    "load_axis_hints",
    "load_tolerances",
    "load_registration_fields",
    "load_required_receipts",
    "load_seam_declaration",
]

# The CLOSED seam vocabulary — the keys a manifest's ``seams`` mapping may draw
# from (``docs/design/domain-packs.md`` seam table S1-S6). Equality-pinned in
# ``tests/contracts/test_pack_boundary.py`` (the ``DOSSIER_SOURCES`` pattern):
# adding a seam is a reviewed vocabulary change, never ad hoc.
#
#   reader_calls        (S1) executes-live reader vocabularies
#   failure_patterns    (S2) failure-features regex patterns
#   axis_hints          (S3) axis-classification hints
#   audit_template      (S4) the percent-format .py template file itself
#   tolerances          (S5) tolerance defaults
#   registration_fields (S6) registration template field slugs
#
# S6 reading: the seam table's S6 cell lists TWO declarative shapes
# (``registration_fields`` and ``required_receipts``), but only
# ``registration_fields`` is a SEAM NAME (a ``seams`` map key). ``required_receipts``
# is a reserved sibling declaration with its own shape-only loader
# (:func:`load_required_receipts`) but no ``seams`` key — S6 is ONE seam name.
#
# RESERVED future member (do NOT add now): ``actor_policy`` (multi-human MH8),
# which enters via this doc's reviewed-vocabulary process when multi-human lands.
SEAM_NAMES: frozenset[str] = frozenset(
    {
        "reader_calls",
        "failure_patterns",
        "axis_hints",
        "audit_template",
        "tolerances",
        "registration_fields",
    }
)

# Core's EXISTING closed axis vocabulary, derived by IDENTITY from the
# ``DataAxis`` union in :mod:`hpc_agent.experiment_kit.axis` (never a new axis
# vocabulary). An ``axis_hints`` entry's ``axis`` must be one of these names.
AXIS_LITERALS: frozenset[str] = frozenset(t.__name__ for t in typing.get_args(DataAxis))

# A raw-bytes SHA-256 hexdigest: 64 lowercase hex chars (the dossier
# manifest-entry form). Manifest ``files`` shas must match this shape.
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


# --- raw-bytes sha helpers --------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    """Return the 64-char lowercase-hex SHA-256 of *data* (raw bytes).

    The one raw-bytes hashing primitive for pack integrity. No existing helper
    in ``state``/``infra`` is public and general (``ops/export_dossier._sha256_hex``
    and ``infra/transport._sha256_bytes`` are both private, module-local), so
    this is the pack layer's local definition.
    """
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the raw-bytes SHA-256 hexdigest of the file at *path*.

    Raises the underlying ``OSError`` if the file is unreadable — callers that
    must refuse loudly (integrity check) translate it to a named
    :class:`errors.SpecInvalid`.
    """
    return sha256_bytes(path.read_bytes())


# --- manifest model ---------------------------------------------------------


@dataclass(frozen=True)
class PackFile:
    """One entry in a manifest's closed integrity set: a relpath + its raw sha."""

    path: str
    sha256: str


@dataclass(frozen=True)
class DerivedFrom:
    """The lineage stamp on a PROGRAM pack — which domain seam it was consumed from.

    Stamped MECHANICALLY by ``program-init`` at program creation (P1a,
    ``docs/design/program-init.md``), never hand-authored. Identity + freshness
    evidence, never load-bearing selection:

    * ``pack`` — the source DOMAIN pack's ``name`` slug. The derivation EDGE is
      identified by ``{pack, seam}`` matched against a co-bound candidate's
      ``manifest.name`` — NEVER by sha equality (a lab-vs-upstream copy or a
      skeleton upgrade moves the sha but keeps the edge; DC2).
    * ``seam`` — the consumed :data:`SEAM_NAMES` member (``audit_template`` at v1).
    * ``version`` — the source pack's ``version`` string at init time. OPAQUE echo,
      never compared by core (same version discipline as :class:`PackManifest`).
    * ``sha`` — the raw-bytes SHA-256 (64-char lowercase hex) of the seam FILE
      actually consumed at init. Freshness EVIDENCE only: a resolver/status may
      disclose ``lineage behind`` when it differs from the currently-bound source
      seam's sha, but a mismatch NEVER severs the edge (DC1/DC2).
    """

    pack: str
    seam: str
    version: str
    sha: str


def parse_derived_from(data: Any, *, what: str) -> DerivedFrom:
    """Validate a parsed ``derived_from`` block into a :class:`DerivedFrom` (shape only).

    Refuses (loud :class:`errors.SpecInvalid`) on: a non-object block; a non-slug
    ``pack``; a ``seam`` outside :data:`SEAM_NAMES`; an empty/non-string
    ``version``; a ``sha`` that is not 64-char lowercase hex. Never interprets a
    value's meaning — ``pack``/``seam`` are matched by identity downstream,
    ``version``/``sha`` are opaque freshness evidence. *what* names the enclosing
    context for the error message (``pack manifest`` / ``sweep recipe``).
    """
    _require_mapping(data, what=f"{what} 'derived_from'")
    pack = data.get("pack")
    if not isinstance(pack, str):
        raise errors.SpecInvalid(f"{what} derived_from 'pack' must be a string")
    validate_tag(pack)  # slug class — it names a source pack by identity
    seam = data.get("seam")
    if seam not in SEAM_NAMES:
        raise errors.SpecInvalid(
            f"{what} derived_from 'seam' {seam!r} is not a member of the closed seam "
            f"vocabulary {sorted(SEAM_NAMES)}"
        )
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise errors.SpecInvalid(
            f"{what} derived_from 'version' must be a non-empty string (opaque, never "
            f"compared); got {version!r}"
        )
    sha = data.get("sha")
    if not isinstance(sha, str) or not _SHA256_HEX_RE.fullmatch(sha):
        raise errors.SpecInvalid(
            f"{what} derived_from 'sha' must be 64-char lowercase hex (raw-bytes "
            f"SHA-256 of the consumed seam file); got {sha!r}"
        )
    return DerivedFrom(pack=pack, seam=seam, version=version, sha=sha)


@dataclass(frozen=True)
class PackManifest:
    """A pack manifest — identity + pointers only, never interpreted content.

    ``name`` is a slug (it keys the pack journal path). ``version`` is an OPAQUE
    echoed string (core never compares it — ORDERING is the sha's job via bind
    order). ``files`` is the closed integrity set. ``seams`` maps a
    :data:`SEAM_NAMES` member to the relpath of a listed file. ``fills_slots`` is
    an advisory identity list (a pack cannot self-appoint into a gate — DP4).
    """

    name: str
    version: str
    files: tuple[PackFile, ...]
    seams: dict[str, str]
    fills_slots: tuple[str, ...]
    #: The lineage stamp for a PROGRAM pack derived from a domain seam, or ``None``
    #: for a lineage ROOT (a domain pack). Optional + back-compat: a legacy manifest
    #: with no ``derived_from`` key parses to ``None`` and behaves byte-identically.
    derived_from: DerivedFrom | None = None

    def sha_for(self, relpath: str) -> str | None:
        """The recorded raw-bytes sha for a listed file, or ``None`` if unlisted."""
        for f in self.files:
            if f.path == relpath:
                return f.sha256
        return None


def _require_mapping(data: Any, *, what: str) -> Mapping[str, Any]:
    if not isinstance(data, dict):
        raise errors.SpecInvalid(f"{what} must be a JSON object, got {type(data).__name__}")
    return data


def parse_manifest(data: Mapping[str, Any]) -> PackManifest:
    """Validate a parsed manifest dict into a :class:`PackManifest` (shape only).

    Refuses (loud :class:`errors.SpecInvalid`) on: a non-slug ``name``; an empty
    or non-string ``version``; a malformed ``files`` entry or a sha that is not
    64-char lowercase hex; a duplicate file path; a ``seams`` key outside
    :data:`SEAM_NAMES`; a ``seams`` pointer naming a file not in ``files``; a
    non-slug ``fills_slots`` entry. It NEVER interprets a declared value.
    """
    _require_mapping(data, what="pack manifest")

    name = data.get("name")
    if not isinstance(name, str):
        raise errors.SpecInvalid("pack manifest 'name' must be a string")
    validate_tag(name)  # slug class — it keys the pack journal path

    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise errors.SpecInvalid(
            f"pack manifest 'version' must be a non-empty string (opaque, "
            f"never compared); got {version!r}"
        )

    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        raise errors.SpecInvalid("pack manifest 'files' must be a list")
    files: list[PackFile] = []
    seen_paths: set[str] = set()
    for i, entry in enumerate(raw_files):
        if not isinstance(entry, dict):
            raise errors.SpecInvalid(f"pack manifest files[{i}] must be an object")
        path = entry.get("path")
        sha = entry.get("sha256")
        if not isinstance(path, str) or not path:
            raise errors.SpecInvalid(f"pack manifest files[{i}].path must be a non-empty string")
        if not isinstance(sha, str) or not _SHA256_HEX_RE.fullmatch(sha):
            raise errors.SpecInvalid(
                f"pack manifest files[{i}].sha256 must be 64-char lowercase hex "
                f"(raw-bytes SHA-256); got {sha!r} for {path!r}"
            )
        if path in seen_paths:
            raise errors.SpecInvalid(
                f"pack manifest lists duplicate file path {path!r} — the "
                "integrity set must be unambiguous"
            )
        seen_paths.add(path)
        files.append(PackFile(path=path, sha256=sha))

    raw_seams = data.get("seams", {})
    if not isinstance(raw_seams, dict):
        raise errors.SpecInvalid("pack manifest 'seams' must be an object")
    seams: dict[str, str] = {}
    for seam, rel in raw_seams.items():
        if seam not in SEAM_NAMES:
            raise errors.SpecInvalid(
                f"pack manifest declares unknown seam {seam!r}; the seam "
                f"vocabulary is closed: {sorted(SEAM_NAMES)}"
            )
        if not isinstance(rel, str) or not rel:
            raise errors.SpecInvalid(f"pack manifest seams[{seam!r}] must be a file relpath string")
        if rel not in seen_paths:
            raise errors.SpecInvalid(
                f"pack manifest seam {seam!r} points at {rel!r}, which is not a "
                "listed file — every seam pointer must name an entry in 'files'"
            )
        seams[seam] = rel

    raw_slots = data.get("fills_slots", [])
    if not isinstance(raw_slots, list):
        raise errors.SpecInvalid("pack manifest 'fills_slots' must be a list")
    slots: list[str] = []
    for slot in raw_slots:
        if not isinstance(slot, str):
            raise errors.SpecInvalid("pack manifest 'fills_slots' entries must be slug strings")
        validate_tag(slot)
        slots.append(slot)

    # ``derived_from`` is OPTIONAL (a domain pack is a lineage root and omits it).
    # Absent → None (back-compat: legacy manifests parse identically). Present →
    # full shape validation, malformed → loud SpecInvalid (never a silent drop).
    raw_derived = data.get("derived_from")
    derived_from = (
        None if raw_derived is None else parse_derived_from(raw_derived, what="pack manifest")
    )

    return PackManifest(
        name=name,
        version=version,
        files=tuple(files),
        seams=seams,
        fills_slots=tuple(slots),
        derived_from=derived_from,
    )


def load_manifest(manifest_path: Path) -> PackManifest:
    """Read + parse a manifest ``.json`` at *manifest_path*.

    A missing/unreadable file or non-JSON content is a broken opted-in setup —
    a loud :class:`errors.SpecInvalid` naming the path (the
    ``ops/notebook_gate._read_required_py`` posture), never a silent pass.
    """
    import json

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"pack manifest {str(manifest_path)!r} is unreadable ({exc}); an "
            "opted-in repo with a missing/unreadable manifest is broken, not a "
            "silent pass"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise errors.SpecInvalid(
            f"pack manifest {str(manifest_path)!r} is not valid JSON ({exc})"
        ) from exc
    return parse_manifest(data)


def verify_manifest_integrity(pack_root: Path, manifest: PackManifest) -> None:
    """Recompute every listed file's raw-bytes sha under *pack_root*; refuse on drift.

    *pack_root* is the directory the manifest's ``files`` relpaths resolve
    against (the manifest's own parent dir, the pack-relative layout). A missing
    file or a sha mismatch is a loud :class:`errors.SpecInvalid` naming the path
    and both shas — the drift-revocation the whole design earns. Raw bytes for
    EVERY file, templates included (S4's normalized-source sha is the notebook
    gate's separate recompute).
    """
    for f in manifest.files:
        target = pack_root / f.path
        try:
            actual = sha256_file(target)
        except (OSError, UnicodeDecodeError) as exc:
            raise errors.SpecInvalid(
                f"pack {manifest.name!r}: listed file {f.path!r} is unreadable "
                f"({exc}); a dangling manifest reference is a broken setup, not a "
                "silent pass"
            ) from exc
        if actual != f.sha256:
            raise errors.SpecInvalid(
                f"pack {manifest.name!r}: file {f.path!r} sha mismatch — manifest "
                f"records {f.sha256}, on-disk content is {actual}. Editing pack "
                "content without re-binding revokes every clearance signed under "
                "the old sha."
            )


# --- shape-only seam-declaration loaders ------------------------------------
#
# Each loader takes an ALREADY-PARSED JSON value (T7's resolver reads the file
# and json.loads it, keeping file I/O out of this shape layer) plus the *source*
# relpath for error messages. Every loader validates STRUCTURE only. A seam file
# failing shape validation in a bound pack is a loud SpecInvalid naming the file.


def load_reader_calls(data: Any, *, source: str) -> list[str]:
    """S1: a list of dotted callable-name strings (e.g. ``widgets.load_widget``).

    Shape only — the lint later matches these by NAME identity; core never
    learns what a reader does.
    """
    if not isinstance(data, list):
        raise errors.SpecInvalid(f"reader_calls seam {source!r} must be a JSON list")
    out: list[str] = []
    for i, item in enumerate(data):
        if not isinstance(item, str) or not item:
            raise errors.SpecInvalid(
                f"reader_calls seam {source!r}[{i}] must be a non-empty dotted-name string"
            )
        out.append(item)
    return out


def load_failure_patterns(data: Any, *, source: str) -> dict[str, str]:
    """S2: a mapping of slug pattern-ids to regex strings (each must COMPILE).

    The compile is a SHAPE check (a regex is well-formed), never a meaning check.
    Core counts hits and records the ids as evidence — it never maps a hit to a
    category or an action.
    """
    _require_mapping(data, what=f"failure_patterns seam {source!r}")
    out: dict[str, str] = {}
    for key, pattern in data.items():
        validate_tag(key)
        if not isinstance(pattern, str):
            raise errors.SpecInvalid(
                f"failure_patterns seam {source!r}[{key!r}] must be a regex string"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise errors.SpecInvalid(
                f"failure_patterns seam {source!r}[{key!r}] is not a valid regex ({exc})"
            ) from exc
        out[key] = pattern
    return out


def load_axis_hints(data: Any, *, source: str) -> list[dict[str, str]]:
    """S3: a list of ``{pattern: <regex>, axis: <core DataAxis literal>}`` hints.

    ``pattern`` must compile (shape); ``axis`` must be one of :data:`AXIS_LITERALS`
    (identity against core's EXISTING closed vocabulary — never a new axis kind).
    A hint only ever adds caution downstream; it can never auto-resolve an axis.
    """
    if not isinstance(data, list):
        raise errors.SpecInvalid(f"axis_hints seam {source!r} must be a JSON list")
    out: list[dict[str, str]] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise errors.SpecInvalid(f"axis_hints seam {source!r}[{i}] must be an object")
        extra = set(entry) - {"pattern", "axis"}
        if extra:
            raise errors.SpecInvalid(
                f"axis_hints seam {source!r}[{i}] has unexpected keys {sorted(extra)}; "
                "an entry is exactly {pattern, axis}"
            )
        pattern = entry.get("pattern")
        axis = entry.get("axis")
        if not isinstance(pattern, str) or not pattern:
            raise errors.SpecInvalid(
                f"axis_hints seam {source!r}[{i}].pattern must be a non-empty regex string"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise errors.SpecInvalid(
                f"axis_hints seam {source!r}[{i}].pattern is not a valid regex ({exc})"
            ) from exc
        if axis not in AXIS_LITERALS:
            raise errors.SpecInvalid(
                f"axis_hints seam {source!r}[{i}].axis {axis!r} is not a core "
                f"DataAxis literal {sorted(AXIS_LITERALS)}; a pack never declares a "
                "new axis vocabulary"
            )
        out.append({"pattern": pattern, "axis": axis})
    return out


def load_tolerances(data: Any, *, source: str) -> dict[str, float]:
    """S5: a mapping of slug tolerance-ids to plain numbers.

    Shape only — the number flows to the fingerprint precedence seam as its own
    labeled tier (the consumer's concern). Booleans are refused (``bool`` is an
    ``int`` subclass but is not a tolerance).
    """
    _require_mapping(data, what=f"tolerances seam {source!r}")
    out: dict[str, float] = {}
    for key, value in data.items():
        validate_tag(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise errors.SpecInvalid(
                f"tolerances seam {source!r}[{key!r}] must be a plain number, got {value!r}"
            )
        out[key] = value
    return out


def _load_slug_list(data: Any, *, seam: str, source: str) -> list[str]:
    if not isinstance(data, list):
        raise errors.SpecInvalid(f"{seam} seam {source!r} must be a JSON list")
    out: list[str] = []
    for i, item in enumerate(data):
        if not isinstance(item, str):
            raise errors.SpecInvalid(f"{seam} seam {source!r}[{i}] must be a slug string")
        validate_tag(item)
        out.append(item)
    return out


def load_registration_fields(data: Any, *, source: str) -> list[str]:
    """S6: a list of registration field slugs. Shape only, RESERVED — no consumer.

    The future registration kernel counts presence; core never interprets a
    field's meaning.
    """
    return _load_slug_list(data, seam="registration_fields", source=source)


def load_required_receipts(data: Any, *, source: str) -> list[str]:
    """S6 sibling: a list of required-receipt slot slugs. Shape only, RESERVED.

    Not a :data:`SEAM_NAMES` member (S6 is one seam name); this loads the
    reserved manifest-list declaration the registration kernel will count.
    """
    return _load_slug_list(data, seam="required_receipts", source=source)


# The content-bearing seams that have a declaration loader. ``audit_template``
# is deliberately absent: its declaration IS a percent-format ``.py`` file the
# notebook gate consumes, not a shape core parses here.
_SEAM_LOADERS = {
    "reader_calls": load_reader_calls,
    "failure_patterns": load_failure_patterns,
    "axis_hints": load_axis_hints,
    "tolerances": load_tolerances,
    "registration_fields": load_registration_fields,
}


def load_seam_declaration(seam: str, data: Any, *, source: str) -> Any:
    """Dispatch to the shape-only loader for *seam* over already-parsed *data*.

    Refuses an unknown seam and ``audit_template`` (which has no data loader —
    it is a ``.py`` file the notebook gate consumes). T7's resolver reads the
    seam file and calls this.
    """
    loader = _SEAM_LOADERS.get(seam)
    if loader is None:
        if seam == "audit_template":
            raise errors.SpecInvalid(
                "audit_template has no seam-declaration loader; it is a .py "
                "template file the notebook gate consumes directly"
            )
        raise errors.SpecInvalid(
            f"unknown seam {seam!r}; the seam vocabulary is closed: {sorted(SEAM_NAMES)}"
        )
    return loader(data, source=source)
