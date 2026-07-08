"""The ONE domain-pack seam-declaration resolver (T7).

Design origin: ``docs/design/domain-packs.md`` (the seam table S1-S6, "The bind
event", the D7/dangling split). This is the single resolver every seam consumer
calls so it stays pack-IGNORANT: ``notebook-lint`` (T9a) receives a
``reader_calls`` list the way it already receives ``input_roots``; the
failure-features glue (T9b) receives failure patterns; the axis classifier (T9c)
receives axis hints; the fingerprint precedence seam receives tolerances. None of
them learns a pack exists — they consume typed opaque lists/mappings, each
carrying the ``{pack, version, sha}`` echo core copies verbatim and never reads
back for meaning.

**Why this lives in ``state/`` (drift-log item 2).** Its named consumers sit in
DIFFERENT ops subjects (``ops/notebook/lint.py`` = the ``notebook`` subject,
``ops/recover/features_glue.py`` = the ``recover`` subject), and
``scripts/lint_subject_imports.py`` forbids either from importing an ``ops/pack/``
module — subjects compose only through the ``state``/``infra`` substrate. The
resolver is pure I/O + reduction (opt-in read, ``state/pack.py`` loaders,
``state/pack_receipts.py::current_bind``), so ``state`` placement is natural.

**The resolution, per opted-in pack (the dangling-reference posture).**

1. Read the ``packs`` opt-in block off ``interview.json``. ABSENT → the D7
   silence: an empty result and ZERO further filesystem probes. A repo that never
   opted in never pays.
2. Resolve the manifest relpath (loud :class:`errors.SpecInvalid` if
   missing/unreadable — an opted-in dangling manifest is a broken setup, never a
   silent pass), and require a CURRENT bind over the pack's journal records
   (:func:`~hpc_agent.state.pack_receipts.current_bind`; no current bind = a
   dangling ``receipt_bindings`` reference = loud).
3. Verify on-disk integrity AGAINST THE BIND'S recorded shas (the
   ``ops/notebook_gate._linked_source_drift`` pattern): the manifest's own
   raw-bytes sha must still equal the bind's ``manifest_sha`` (a re-generated
   manifest reads as drift), and every listed file's on-disk raw sha must still
   equal its recorded sha (a changed-on-disk pack file reads as drift even before
   any re-bind). Drift → loud, the drift-revocation the whole design earns.
4. Load each declared seam file via ``state/pack.py``'s shape-only loaders and
   return TYPED opaque declarations, each stamped with the ``{pack, version,
   sha}`` echo (``sha`` = the bind's ``manifest_sha``).

Pure I/O + reduction: no ``importlib``/``exec``/``eval`` (DP2/DP3 — pack code
never runs in core, distribution is invisible). No consumer wiring lands here —
T9a/T9b/T9c are Wave C hot files that CALL this and stay pack-ignorant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state.pack import (
    SEAM_NAMES,
    load_manifest,
    load_seam_declaration,
    sha256_file,
    verify_manifest_integrity,
)
from hpc_agent.state.pack_receipts import current_bind

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from hpc_agent.state.pack import PackManifest

__all__ = [
    "PackEcho",
    "ReaderCallsDecl",
    "FailurePatternsDecl",
    "AxisHintsDecl",
    "TolerancesDecl",
    "RegistrationFieldsDecl",
    "PackDeclarations",
    "resolve_reader_calls",
    "resolve_failure_patterns",
    "resolve_axis_hints",
    "resolve_tolerances",
    "resolve_registration_fields",
    "resolve_declarations",
    "resolve_template_pack_echo",
]

# The seams that carry a shape-loadable declaration file. ``audit_template`` is a
# ``SEAM_NAMES`` member but has NO data loader — it is a percent-format ``.py``
# the notebook gate consumes directly (S4), so it never appears in a declaration.
_LOADABLE_SEAMS: frozenset[str] = SEAM_NAMES - {"audit_template"}


# --- the echo every declaration carries -------------------------------------


@dataclass(frozen=True)
class PackEcho:
    """The opaque ``{pack, version, sha}`` echo stamped on every pack-sourced record.

    * ``pack`` — the pack name (the bind's ``subject_id``).
    * ``version`` — the opaque version string the bind recorded; core echoes it,
      never compares it (ORDERING is the sha's job via bind order). ``None`` when
      the bind recorded no version.
    * ``sha`` — the bind's ``manifest_sha``: the identity of the standards in
      force. A re-bind moves it and revokes everything signed under the old one.

    Core copies this verbatim onto the consuming record; it never reads it back
    for meaning (identity only — the ``reproduces`` field precedent).
    """

    pack: str
    version: str | None
    sha: str

    def as_dict(self) -> dict[str, str | None]:
        """The ``{pack, version, sha}`` mapping a consumer stamps onto its record."""
        return {"pack": self.pack, "version": self.version, "sha": self.sha}


# --- typed per-seam declarations (opaque payloads + echo) -------------------


@dataclass(frozen=True)
class ReaderCallsDecl:
    """S1: dotted callable-name strings the lint matches by NAME identity."""

    names: tuple[str, ...]
    echo: PackEcho


@dataclass(frozen=True)
class FailurePatternsDecl:
    """S2: ``{pattern_id: regex}`` core COMPILES and counts hits of (never maps)."""

    patterns: dict[str, str]
    echo: PackEcho


@dataclass(frozen=True)
class AxisHintsDecl:
    """S3: ``[{pattern, axis}]`` hints that add caution, never clearance."""

    hints: tuple[dict[str, str], ...]
    echo: PackEcho


@dataclass(frozen=True)
class TolerancesDecl:
    """S5: ``{tolerance_id: number}`` id->value resolution.

    The number flows to the determinism-fingerprint precedence seam as its OWN
    labeled ``S5 pack default`` tier. The consumer wiring is DELIBERATELY NOT here:
    a value pre-folded into the existing caller-owned-tolerance path would be
    indistinguishable from a caller override and would outrank a measured
    envelope — the exact precedence inversion the fingerprint's row forbids
    (``docs/design/domain-packs.md`` S5, corrected 2026-07-07). This resolver
    ships the shape-only id->value map + echo; nothing consumes it yet.
    """

    tolerances: dict[str, float]
    echo: PackEcho


@dataclass(frozen=True)
class RegistrationFieldsDecl:
    """S6: registration field slugs. RESERVED — the future kernel counts presence."""

    fields: tuple[str, ...]
    echo: PackEcho


@dataclass(frozen=True)
class PackDeclarations:
    """Every opted-in pack's declarations, grouped by seam.

    One :func:`resolve_declarations` pass builds all five lists (each accessor
    below is the same pass filtered to one seam). A list holds one entry per
    opted-in pack that DECLARES that seam; a pack silent on a seam contributes
    nothing to its list.
    """

    reader_calls: tuple[ReaderCallsDecl, ...]
    failure_patterns: tuple[FailurePatternsDecl, ...]
    axis_hints: tuple[AxisHintsDecl, ...]
    tolerances: tuple[TolerancesDecl, ...]
    registration_fields: tuple[RegistrationFieldsDecl, ...]


# --- internal: the one resolution pass --------------------------------------


@dataclass(frozen=True)
class _ResolvedPack:
    """One opted-in pack, verified current, with its declared seams loaded."""

    echo: PackEcho
    declarations: dict[str, Any]  # seam name -> loaded shape-only value


def _read_packs_optin(experiment_dir: Path) -> list[dict[str, Any]]:
    """The interview.json ``packs`` opt-in list, or ``[]`` when not opted in.

    # T8a seam: the ``packs`` field lands on ``InterviewSpec`` in Wave C; until
    # then this reads it as a raw dict by its documented shape
    # (``[{pack, manifest, receipt_bindings}]``). Mirrors
    # ``ops/notebook_gate._read_audited_source``: a missing/corrupt/non-object
    # interview.json, or an absent ``packs`` key, reads as "not opted in" → the D7
    # silent empty. This is the ONLY filesystem probe on the not-opted-in path.

    A PRESENT-but-malformed ``packs`` block (not a list) is an opted-in-but-broken
    setup → loud :class:`errors.SpecInvalid`, never a silent pass.
    """
    for rel in ("interview.json", ".hpc/interview.json"):
        path = experiment_dir / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        if "packs" not in doc:
            return []
        block = doc["packs"]
        if not isinstance(block, list):
            raise errors.SpecInvalid(
                "interview.json 'packs' opt-in block must be a list of "
                "{pack, manifest, receipt_bindings} objects; an opted-in repo "
                "with a malformed block is broken, not a silent pass"
            )
        return [e for e in block if isinstance(e, dict)]
    return []


def _read_json_file(path: Path, *, what: str) -> Any:
    """Read + ``json.loads`` a bound pack file, or refuse LOUDLY.

    An opted-in pack's seam file that is missing/unreadable/non-JSON is a broken
    setup (the ``_read_required_py`` posture) — never a silent pass.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"{what} {str(path)!r} is unreadable ({exc}); an opted-in pack with a "
            "missing/unreadable seam file is broken, not a silent pass"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise errors.SpecInvalid(f"{what} {str(path)!r} is not valid JSON ({exc})") from exc


def _verify_against_bind(
    manifest_path: Path, manifest: PackManifest, *, pack: str, manifest_sha: str
) -> None:
    """Refuse if the on-disk pack drifted from the CURRENT bind's recorded shas.

    Two drifts, both loud (the ``_linked_source_drift`` pattern anchored to the
    bind): the manifest's own raw-bytes sha must still equal the bind's
    ``manifest_sha`` (a re-generated manifest is drift), and — since a matching
    manifest sha means ``manifest.files`` ARE the bind's recorded files — every
    listed file's on-disk raw sha must still equal its recorded sha (a changed
    pack file is drift even before any re-bind).
    """
    disk_manifest_sha = sha256_file(manifest_path)
    if disk_manifest_sha != manifest_sha:
        raise errors.SpecInvalid(
            f"pack {pack!r}: manifest on disk ({disk_manifest_sha}) no longer "
            f"matches the current bind ({manifest_sha}). Editing pack standards "
            "without re-binding revokes every clearance signed under the old sha."
        )
    verify_manifest_integrity(manifest_path.parent, manifest)


def _resolve_packs(
    experiment_dir: Path,
    *,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None,
) -> list[_ResolvedPack]:
    """Read the opt-in, verify each pack current, and load its declared seams.

    The ONE pass every public accessor derives from. Empty opt-in → ``[]`` with
    zero probes beyond interview.json.
    """
    optin = _read_packs_optin(experiment_dir)
    if not optin:
        return []

    resolved: list[_ResolvedPack] = []
    for entry in optin:
        pack_name = entry.get("pack")
        manifest_rel = entry.get("manifest")
        if not isinstance(pack_name, str) or not pack_name:
            raise errors.SpecInvalid(
                "interview.json 'packs' entry is missing a string 'pack' name; an "
                "opted-in repo with a malformed entry is broken, not a silent pass"
            )
        if not isinstance(manifest_rel, str) or not manifest_rel:
            raise errors.SpecInvalid(
                f"pack {pack_name!r}: 'packs' entry declares no 'manifest' relpath — "
                "a dangling opt-in reference is broken, not a silent pass"
            )

        manifest_path = experiment_dir / manifest_rel
        manifest = load_manifest(manifest_path)  # loud on missing/unreadable/bad JSON
        if manifest.name != pack_name:
            raise errors.SpecInvalid(
                f"pack opt-in names {pack_name!r} but manifest {manifest_rel!r} "
                f"declares {manifest.name!r} — the reference is dangling/mismatched"
            )

        records = _records_for(pack_name, records_by_pack, records_reader)
        bind = current_bind(records, pack=pack_name)
        if bind is None:
            raise errors.SpecInvalid(
                f"pack {pack_name!r}: opted in but has no CURRENT bind — a dangling "
                "receipt/pack reference is loud, never a silent pass (bind the pack "
                "via pack-bind before opting in)"
            )

        _verify_against_bind(
            manifest_path, manifest, pack=pack_name, manifest_sha=bind.manifest_sha
        )

        echo = PackEcho(pack=pack_name, version=bind.version, sha=bind.manifest_sha)
        declarations: dict[str, Any] = {}
        for seam, rel in manifest.seams.items():
            if seam not in _LOADABLE_SEAMS:
                continue  # audit_template: a .py the notebook gate consumes, no loader
            data = _read_json_file(
                manifest_path.parent / rel, what=f"pack {pack_name!r} seam {seam}"
            )
            declarations[seam] = load_seam_declaration(seam, data, source=rel)
        resolved.append(_ResolvedPack(echo=echo, declarations=declarations))
    return resolved


def _records_for(
    pack: str,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None,
) -> Sequence[Mapping[str, Any]]:
    """The pack journal records for *pack*.

    # T8 seam: once ``state/decision_journal.py`` gains the ``"pack"`` scope kind
    # (Wave C), callers pass ``records_reader=lambda name:
    # read_decisions(experiment_dir, "pack", name)``. Until then the records are
    # supplied directly via *records_by_pack* (a ``{pack: records}`` mapping),
    # keeping this resolver importable/testable standalone ahead of T8. A pack
    # with no supplied records has no current bind → the loud dangling refusal.
    """
    if records_reader is not None:
        return records_reader(pack)
    if records_by_pack is not None:
        return records_by_pack.get(pack, ())
    return ()


def _abs(path: Path) -> Path:
    """Best-effort absolute-resolve for identity comparison; unresolvable → as-is."""
    try:
        return path.resolve()
    except OSError:
        return path


def _read_pack_journal(experiment_dir: Path, pack_name: str) -> list[dict[str, Any]]:
    """T8-seam direct reader of ``.hpc/packs/<name>.decisions.jsonl`` (append order).

    Mirrors ``ops/pack/bind_op._read_pack_records`` exactly — the SAME journal file
    and record shape — so the fail-open template-echo lookup works standalone ahead
    of T8's ``"pack"`` decision-journal scope kind (a not-yet-created file → ``[]``;
    one corrupt line never strands the rest). Re-points to
    :func:`decision_journal.read_decisions` (``"pack"`` kind) when T8 lands.
    """
    from hpc_agent._kernel.contract.layout import RepoLayout

    path = RepoLayout(experiment_dir).hpc / "packs" / f"{pack_name}.decisions.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


# --- public per-seam accessors + the combined resolve -----------------------


def resolve_reader_calls(
    experiment_dir: Path,
    *,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
) -> list[ReaderCallsDecl]:
    """S1 reader vocabularies from every opted-in pack that declares them."""
    return [
        ReaderCallsDecl(names=tuple(rp.declarations["reader_calls"]), echo=rp.echo)
        for rp in _resolve_packs(
            experiment_dir, records_by_pack=records_by_pack, records_reader=records_reader
        )
        if "reader_calls" in rp.declarations
    ]


def resolve_failure_patterns(
    experiment_dir: Path,
    *,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
) -> list[FailurePatternsDecl]:
    """S2 failure patterns from every opted-in pack that declares them."""
    return [
        FailurePatternsDecl(patterns=dict(rp.declarations["failure_patterns"]), echo=rp.echo)
        for rp in _resolve_packs(
            experiment_dir, records_by_pack=records_by_pack, records_reader=records_reader
        )
        if "failure_patterns" in rp.declarations
    ]


def resolve_axis_hints(
    experiment_dir: Path,
    *,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
) -> list[AxisHintsDecl]:
    """S3 axis hints from every opted-in pack that declares them."""
    return [
        AxisHintsDecl(hints=tuple(rp.declarations["axis_hints"]), echo=rp.echo)
        for rp in _resolve_packs(
            experiment_dir, records_by_pack=records_by_pack, records_reader=records_reader
        )
        if "axis_hints" in rp.declarations
    ]


def resolve_tolerances(
    experiment_dir: Path,
    *,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
) -> list[TolerancesDecl]:
    """S5 tolerance defaults from every opted-in pack that declares them.

    Shape-only id->value + echo. The fingerprint-precedence consumer wiring is
    DELIBERATELY NOT here (see :class:`TolerancesDecl`).
    """
    return [
        TolerancesDecl(tolerances=dict(rp.declarations["tolerances"]), echo=rp.echo)
        for rp in _resolve_packs(
            experiment_dir, records_by_pack=records_by_pack, records_reader=records_reader
        )
        if "tolerances" in rp.declarations
    ]


def resolve_registration_fields(
    experiment_dir: Path,
    *,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
) -> list[RegistrationFieldsDecl]:
    """S6 registration fields from every opted-in pack that declares them (RESERVED)."""
    return [
        RegistrationFieldsDecl(fields=tuple(rp.declarations["registration_fields"]), echo=rp.echo)
        for rp in _resolve_packs(
            experiment_dir, records_by_pack=records_by_pack, records_reader=records_reader
        )
        if "registration_fields" in rp.declarations
    ]


def resolve_declarations(
    experiment_dir: Path,
    *,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
) -> PackDeclarations:
    """Resolve EVERY seam in one pass → a :class:`PackDeclarations` bundle.

    One opt-in read + one per-pack verify; the five lists are that pass grouped by
    seam. Absent opt-in → an all-empty bundle with zero probes beyond
    interview.json (the D7 silence).
    """
    packs = _resolve_packs(
        experiment_dir, records_by_pack=records_by_pack, records_reader=records_reader
    )
    return PackDeclarations(
        reader_calls=tuple(
            ReaderCallsDecl(names=tuple(rp.declarations["reader_calls"]), echo=rp.echo)
            for rp in packs
            if "reader_calls" in rp.declarations
        ),
        failure_patterns=tuple(
            FailurePatternsDecl(patterns=dict(rp.declarations["failure_patterns"]), echo=rp.echo)
            for rp in packs
            if "failure_patterns" in rp.declarations
        ),
        axis_hints=tuple(
            AxisHintsDecl(hints=tuple(rp.declarations["axis_hints"]), echo=rp.echo)
            for rp in packs
            if "axis_hints" in rp.declarations
        ),
        tolerances=tuple(
            TolerancesDecl(tolerances=dict(rp.declarations["tolerances"]), echo=rp.echo)
            for rp in packs
            if "tolerances" in rp.declarations
        ),
        registration_fields=tuple(
            RegistrationFieldsDecl(
                fields=tuple(rp.declarations["registration_fields"]), echo=rp.echo
            )
            for rp in packs
            if "registration_fields" in rp.declarations
        ),
    )


def resolve_template_pack_echo(
    experiment_dir: Path,
    template_relpath: str,
    *,
    records_by_pack: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    records_reader: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, str | None] | None:
    """S4 (T9d): the ``{pack, version, sha}`` echo of the CURRENT-bound pack whose
    manifest ``files`` include *template_relpath*, or ``None``.

    The S4 audit-template seam is "nothing new mechanically" EXCEPT this: an audit
    template that happens to live in a bound pack lets the run sidecar / dossier
    prove WHICH pack's template gated the audit
    (:func:`hpc_agent.ops.notebook_gate.audited_source_echo`). Comparison is file
    IDENTITY — the template path resolved under *experiment_dir* against each pack
    file resolved under its manifest's dir — never a name/content-meaning check.

    **FAIL-OPEN, unlike every seam resolver above.** This feeds a sidecar echo, not
    a gate, and the ``audited_source`` block does NOT itself name a pack context
    (its fields are ``{source, template, audit_id}``). So the loud dangling refusal
    stays on the ENFORCEMENT path (:func:`resolve_declarations` / the pack gate);
    here every not-pack-bound outcome is silent-absent → ``None`` → a byte-identical
    echo (the D7 silence carried onto the echo): no ``packs`` opt-in, a template in
    no bound pack, a pack with no current bind, an on-disk manifest DRIFTED from its
    current bind (no honest echo), or even a malformed ``packs`` block all return
    ``None`` rather than raise. Cheap: with no opt-in it touches only interview.json
    (zero manifest/journal probes).
    """
    try:
        optin = _read_packs_optin(experiment_dir)
    except errors.SpecInvalid:
        return None  # a broken packs block is loud on the enforcement path, silent here
    if not optin:
        return None

    target_abs = _abs(experiment_dir / template_relpath)
    for entry in optin:
        pack_name = entry.get("pack")
        manifest_rel = entry.get("manifest")
        if not isinstance(pack_name, str) or not pack_name:
            continue
        if not isinstance(manifest_rel, str) or not manifest_rel:
            continue
        try:
            manifest_path = experiment_dir / manifest_rel
            manifest = load_manifest(manifest_path)
            if manifest.name != pack_name:
                continue
            if records_by_pack is not None:
                records: Sequence[Mapping[str, Any]] = records_by_pack.get(pack_name, ())
            elif records_reader is not None:
                records = records_reader(pack_name)
            else:
                records = _read_pack_journal(experiment_dir, pack_name)
            bind = current_bind(records, pack=pack_name)
            if bind is None:
                continue
            # Honesty: only echo when the on-disk manifest still matches the bind's
            # recorded sha — a drifted manifest's file list is not the bound one.
            if sha256_file(manifest_path) != bind.manifest_sha:
                continue
            parent = manifest_path.parent
            for pack_file in manifest.files:
                if _abs(parent / pack_file.path) == target_abs:
                    return PackEcho(
                        pack=pack_name, version=bind.version, sha=bind.manifest_sha
                    ).as_dict()
        except errors.SpecInvalid:
            continue  # fail-open: a broken/dangling pack never crashes the sidecar echo
    return None
