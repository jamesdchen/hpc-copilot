"""Graduation gate — refuse a submit whose audited ``.py`` is not signed current.

The notebook-audit substrate's D8 gate (``docs/design/notebook-audit.md``): ONE
definition, two synchronous seats. This module is that ONE definition;
:func:`assert_source_audited` is called at
:mod:`hpc_agent.ops.resolve_submit_inputs` (pre-sidecar, the S1 human boundary)
and :mod:`hpc_agent.ops.submit_flow` (pre-staging, before any rsync/SSH) — the
same defense-in-depth / gate-before-cluster-work pattern the scope gate
(:mod:`hpc_agent.ops.scope_gate`) wires at its two seats.

Opt-in + fail-safe, the ``ops/scope_gate.py`` posture copied exactly (D7): with
NO ``audited_source`` block on ``interview.json`` the gate RETURNS silently and
byte-identically — zero filesystem probes beyond the single ``interview.json``
read (the seats already read that file). It fires ONLY inside the opted-in
surface. An opted-in repo whose declared source/template ``.py`` is missing or
unparseable is BROKEN, not a silent pass — that is a LOUD :class:`errors.SpecInvalid`
naming the path (mirrors the T8 sign-off gate's unresolvable-source refusal).

Drift = unsigned by construction (D8): a section signed then edited simply reads
unsigned at its new hash — there is no drift state machine. The
:func:`~hpc_agent.state.notebook_audit.audit_module` reduction owns the
per-section verdict; this gate adds one more revocation the reduction cannot see
— a **drifted linked source**: a passing section whose newest sign-off recorded
``linked_sources`` (T4's ``{module, file, module_sha}`` convention) reads unsigned
if any linked file no longer matches its recorded ``sha256_normalized`` (a
changed imported dependency revokes the section's trust).

notebook-audit 6a ("track-total, attend-drift") adds a TRANSITIVE-import-closure
**audit net** on top of the per-section linked-source check. A
``notebook-module-sign-off`` record CARRIES the net — ``resolved["audit_net"] =
{env_hash, modules: {module: {tier, module_sha}}}`` — resolved at SIGN-OFF time
(:func:`build_audit_net`). The gate RECOMPUTES each carried module's current tier
(:func:`_classify_net_module`) and REFUSES the submit on a drifted closure
(:data:`NET_NEW_DRIFTED` / :data:`NET_UNRESOLVED` => :class:`errors.SourceUnaudited`
naming the modules); :data:`NET_EXTERNAL` entries are DISCLOSED as ``env_hash``-bound
(the record carries the local ``env_hash`` the EXTERNAL classification rested on),
never refused. A closure module reads :data:`NET_INHERITED` (no attention) when its
current sha is UNCHANGED OR carries a fresh proof leg — ledger-attested
(:func:`module_sha_signed`) OR template-identical. Net-LESS sign-off records (the
pre-6a shape) are GRANDFATHERED: validated under the old rule above, never
retro-refused by the net path.

Pure local reads — no SSH, no TOP-LEVEL ``_wire`` import, no scheduler (the net's
shared module resolver and the sign-off-time closure walk are LAZY imports, reached
only inside the opted-in net surface). The EXTERNAL classification uses
``importlib.util.find_spec`` — metadata-only; it NEVER imports (executes) a module.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.interview_doc import iter_interview_docs
from hpc_agent.state.notebook_audit import (
    AUTO_CLEAR_BLOCK,
    MODULE_SIGN_OFF_BLOCK,
    PASSING_STATUSES,
    REUSED,
    SIGN_OFF_BLOCK,
    audit_module,
    module_sha_signed,
    read_signoff_ledger,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from hpc_agent.state.audit_source import ParsedModule

__all__ = [
    "assert_source_audited",
    "audit_currency",
    "audited_source_echo",
    "audit_net_disclosures",
    "build_audit_net",
    "AUDIT_NET_FIELD",
    "NET_INHERITED",
    "NET_EXTERNAL",
    "NET_NEW_DRIFTED",
    "NET_UNRESOLVED",
]

#: The two notebook-attestation blocks a sign-off/auto-clear record can carry —
#: used to locate the winning record for a passing section's linked-source check.
_NOTEBOOK_BLOCKS = frozenset({SIGN_OFF_BLOCK, AUTO_CLEAR_BLOCK})


def _read_audited_source(experiment_dir: Path) -> dict[str, Any] | None:
    """The interview.json ``audited_source`` block, or ``None`` when not opted in.

    Mirrors :func:`hpc_agent.ops.decision.journal._read_interview_audited_source`'s
    posture (the canonical campaign-dir root, ``.hpc/interview.json`` accepted
    defensively — the ``detect_entry_point`` convention). A missing file, a
    corrupt/non-object file, or an absent ``audited_source`` key all read as "not
    opted in" → ``None`` → the D7 silent no-op. This is the ONLY filesystem probe
    on the not-opted-in path.
    """
    for doc in iter_interview_docs(experiment_dir):
        block = doc.get("audited_source")
        if isinstance(block, dict):
            return block
    return None


def _read_required_py(experiment_dir: Path, rel: Any, *, kind: str, audit_id: Any) -> str:
    """Read a REQUIRED ``.py`` (source or template) at *rel*, or refuse LOUDLY.

    An opted-in repo with an unresolvable *kind* is broken (the gate RECOMPUTES
    hashes from the ``.py`` on disk), so a missing path field or an unreadable
    file raises :class:`errors.SpecInvalid` naming *rel* — never a silent pass
    (this is the opted-in surface; D7 silence applies only to the ABSENT
    ``audited_source`` block, resolved earlier).
    """
    if not isinstance(rel, str) or not rel:
        raise errors.SpecInvalid(
            f"notebook graduation gate: audited_source (audit_id {audit_id!r}) "
            f"declares no {kind} .py path. An opted-in repo with an unresolvable "
            f"{kind} is broken — fix interview.json's audited_source block."
        )
    path = experiment_dir / rel
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"notebook graduation gate: audited {kind} {rel!r} is unreadable "
            f"({exc}). The gate recomputes section hashes from the .py on disk; "
            f"an opted-in repo with a missing/unreadable {kind} is broken, not a "
            "silent pass."
        ) from exc


def _parse_required(rel: str, text: str, *, kind: str, audit_id: Any) -> ParsedModule:
    """Parse a REQUIRED ``.py`` into its section model, re-wrapping the loud refusal.

    :func:`parse_percent_source` already raises :class:`errors.SpecInvalid` on a
    malformed marker; this adds the path + kind + audit_id context so the human
    knows WHICH opted-in file is broken.
    """
    try:
        return parse_percent_source(text)
    except errors.SpecInvalid as exc:
        raise errors.SpecInvalid(
            f"notebook graduation gate: audited {kind} {rel!r} (audit_id "
            f"{audit_id!r}) is not valid percent-format source: {exc}"
        ) from exc


def _winning_record(
    records: list[dict[str, Any]], slug: str, section_sha: str | None
) -> dict[str, Any] | None:
    """The newest notebook record for *slug* attesting *section_sha*, or ``None``.

    For a PASSING section, :func:`~hpc_agent.state.notebook_audit.audit_module`
    already resolved the winning attestation and reported its ``signed_section_sha``;
    this locates that exact record (newest-first) so its ``linked_sources`` can be
    drift-checked. Never re-derives the pass verdict — pure record selection.
    """
    if section_sha is None:
        return None
    for record in reversed(records):
        if record.get("block") not in _NOTEBOOK_BLOCKS:
            continue
        resolved = record.get("resolved")
        if not isinstance(resolved, dict):
            continue
        if resolved.get("section") == slug and resolved.get("section_sha") == section_sha:
            return record
    return None


def _linked_source_drift(experiment_dir: Path, record: dict[str, Any] | None) -> str | None:
    """Return a description of the FIRST drifted/missing linked source, or ``None``.

    Reads the sign-off *record*'s ``resolved['linked_sources']`` (T4's
    ``{module, file, module_sha}`` list) and recomputes
    :func:`~hpc_agent.state.audit_source.sha256_normalized` over each linked file.
    A missing file or a sha mismatch means the section's cleared dependency
    changed after sign-off — trust is revoked (the section reads unsigned). A
    record with no ``linked_sources`` (the common case) never drifts.

    WAVE-3 PIECE 3 — the SIGNED-MODULE exemption. A linked module whose CURRENT
    sha differs from the section's recorded ``module_sha`` normally revokes the
    dependent, but if that current sha carries a HUMAN MODULE sign-off (this audit
    OR any other in the experiment repo, :func:`module_sha_signed`) the module was
    deliberately re-reviewed and is treated as CURRENT — no revocation. This is the
    "one module re-sign clears all dependents" flow: the drift no longer forces a
    per-section re-sign, it forces ONE module re-sign that restores every dependent.
    A drifted module with NO module sign-off of its new sha still revokes (the
    KILL-INVARIANT for modules: an unsigned change costs attention).
    """
    if record is None:
        return None
    resolved = record.get("resolved")
    linked = resolved.get("linked_sources") if isinstance(resolved, dict) else None
    if not isinstance(linked, list):
        return None
    for link in linked:
        if not isinstance(link, dict):
            continue
        rel = link.get("file")
        expected = link.get("module_sha")
        if not isinstance(rel, str) or not rel or not isinstance(expected, str) or not expected:
            continue
        path = experiment_dir / rel
        try:
            actual = sha256_normalized(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return f"{rel} missing"
        if actual != expected and not module_sha_signed(experiment_dir, actual):
            # Changed AND its new sha is unsigned — the dependency drifted with no
            # module re-sign to vouch for it. A module sign-off of the CURRENT sha
            # would exempt it (the signed-module flow above).
            return f"{rel} changed"
    return None


def audited_source_echo(experiment_dir: Path) -> dict[str, Any] | None:
    """The sidecar-sealable slice of interview.json's ``audited_source``, or ``None``.

    Returns ``{source, template, audit_id}`` — the source ``.py`` relpath, the
    template ``.py`` relpath, and the opaque audit slug — for the run sidecar to
    echo (notebook-audit T14) so ``export-dossier`` can seal the audit trail.
    ``rendered_notebook`` is deliberately DROPPED: it is a caller-side render
    metadatum, never a sealed record. Reuses :func:`_read_audited_source` — the
    ONE definition of the interview-block read — so the echo can never diverge
    from what :func:`assert_source_audited` audited. ``None`` (not opted in) → the
    caller omits the field → the sidecar stays byte-identical (the D7 fail-safe
    carried onto the echo). Pure local read — no SSH.
    """
    block = _read_audited_source(experiment_dir)
    if block is None:
        return None
    echo: dict[str, Any] = {key: block.get(key) for key in ("source", "template", "audit_id")}

    # S4 domain-pack echo (docs/design/domain-packs.md, T9d): when the audited
    # TEMPLATE .py is itself a file in a CURRENT-bound pack's manifest, additively
    # stamp the opaque {pack, version, sha} echo so export-dossier can prove WHICH
    # pack's template gated the audit. FAIL-OPEN + cheap (the gate's read posture):
    # no packs opt-in, a template in no bound pack, or any dangling/drifted pack
    # reference → NO ``pack`` key → a byte-identical echo (the D7 silence). Core
    # copies the echo verbatim; it never reads it back for meaning.
    template = echo.get("template")
    if isinstance(template, str) and template:
        from hpc_agent.state.pack_declarations import resolve_template_pack_echo

        pack_echo = resolve_template_pack_echo(experiment_dir, template)
        if pack_echo is not None:
            echo["pack"] = pack_echo
    return echo


def audit_currency(experiment_dir: Path) -> tuple[str, int] | None:
    """The opted-in audit's currency for the S1 DISCLOSURE — ``(audit_id, moved)``.

    Run #11 mechanization item 1 (audit-currency disclosure). Reuses the SAME
    computation ``notebook-status`` uses — :func:`~hpc_agent.state.notebook_audit.audit_module`
    reducing every REQUIRED (template) section against the ``audit_id`` journal —
    so nothing here re-implements hashing. Reads ``interview.json``'s
    ``audited_source`` block through the ONE definition (:func:`_read_audited_source`);
    NOT opted in → ``None`` (the D7 silence). Opted in → returns ``(audit_id,
    moved)`` where ``moved`` counts the required sections NOT signed-current
    (:data:`~hpc_agent.state.notebook_audit.PASSING_STATUSES`) — ``moved == 0`` ⇔
    the audit is current.

    DISCLOSURE seam only — it mirrors the notebook-status verdict, NOT the
    graduation refusal: it deliberately does NOT apply the gate's extra
    linked-source drift revocation (:func:`_linked_source_drift`), exactly as
    ``notebook-status`` does not. :func:`assert_source_audited` stays the single
    refusing seat. Like that gate it raises LOUDLY on a BROKEN opted-in repo
    (missing / unreadable / unparseable ``.py``); the disclosure caller
    (``ops/resolve_submit_inputs``) wraps this in a fail-open guard, so a crash
    degrades to disclosed-absent rather than an S1 error.

    Pure local reads — no SSH.
    """
    block = _read_audited_source(experiment_dir)
    if block is None:
        return None  # D7 silence — not opted in

    audit_id = block.get("audit_id")
    source_text = _read_required_py(
        experiment_dir, block.get("source"), kind="source", audit_id=audit_id
    )
    template_text = _read_required_py(
        experiment_dir, block.get("template"), kind="template", audit_id=audit_id
    )
    parsed_source = _parse_required(
        str(block.get("source")), source_text, kind="source", audit_id=audit_id
    )
    parsed_template = _parse_required(
        str(block.get("template")), template_text, kind="template", audit_id=audit_id
    )
    audit = audit_module(
        experiment_dir,
        str(audit_id),
        source=parsed_source,
        required_slugs=parsed_template.slugs,
    )
    moved = sum(1 for sect in audit.sections if sect.status not in PASSING_STATUSES)
    return str(audit_id), moved


def assert_source_audited(experiment_dir: Path) -> None:
    """Refuse a submit whose opted-in audited ``.py`` is not signed at its current hash.

    Loads ``interview.json``'s ``audited_source`` block. ABSENT (not opted in) →
    RETURN silently, byte-identically (D7 fail-safe — no further filesystem
    probes). PRESENT → parse the source + template ``.py`` (a missing/unparseable
    file is a LOUD :class:`errors.SpecInvalid` naming the path), reduce every
    REQUIRED (template) section via
    :func:`~hpc_agent.state.notebook_audit.audit_module`, and drift-check any
    ``linked_sources`` recorded on each PASSING section's winning sign-off. Any
    required section not signed-current (unsigned, drifted, or linked-source
    revoked) raises :class:`errors.SourceUnaudited` NAMING every offending section
    and its status.

    Pure local reads — no SSH. The two submit seats call this ONE definition.
    """
    block = _read_audited_source(experiment_dir)
    if block is None:
        return  # D7 fail-safe: not opted in → byte-identical no-op

    audit_id = block.get("audit_id")
    source_text = _read_required_py(
        experiment_dir, block.get("source"), kind="source", audit_id=audit_id
    )
    template_text = _read_required_py(
        experiment_dir, block.get("template"), kind="template", audit_id=audit_id
    )
    parsed_source = _parse_required(
        str(block.get("source")), source_text, kind="source", audit_id=audit_id
    )
    parsed_template = _parse_required(
        str(block.get("template")), template_text, kind="template", audit_id=audit_id
    )

    audit = audit_module(
        experiment_dir,
        str(audit_id),
        source=parsed_source,
        required_slugs=parsed_template.slugs,
    )

    # Linked-source drift revokes trust the audit_module reduction cannot see: a
    # section signed-current whose newest sign-off recorded linked_sources reads
    # UNSIGNED when any linked file no longer matches its recorded hash.
    records = read_decisions(experiment_dir, "notebook", str(audit_id))
    failures: list[tuple[str, str]] = []
    for sect in audit.sections:
        if sect.status not in PASSING_STATUSES:
            failures.append((sect.slug, sect.status))
            continue
        # WAVE-3 PIECE 2 — a REUSED section rests on the claim that its exact
        # content was HUMAN-signed under a DIFFERENT audit. VERIFY that claim at the
        # gate: re-scan the ledger for a prior human sign-off of this exact sha
        # (excluding this audit). A reuse whose backing sign-off does not exist (a
        # hand-forged ``reuse_of`` naming content no human ever signed) is REFUSED —
        # the reuse clearance is only as good as the sign-off it points at.
        if sect.status == REUSED:
            backing = read_signoff_ledger(
                experiment_dir,
                content_sha=sect.signed_section_sha or "",
                exclude_audit_id=str(audit_id),
            )
            if not backing:
                failures.append(
                    (sect.slug, "unsigned (reuse_of names a sha no prior human sign-off attests)")
                )
                continue
        drift = _linked_source_drift(
            experiment_dir, _winning_record(records, sect.slug, sect.signed_section_sha)
        )
        if drift is not None:
            failures.append((sect.slug, f"unsigned (linked-source drift: {drift})"))

    if failures:
        raise errors.SourceUnaudited.for_sections(str(audit_id), failures)

    # notebook-audit 6a — the audit-net recompute-and-refuse. Reached ONLY when every
    # required section is signed-current (the section path above already passed). A
    # module-sign-off record CARRIES the transitive closure (module -> {tier,
    # module_sha}); the gate recomputes each carried module's current tier and refuses
    # on NEW_DRIFTED / UNRESOLVED (naming the modules). EXTERNAL entries are disclosed
    # (env_hash-bound), never refused. Net-less records are grandfathered — the section
    # path above already validated them under the old rule, so they never reach here.
    net_refusals, _net_disclosures = _evaluate_audit_net(experiment_dir, block, audit_id)
    if net_refusals:
        detail = ", ".join(f"{module!r} ({status})" for module, status in net_refusals)
        raise errors.SourceUnaudited(
            f"audited source for audit_id {audit_id!r} is not cleared for graduation — "
            f"audit-net drift: {len(net_refusals)} module(s) in a signed module's "
            f"transitive import closure no longer match the net recorded at sign-off: "
            f"{detail}. A NEW_DRIFTED closure module changed with no module re-sign; an "
            "UNRESOLVED one no longer resolves (deleted / never installed). Re-sign the "
            "affected module (append-decision, scope_kind='notebook', "
            "block='notebook-module-sign-off') at its current hash — one module re-sign "
            "of the new sha clears the closure, exactly as the linked-source flow does."
        )


# --- audit-net recompute-and-refuse (notebook-audit 6a) ----------------------
# The transitive import-closure "audit net". A notebook-module-sign-off record
# CARRIES the net (module -> {tier, module_sha}) resolved at sign-off time; the
# gate RECOMPUTES each carried module's current tier and REFUSES on drift
# (NEW_DRIFTED / UNRESOLVED), DISCLOSES EXTERNAL entries as env_hash-bound, and
# GRANDFATHERS net-less records (validated under the old rule, never retro-refused).
# The module RESOLUTION is the ONE definition shared by the gate and the sign-off-
# time builder (build_audit_net) — never a second copy. Pure local reads + stdlib
# importlib.util.find_spec (metadata-only — NEVER imports/executes a module).

#: The durable tier vocabulary carried on a module-sign-off record's
#: ``resolved["audit_net"]["modules"][<module>]["tier"]``. Machinery's
#: ``AuditNetTier`` enum (``ops/notebook/linked_sources.py``) maps onto these strings
#: at the sign-off-time build seam (:func:`_resolve_closure_machinery`).
NET_INHERITED = "inherited"
#: A module that resolves to the installed environment (``find_spec``), not a local
#: file under a ``source_root`` — disclosed as bound to the record's ``env_hash``,
#: never refused.
NET_EXTERNAL = "external"
#: A local module whose current sha differs from the recorded one AND carries no fresh
#: proof leg (neither ledger-attested nor template-identical) — refused.
NET_NEW_DRIFTED = "new_drifted"
#: A module that resolves to nothing — neither a local file nor ``find_spec``-able (a
#: deleted / never-installed dependency) — refused.
NET_UNRESOLVED = "unresolved"

#: The tiers that REFUSE at gate time (ruling 2: NEW_DRIFTED / UNRESOLVED => refuse).
_NET_REFUSE_TIERS = frozenset({NET_NEW_DRIFTED, NET_UNRESOLVED})

#: The ``resolved`` key a net-carrying ``notebook-module-sign-off`` record carries.
AUDIT_NET_FIELD = "audit_net"


def _find_spec_origin(module: str) -> str | None:
    """The ``find_spec`` ORIGIN for *module*, or ``None`` when it does not resolve.

    Routes through the ONE exec-free definition,
    :func:`hpc_agent.ops.notebook.linked_sources.find_spec_origin_exec_free`
    (LAZY import — the gate's linked-sources posture): ``find_spec`` ONLY on the
    top-level segment (a DOTTED ``find_spec`` imports/execs the parent's
    ``__init__.py``, which the 6a never-exec boundary forbids — and a parent
    whose ``__init__`` raises, e.g. a carried net naming ``boompkg.sub`` with a
    ``RuntimeError``-raising ``boompkg/__init__``, would otherwise crash the
    gate with that exception at the submit boundary), deeper segments by a pure
    filesystem walk of ``submodule_search_locations``. ANY resolution failure
    reads ``None`` — an unclassifiable module is a classification RESULT (the
    gate's UNRESOLVED tier), never an exception escaping the gate.
    """
    from hpc_agent.ops.notebook.linked_sources import find_spec_origin_exec_free

    return find_spec_origin_exec_free(module)


def _compute_env_hash(external_origins: Mapping[str, str | None]) -> str:
    """The local ``env_hash`` binding the EXTERNAL classification (6a).

    A deterministic sha over the sorted ``{module: origin}`` map of the net's EXTERNAL
    modules — the environment's fingerprint AS SEEN THROUGH the closure's external
    dependencies (each origin encodes the installed location a module resolved to).
    The sign-off record carries this; the gate recomputes it and DISCLOSES a drift (an
    origin moved → the EXTERNAL classification was bound to a different environment).
    An empty external set → the sha of ``{}`` (a stable sentinel). Opaque throughout:
    origins are hashed, never parsed. Same canonical-JSON + sha256 posture as
    ``state/env_lock.py::env_lock_sha`` / ``state/run_sha.py::compute_env_hash``.
    """
    canonical = json.dumps(
        {m: (external_origins[m] or "") for m in sorted(external_origins)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_net_module(
    experiment_dir: Path, module: str, source_roots: Sequence[Path]
) -> tuple[str | None, str | None]:
    """Resolve ONE module to ``(local_sha, external_origin)`` — the ONE resolution.

    Exactly the resolution the gate and the sign-off-time builder share (never a second
    copy): a file under a ``source_root`` (the ONE ``resolve_module_file`` from
    ``ops/notebook/linked_sources.py``) → ``(sha, None)``; else a ``find_spec``-able
    installed module → ``(None, origin)``; else (a deleted / never-installed name) →
    ``(None, None)``. ``find_spec`` is metadata-only (never exec). An unreadable local
    file reads as unresolved (``(None, None)``) — a file mid-rewrite contributes no sha.
    """
    from hpc_agent.ops.notebook.linked_sources import resolve_module_file

    resolved = resolve_module_file(module, list(source_roots))
    if resolved is not None:
        try:
            return sha256_normalized(resolved.read_text(encoding="utf-8")), None
        except (OSError, UnicodeDecodeError):
            return None, None
    return None, _find_spec_origin(module)


def _template_module_shas(
    experiment_dir: Path, template_rel: Any, source_roots: Sequence[Path]
) -> dict[str, str]:
    """Map the template's imported modules → their ``module_sha`` (the template-identical
    INHERITED proof leg, ruling 3).

    Parses the template and resolves each import under *source_roots* through the ONE
    resolver, hashing each resolved file. A module the source resolves to the SAME sha
    is template-identical (it came from the template unchanged) and needs no per-module
    attention. Fail-open: a missing / unparseable template or an unreadable module yields
    ``{}`` / skips the entry (the leg simply never fires) — the gate's LOUD refusal of a
    broken template happens on the SECTION path, never here.
    """
    if not isinstance(template_rel, str) or not template_rel:
        return {}
    try:
        from hpc_agent.ops.notebook.linked_sources import imported_modules

        text = (experiment_dir / template_rel).read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return {}
    out: dict[str, str] = {}
    for module in imported_modules(tree):
        if module in out:
            continue
        local_sha, _origin = _resolve_net_module(experiment_dir, module, source_roots)
        if local_sha is not None:
            out[module] = local_sha
    return out


def _classify_net_module(
    experiment_dir: Path,
    module: str,
    *,
    recorded_sha: str | None,
    source_roots: Sequence[Path],
    template_shas: Mapping[str, str],
) -> tuple[str, str | None, str | None]:
    """Classify ONE carried module's CURRENT tier → ``(tier, current_sha, origin)``.

    The gate-time tier decision (ruling 3 + the EXTERNAL / UNRESOLVED semantics):

    * a local file under a ``source_root`` (``current_sha`` = its fresh hash):
        - unchanged (``current_sha == recorded_sha``) → :data:`NET_INHERITED` (the
          recorded proof still holds);
        - else a FRESH proof leg — :func:`module_sha_signed` ``(current_sha)``
          (ledger-attested) OR ``template_shas[module] == current_sha``
          (template-identical) → :data:`NET_INHERITED`;
        - else :data:`NET_NEW_DRIFTED` (moved with no re-attestation — attention owed).
    * no local file but ``find_spec`` resolves → :data:`NET_EXTERNAL` (env-bound;
      ``origin`` feeds the ``env_hash``). Metadata-only — a module that raises on import
      still classifies EXTERNAL (never exec).
    * neither → :data:`NET_UNRESOLVED`.

    The resolution itself routes through the ONE :func:`_resolve_net_module`.
    """
    local_sha, origin = _resolve_net_module(experiment_dir, module, source_roots)
    if local_sha is not None:
        if local_sha == recorded_sha:
            return NET_INHERITED, local_sha, None
        if module_sha_signed(experiment_dir, local_sha):
            return NET_INHERITED, local_sha, None
        if template_shas.get(module) == local_sha:
            return NET_INHERITED, local_sha, None
        return NET_NEW_DRIFTED, local_sha, None
    if origin is not None:
        return NET_EXTERNAL, None, origin
    return NET_UNRESOLVED, None, None


#: The well-formed tier vocabulary a carried modules ENTRY may name — the four
#: durable tiers (:func:`build_audit_net` mints ``inherited`` / ``external`` /
#: ``unresolved``; ``new_drifted`` is a gate-computed tier a record may still carry).
_NET_TIER_VOCABULARY = frozenset({NET_INHERITED, NET_EXTERNAL, NET_NEW_DRIFTED, NET_UNRESOLVED})

#: The tiers whose attestation IS a recorded local-file sha. A missing / non-str /
#: empty sha on one of these would recompute as NEW_DRIFTED at gate time (the
#: recorded ``None`` never equals the current sha) — a refusal forged out of a
#: malformed entry, so the entry is not well-formed without it.
_SHA_BEARING_TIERS = frozenset({NET_INHERITED, NET_NEW_DRIFTED})


def _well_formed_net_entry(entry: Any) -> bool:
    """True iff *entry* is a well-formed carried ``modules`` value (6a).

    The exact shape :func:`build_audit_net` mints: a ``Mapping`` whose ``tier``
    is a string from :data:`_NET_TIER_VOCABULARY` and whose ``module_sha`` is a
    non-empty string for the sha-bearing tiers (:data:`_SHA_BEARING_TIERS` — the
    recorded sha IS the attestation) and ``None`` for the env-bound /
    unresolvable tiers (``external`` / ``unresolved``, which carry no local
    file). Anything else is MALFORMED.
    """
    if not isinstance(entry, Mapping):
        return False
    tier = entry.get("tier")
    if not isinstance(tier, str) or tier not in _NET_TIER_VOCABULARY:
        return False
    module_sha = entry.get("module_sha")
    if tier in _SHA_BEARING_TIERS:
        return isinstance(module_sha, str) and bool(module_sha)
    return module_sha is None


def _carried_audit_net(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``resolved["audit_net"]`` a module-sign-off record carries, or ``None``.

    ``None`` = a LEGACY net-less record — GRANDFATHERED (validated under the old rule,
    never retro-refused). A present-but-malformed net ALSO reads ``None``,
    ALL-OR-NOTHING: a non-dict ``modules`` map, a non-string module name, or ANY
    malformed entry (:func:`_well_formed_net_entry`) discards the WHOLE net as
    net-less — there is no per-entry salvage. Only a WELL-FORMED net triggers the
    recompute, so a hand-forged net shape can never manufacture a refusal (a
    malformed entry like ``{"engine": "junk"}`` would otherwise record a ``None``
    sha and reclassify the module NEW_DRIFTED at gate time — a refusal minted out
    of junk); the gate only ever refuses a well-formed net whose recomputed
    closure drifted. Fail-OPEN on malformed, fail-CLOSED only on well-formed drift.
    """
    resolved = record.get("resolved")
    net = resolved.get(AUDIT_NET_FIELD) if isinstance(resolved, dict) else None
    if not isinstance(net, dict):
        return None
    modules = net.get("modules")
    if not isinstance(modules, dict):
        return None
    for module, entry in modules.items():
        if not isinstance(module, str) or not module:
            return None
        if not _well_formed_net_entry(entry):
            return None
    return net


def _evaluate_audit_net(
    experiment_dir: Path, block: Mapping[str, Any], audit_id: Any
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Recompute every net-carrying module-sign-off record's closure.

    Returns ``(refusals, disclosures)``. ``refusals`` — ``[(module, status)]`` for every
    carried module whose CURRENT tier is :data:`NET_NEW_DRIFTED` / :data:`NET_UNRESOLVED`
    (ruling 2 — deduped by module across records). ``disclosures`` — one dict per carried
    EXTERNAL module: ``{module, tier, recorded_env_hash, current_env_hash, env_status}``
    (the env_hash binding; ``env_status`` is ``"match"`` or ``"drifted"``, never a
    refusal). Net-LESS records are skipped entirely (grandfathered). Pure local reads:
    reads the journal + recomputes each carried module via the ONE classifier.
    """
    source_roots = [
        experiment_dir / r for r in (block.get("source_roots") or []) if isinstance(r, str) and r
    ]
    template_shas = _template_module_shas(experiment_dir, block.get("template"), source_roots)
    refusals: list[tuple[str, str]] = []
    disclosures: list[dict[str, Any]] = []
    refused: set[str] = set()
    for record in read_decisions(experiment_dir, "notebook", str(audit_id)):
        if record.get("block") != MODULE_SIGN_OFF_BLOCK:
            continue
        net = _carried_audit_net(record)
        if net is None:
            continue  # GRANDFATHERED — a net-less record validates under the old rule
        recorded_env_hash = net.get("env_hash")
        recorded_env_hash = recorded_env_hash if isinstance(recorded_env_hash, str) else None
        current_origins: dict[str, str | None] = {}
        for module, entry in net["modules"].items():
            recorded_sha = entry.get("module_sha") if isinstance(entry, dict) else None
            recorded_sha = recorded_sha if isinstance(recorded_sha, str) else None
            tier, _sha, origin = _classify_net_module(
                experiment_dir,
                module,
                recorded_sha=recorded_sha,
                source_roots=source_roots,
                template_shas=template_shas,
            )
            if tier == NET_EXTERNAL:
                current_origins[module] = origin
            if tier in _NET_REFUSE_TIERS and module not in refused:
                refused.add(module)
                refusals.append((module, f"audit-net {tier}"))
        if current_origins:
            current_env_hash = _compute_env_hash(current_origins)
            for module in sorted(current_origins):
                disclosures.append(
                    {
                        "module": module,
                        "tier": NET_EXTERNAL,
                        "recorded_env_hash": recorded_env_hash,
                        "current_env_hash": current_env_hash,
                        "env_status": "match"
                        if recorded_env_hash == current_env_hash
                        else "drifted",
                    }
                )
    return refusals, disclosures


def audit_net_disclosures(experiment_dir: Path) -> list[dict[str, Any]]:
    """The opted-in audit's EXTERNAL env_hash-bound disclosures (6a), or ``[]``.

    The disclosure companion to :func:`assert_source_audited`'s net refusal (the
    ``audit_currency`` disclosure-seam posture): recomputes every net-carrying
    module-sign-off record's closure and returns one entry per carried EXTERNAL module —
    ``{module, tier, recorded_env_hash, current_env_hash, env_status}`` — the environment
    binding an EXTERNAL classification rests on (an origin moved → ``env_status="drifted"``).
    EXTERNAL is disclosure-only, NEVER a refusal (ruling 2). Not opted in → ``[]`` (the D7
    silence). Pure local reads.
    """
    block = _read_audited_source(experiment_dir)
    if block is None:
        return []
    _, disclosures = _evaluate_audit_net(experiment_dir, block, block.get("audit_id"))
    return disclosures


def _resolve_closure_machinery(
    experiment_dir: Path, source_relpath: str, source_roots: Sequence[Path]
) -> list[tuple[str, str | None, str | None]]:
    """Resolve the source's transitive import closure via machinery (the 6a A-seam).

    The PRODUCTION resolver behind :func:`build_audit_net` (the no-``_resolver``
    path). LAZY-imports ``resolve_audit_net`` / ``AuditNetEntry`` /
    ``AuditNetTier`` / ``imported_modules`` from
    ``ops/notebook/linked_sources.py``, then:

    1. parses the audited source (``experiment_dir`` + *source_relpath*) with
       ``ast`` and seeds the closure with its DIRECT imports
       (``imported_modules`` over the parsed tree) — the seed is an
       ``Iterable[str]`` of dotted module names, so passing the raw relpath
       string would iterate it as CHARACTERS (the ``"."`` reaching
       ``resolve_module_file`` crashed the builder; an unreadable/unparseable
       source is instead a LOUD :class:`errors.SpecInvalid` naming the relpath,
       never a silent empty net);
    2. calls ``resolve_audit_net`` with its REAL signature — the same call
       ``ops/notebook/lint.py::_check_audit_net`` makes, including the
       ``sha_is_signed`` leg bound to :func:`module_sha_signed` and
       ``template_modules`` from the opted-in template's own direct imports
       WHEN AVAILABLE (a missing/unparseable template or a not-opted-in repo
       simply never fires that leg — fail-open, exactly as lint's
       ``template_tree=None`` posture);
    3. UNPACKS the returned ``(entries, cap_hit)`` tuple (iterating the tuple
       itself would yield ``(list, bool)`` and the entry guard below would drop
       BOTH — silently minting an empty net whose gate recompute has nothing to
       refuse on). A capped closure (``cap_hit``) is disclosed on the lint
       surface (``_check_audit_net``'s cap marker); the carried net records the
       entries machinery characterized.

    Each :class:`AuditNetEntry` maps to ``(module, local_sha|None,
    external_origin|None)``: an ``AuditNetTier.EXTERNAL`` entry is installed —
    its ``find_spec`` origin is captured without a source_root probe; every
    other entry is re-resolved through the shared :func:`_resolve_net_module`
    so the local sha / origin the record carries is the gate's OWN resolution
    (one definition — machinery's tier is advisory input, never a forked
    verdict). Reached only at sign-off time (the build seam), never on the
    not-opted-in gate path.
    """
    # LAZY A-seam: the transitive-closure resolver + its entry / tier types land at
    # merge (builder A). Reached through an ``Any``-typed handle so this file stays
    # mypy-clean until they do, while still coding against the pinned names verbatim —
    # a rename there fails here at the boundary, not silently in the record.
    from hpc_agent.ops.notebook import linked_sources as _linked_sources  # noqa: PLC0415

    _machinery: Any = _linked_sources
    resolve_audit_net = _machinery.resolve_audit_net
    AuditNetEntry = _machinery.AuditNetEntry
    AuditNetTier = _machinery.AuditNetTier
    imported_modules = _machinery.imported_modules

    try:
        tree = ast.parse((experiment_dir / source_relpath).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise errors.SpecInvalid(
            f"audit-net build: the audited source {source_relpath!r} is unreadable or "
            f"unparseable ({exc}). The net attests the source being signed — a missing "
            "or broken source is loud, never a silent empty net."
        ) from exc

    # The template-identical INHERITED leg, when the opted-in block names a
    # parseable template (mirrors lint's template_tree posture; fail-open — an
    # unavailable template only means the leg never fires).
    template_modules: set[str] = set()
    block = _read_audited_source(experiment_dir)
    template_rel = block.get("template") if block is not None else None
    if isinstance(template_rel, str) and template_rel:
        try:
            template_tree = ast.parse((experiment_dir / template_rel).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            template_tree = None
        if template_tree is not None:
            template_modules = set(imported_modules(template_tree))

    entries, _cap_hit = resolve_audit_net(
        imported_modules(tree),
        experiment_dir,
        list(source_roots),
        template_modules=template_modules,
        sha_is_signed=lambda sha: module_sha_signed(experiment_dir, sha),
    )
    out: list[tuple[str, str | None, str | None]] = []
    for entry in entries:
        if not isinstance(entry, AuditNetEntry):  # defensive: machinery's contract
            continue
        module = str(getattr(entry, "module", "") or "")
        if not module:
            continue
        if getattr(entry, "tier", None) is AuditNetTier.EXTERNAL:
            out.append((module, None, _find_spec_origin(module)))
            continue
        local_sha, origin = _resolve_net_module(experiment_dir, module, source_roots)
        out.append((module, local_sha, origin))
    return out


def build_audit_net(
    experiment_dir: Path,
    source_relpath: str,
    source_roots: Sequence[str],
    *,
    _resolver: Callable[..., list[tuple[str, str | None, str | None]]] | None = None,
) -> dict[str, Any]:
    """Build the durable audit net a ``notebook-module-sign-off`` record CARRIES (6a).

    The sign-off-time builder the skill / machinery invokes to populate
    ``resolved["audit_net"]`` BEFORE the human signs: resolve the source's transitive
    import closure and classify each module — a local file under a ``source_root`` →
    :data:`NET_INHERITED` at its current sha (the baseline the sign-off attests); a
    ``find_spec``-able installed module → :data:`NET_EXTERNAL`; neither →
    :data:`NET_UNRESOLVED`. Returns ``{"env_hash": <sha>, "modules": {module: {tier,
    module_sha}}}`` — the exact shape :func:`_carried_audit_net` reads and the gate
    recomputes. ``env_hash`` binds the EXTERNAL set (the local ``find_spec`` origins);
    the gate recomputes it and discloses a drift.

    The closure walk routes through machinery's ``resolve_audit_net`` (the default
    *resolver*, :func:`_resolve_closure_machinery` — it parses the source at
    *source_relpath*, seeds the closure with the source's direct imports, and
    unpacks machinery's ``(entries, cap_hit)`` tuple); *tier* decisions reuse the
    SAME :func:`_resolve_net_module` the gate uses (one definition). ``_resolver``
    is the test seam — a callable ``(experiment_dir, source_relpath, source_roots)
    -> [(module, local_sha|None, external_origin|None)]``; tests inject a double so
    the A seam is never imported under CI. Pure local reads; ``find_spec``
    metadata-only and exec-free.
    """
    roots = [experiment_dir / r for r in source_roots if isinstance(r, str) and r]
    resolver = _resolver if _resolver is not None else _resolve_closure_machinery
    modules: dict[str, Any] = {}
    external_origins: dict[str, str | None] = {}
    for module, local_sha, origin in resolver(experiment_dir, source_relpath, roots):
        if module in modules:
            continue
        if local_sha is not None:
            modules[module] = {"tier": NET_INHERITED, "module_sha": local_sha}
        elif origin is not None:
            modules[module] = {"tier": NET_EXTERNAL, "module_sha": None}
            external_origins[module] = origin
        else:
            modules[module] = {"tier": NET_UNRESOLVED, "module_sha": None}
    return {"env_hash": _compute_env_hash(external_origins), "modules": modules}
