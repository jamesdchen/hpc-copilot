"""``suggest-prelude-action`` primitive — pick the next prelude step, in code.

The prelude (idea → S1) is a chain of deterministic transitions with only four
genuine judgment points (draft authoring, the typed sign-off, the axis LLM tree
behind ``classify-axis-auto``'s escalation, and human-owned intent fields —
``docs/plans/prelude-chain-2026-07-30.md``). Everything between them is
mechanical, yet the agent has been walking it as prose: "is there an audit? has
it passed? is there an interview.json? is the pack bound *and* opted in?".

This is the ``suggest-setup-action`` treatment (``cli/setup_actions.py``) applied
to that chain: one TOTAL priority ladder, expressed as ordered kernel rules over
:func:`hpc_agent._kernel.decision.decide`, reading the five prelude substrates

1. the notebook decision journal (``.hpc/notebooks/<audit_id>.decisions.jsonl``)
   plus ``notebook-status``'s ``passed`` predicate,
2. the notebook-audit-config seat (interview.json's ``audited_source`` block or
   the journaled ``notebook-audit-config`` record),
3. the pack journal + interview.json's ``packs`` opt-in — and the INTEGRITY of
   the pair, which is where the 2026-07-30 live fumble lived: a pack with a
   ``bound`` record but no ``packs`` entry (or the reverse) silently voids every
   pack gate, and no verb reported it. It is now a named remedy,
4. ``.hpc/axes.yaml`` presence + staleness (the stored ``run_signature_sha`` vs.
   the entry point's current one), and
5. ``interview.json`` presence / ``_materialized``

and returning ONE next action + the ``why`` + a scaffold of the exact call.

Boundary posture:

* **Total.** Every state maps to exactly one suggestion; the last rung is the
  catch-all "the prelude is settled". The ladder never escalates.
* **Never crashes.** Every substrate read is tolerant; an unreadable or corrupt
  one becomes rung 0 — a DISCLOSED suggestion to run ``doctor`` — never an
  exception. A suggestion query that dies on a broken repo is useless exactly
  when it is needed.
* **One definition.** The ``passed`` predicate is recomputed through the SAME
  reduction ``notebook-status`` uses (:func:`hpc_agent.state.notebook_audit.audit_module`),
  never a journal-only proxy; when the source/template are not resolvable from a
  durable seat, the ladder says so and points at ``notebook-status`` instead of
  inventing a second definition.
* **Advisory.** It suggests; it never journals, gates, or consents. Read-only.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.suggest_prelude_action import (
    PreludeFinding,
    PreludeScaffold,
    PreludeSubstrates,
    SuggestPreludeActionResult,
)
from hpc_agent.cli._dispatch import CliShape

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.fixtures.escalation import CandidateAction

__all__ = ["suggest_prelude_action"]

# The journal-filename suffix and the per-kind journal directory are both
# DERIVED from ``state.decision_journal.decisions_path`` (the one path builder)
# rather than restated here — see :func:`_journal_ids`.
_DIR_PROBE = "_dir_probe"

# Placeholder values for fields no durable record can supply. Each is
# schema-valid for its target field so the scaffold still validates; the caller
# is told to replace them via ``unresolved_fields`` (the scaffold-spec posture).
_PH_SOURCE = "PLACEHOLDER-source.py"
_PH_TEMPLATE = "PLACEHOLDER-template.py"
_PH_MANIFEST = "PLACEHOLDER-packs/<pack>/pack.json"
_PH_SLUG = "PLACEHOLDER-section-slug"
_PH_SIGNATURE_SHA = "PLACEHOLDER-run-signature-sha"
_PH_RUN_NAME = "placeholder_run"

# The fail-safe axis kind the classify-axis scaffold pre-fills: 'sequential' is
# the classifier's own conservative default, so a caller who invokes the
# scaffold unedited under-parallelizes rather than returning wrong numbers. It
# is still flagged unresolved — the classification is a judgment point.
_FAILSAFE_AXIS = "sequential"

# The template path the cold-start scaffold proposes. Convention only (the verb
# accepts any relpath); named here so the suggested call is copy-pasteable.
_DEFAULT_TEMPLATE_OUT = ".hpc/templates/audit_template.py"


# --- substrate evidence ------------------------------------------------------


@dataclasses.dataclass
class _Audit:
    """One audit's reduced substrate state (substrates 1 + 2)."""

    audit_id: str
    config_recorded: bool = False
    audited_source_seat: bool = False
    source: str | None = None
    template: str | None = None
    #: ``notebook-status``'s ``passed`` predicate, or ``None`` when the
    #: source/template are not resolvable from a durable seat (then the ladder
    #: refuses to invent a second predicate and points at ``notebook-status``).
    passed: bool | None = None
    awaiting: int | None = None


@dataclasses.dataclass
class _PackRepair:
    """One pack opt-in/bind integrity mismatch (substrate 3)."""

    pack: str
    #: ``"bound_not_opted_in"`` (the 2026-07-30 fumble) or ``"opted_in_not_bound"``.
    kind: str
    manifest: str | None = None


@dataclasses.dataclass
class _Prelude:
    """The whole evidence vector — assembled once, decided over by the rules."""

    experiment_dir: Path
    findings: list[PreludeFinding] = dataclasses.field(default_factory=list)
    disclosures: list[str] = dataclasses.field(default_factory=list)
    #: Rung-0 evidence: a substrate that could not be read or parsed.
    corrupt: list[PreludeFinding] = dataclasses.field(default_factory=list)
    audits: list[_Audit] = dataclasses.field(default_factory=list)
    #: audit_ids DECLARED by an interview.json ``audited_source`` block — an audit
    #: exists from the moment the block does, before its journal file appears.
    declared_audit_ids: list[str] = dataclasses.field(default_factory=list)
    packs_opted_in: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    packs_bound: list[str] = dataclasses.field(default_factory=list)
    pack_repairs: list[_PackRepair] = dataclasses.field(default_factory=list)
    axes_present: bool = False
    axes_executors: dict[str, Any] = dataclasses.field(default_factory=dict)
    interview_present: bool = False
    #: Whether any interview.json carries INTENT — a non-empty ``goal``, the
    #: human-owned field ``audit-handoff`` exists to draft. Distinguishes a bare
    #: ``audited_source``-only shell (rung 5) from a drafted-but-unmaterialized
    #: interview (rung 6).
    interview_intent: bool = False
    materialized: bool = False
    entry_point_run_name: str | None = None

    def note(
        self, substrate: str, detail: str, remedy: str | None = None, *, corrupt: bool = False
    ) -> PreludeFinding:
        """Record one finding; *corrupt* also makes it rung-0 evidence."""
        finding = PreludeFinding(substrate=substrate, detail=detail, remedy=remedy)  # type: ignore[arg-type]
        self.findings.append(finding)
        if corrupt:
            self.corrupt.append(finding)
        return finding


def _journal_ids(experiment_dir: Path, scope_kind: str) -> tuple[list[str], list[str]]:
    """Every scope_id with a journal file on disk, plus the ids whose file is corrupt.

    The journal directory and the ``.decisions.jsonl`` suffix are both derived
    from :func:`hpc_agent.state.decision_journal.decisions_path` — the ONE path
    builder — rather than restated. The derivation is guarded behind an
    ``.hpc/`` existence check because ``RepoLayout.hpc`` CREATES the directory on
    access, and a read-only suggestion must not write into a bare experiment dir.

    "Corrupt" here means a NON-EMPTY journal file that yields zero records:
    :func:`~hpc_agent.state.decision_journal.read_decisions` skips individually
    bad lines by design, so a file of nothing but bad lines is silently empty —
    exactly the state that must surface rather than read as "no audit".
    """
    from hpc_agent.state.decision_journal import decisions_path, read_decisions

    base = experiment_dir / ".hpc"
    if not base.is_dir():
        return [], []
    probe = decisions_path(experiment_dir, scope_kind, _DIR_PROBE)
    suffix = probe.name[len(_DIR_PROBE) :]
    if not probe.parent.is_dir():
        return [], []
    ids: list[str] = []
    corrupt: list[str] = []
    for path in sorted(probe.parent.glob(f"*{suffix}")):
        if not path.is_file():
            continue
        scope_id = path.name[: -len(suffix)]
        if not scope_id:
            continue
        try:
            records = read_decisions(experiment_dir, scope_kind, scope_id)
        except errors.HpcError:
            corrupt.append(scope_id)
            continue
        ids.append(scope_id)
        try:
            has_bytes = path.stat().st_size > 0
        except OSError:  # pragma: no cover — stat after a successful read
            has_bytes = False
        if has_bytes and not records:
            corrupt.append(scope_id)
    return ids, corrupt


def _read_interview(ev: _Prelude) -> None:
    """Substrates 3 + 5: interview.json presence, ``_materialized``, ``packs``."""
    from hpc_agent.state.interview_doc import INTERVIEW_JSON_RELPATHS, iter_interview_docs

    for rel in INTERVIEW_JSON_RELPATHS:
        path = ev.experiment_dir / rel
        if not path.is_file():
            continue
        ev.interview_present = True
        # ``iter_interview_docs`` skips an unparseable candidate SILENTLY (the D7
        # not-opted-in fail-safe). For a SUGGESTION that silence is the wrong
        # answer — "no interview.json" and "an interview.json we cannot read" are
        # different next steps — so probe parseability ourselves and disclose.
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            ev.note(
                "interview",
                f"{rel} exists but is not parseable JSON ({exc})",
                remedy=f"repair or remove {rel}",
                corrupt=True,
            )
            continue
        if not isinstance(doc, dict):
            ev.note(
                "interview",
                f"{rel} top level is {type(doc).__name__}, not a JSON object",
                remedy=f"repair {rel}",
                corrupt=True,
            )

    for doc in iter_interview_docs(ev.experiment_dir):
        goal = doc.get("goal")
        if isinstance(goal, str) and goal.strip():
            ev.interview_intent = True
        # The ``audited_source`` block DECLARES an audit — the opt-in path's audit
        # exists from the moment the block does, before any journal record makes a
        # file appear. Enumerating audits from journals alone would miss exactly
        # the freshly-opened audit whose sections are all awaiting sign-off.
        audited = doc.get("audited_source")
        if isinstance(audited, dict):
            declared = audited.get("audit_id")
            if isinstance(declared, str) and declared and declared not in ev.declared_audit_ids:
                ev.declared_audit_ids.append(declared)
        materialized = doc.get("_materialized")
        if isinstance(materialized, dict) and not ev.materialized:
            ev.materialized = True
            entry = materialized.get("entry_point")
            if isinstance(entry, dict):
                name = entry.get("run_name")
                if isinstance(name, str) and name:
                    ev.entry_point_run_name = name
        if "packs" not in doc or ev.packs_opted_in:
            continue
        block = doc["packs"]
        if not isinstance(block, list):
            ev.note(
                "pack",
                "interview.json 'packs' opt-in block is not a list",
                remedy="repair the packs block (a list of {pack, manifest, receipt_bindings})",
                corrupt=True,
            )
            continue
        for raw in block:
            if not isinstance(raw, dict):
                continue
            name = raw.get("pack")
            if isinstance(name, str) and name and name not in ev.packs_opted_in:
                ev.packs_opted_in[name] = raw


def _read_packs(ev: _Prelude) -> None:
    """Substrate 3: the pack journals + the opt-in/bind integrity pair."""
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.pack_receipts import PACK_SUBJECT_KIND, current_bind

    journal_ids, corrupt_ids = _journal_ids(ev.experiment_dir, PACK_SUBJECT_KIND)
    for scope_id in corrupt_ids:
        ev.note(
            "pack",
            f"pack journal {scope_id!r} has content but yields no readable records",
            remedy="run doctor; the journal is corrupt",
            corrupt=True,
        )
    bound: list[str] = []
    for name in journal_ids:
        try:
            records = read_decisions(ev.experiment_dir, PACK_SUBJECT_KIND, name)
        except errors.HpcError:  # pragma: no cover — _journal_ids already filtered
            continue
        if current_bind(records, pack=name) is not None:
            bound.append(name)
    ev.packs_bound = sorted(bound)

    # THE INTEGRITY PAIR. A bind and an opt-in are two independent records and
    # nothing reconciled them: ``pack-status`` starts from the opt-in list, so a
    # pack that is BOUND but not opted in is invisible to it and every pack gate
    # silently passes. Both directions are now named remedies.
    for name in ev.packs_bound:
        if name in ev.packs_opted_in:
            continue
        ev.pack_repairs.append(_PackRepair(pack=name, kind="bound_not_opted_in"))
        ev.note(
            "pack",
            f"pack {name!r} has a current bind but no interview.json 'packs' entry",
            remedy=f"bound but not opted in — add the packs entry for {name!r}",
        )
    for name in sorted(ev.packs_opted_in):
        if name in ev.packs_bound:
            continue
        entry = ev.packs_opted_in[name]
        manifest = entry.get("manifest")
        ev.pack_repairs.append(
            _PackRepair(
                pack=name,
                kind="opted_in_not_bound",
                manifest=manifest if isinstance(manifest, str) and manifest else None,
            )
        )
        ev.note(
            "pack",
            f"pack {name!r} is opted in on interview.json but has no current bind",
            remedy=f"opted in but not bound — run pack-bind for {name!r}",
        )


def _read_axes(ev: _Prelude) -> None:
    """Substrate 4: ``.hpc/axes.yaml`` presence + the classified-executor block."""
    from hpc_agent.state.axes import axes_path, read_axes

    ev.axes_present = axes_path(ev.experiment_dir).is_file()
    if not ev.axes_present:
        return
    try:
        config = read_axes(ev.experiment_dir)
    except Exception as exc:  # noqa: BLE001 — a corrupt substrate is rung 0, not a crash
        ev.note(
            "axes",
            f".hpc/axes.yaml is present but unreadable/invalid ({exc})",
            remedy="repair or remove .hpc/axes.yaml",
            corrupt=True,
        )
        return
    executors = (config or {}).get("executors")
    ev.axes_executors = dict(executors) if isinstance(executors, dict) else {}


def _current_signature_sha(ev: _Prelude, run_name: str) -> str | None:
    """The entry point's CURRENT ``run_signature_sha`` from the AST scan, or None.

    ``discover_runs`` is a pure AST walk (no user-code import), so this is safe
    inside a query. Any failure degrades to ``None`` + a disclosure: the ladder
    then treats a PRESENT executors entry as fresh rather than fabricating
    staleness it cannot prove.
    """
    from hpc_agent.state.discover import discover_runs

    try:
        runs = discover_runs(ev.experiment_dir)
    except Exception as exc:  # noqa: BLE001 — degrade, never fail a suggestion
        ev.disclosures.append(f"run discovery unavailable; axes staleness unchecked ({exc})")
        return None
    for info in runs:
        if info.name == run_name:
            sha = getattr(info, "run_signature_sha", None)
            return str(sha) if sha else None
    ev.disclosures.append(
        f"no @register_run named {run_name!r} found by the AST scan; axes staleness unchecked"
    )
    return None


def _read_audits(ev: _Prelude) -> None:
    """Substrates 1 + 2: every audit's journal, config seat, and ``passed``."""
    from hpc_agent.ops.notebook.canonical import read_interview_audited_source
    from hpc_agent.state.audit_source import parse_percent_source
    from hpc_agent.state.notebook_audit import PASSING_STATUSES, audit_module, read_audit_config

    journal_ids, corrupt_ids = _journal_ids(ev.experiment_dir, "notebook")
    for scope_id in corrupt_ids:
        ev.note(
            "notebook-journal",
            f"notebook journal {scope_id!r} has content but yields no readable records",
            remedy="run doctor; the journal is corrupt",
            corrupt=True,
        )
    # An audit exists if EITHER seat says so: a journal file on disk, or an
    # interview.json ``audited_source`` declaration (the opt-in path declares the
    # audit before the first record creates a journal).
    for audit_id in sorted(set(journal_ids) | set(ev.declared_audit_ids)):
        audit = _Audit(audit_id=audit_id)
        ev.audits.append(audit)
        block = read_interview_audited_source(ev.experiment_dir, audit_id)
        journaled = read_audit_config(ev.experiment_dir, audit_id)
        audit.config_recorded = block is not None or journaled is not None
        if block is not None:
            source = block.get("source")
            template = block.get("template")
            if isinstance(source, str) and source and isinstance(template, str) and template:
                audit.audited_source_seat = True
                audit.source = source
                audit.template = template
        if not audit.audited_source_seat:
            continue
        # The ``passed`` predicate, recomputed through the SAME reduction
        # ``notebook-status`` uses — never a journal-only proxy.
        assert audit.source is not None and audit.template is not None
        try:
            source_text = (ev.experiment_dir / audit.source).read_text(encoding="utf-8")
            template_text = (ev.experiment_dir / audit.template).read_text(encoding="utf-8")
            parsed_source = parse_percent_source(source_text)
            parsed_template = parse_percent_source(template_text)
        except (OSError, UnicodeDecodeError, errors.HpcError) as exc:
            ev.note(
                "notebook-journal",
                (
                    f"audit {audit_id!r} names source {audit.source!r} / template "
                    f"{audit.template!r}, which could not be read or parsed ({exc})"
                ),
                remedy="fix the audited_source paths, or re-open the audit under a new audit_id",
                corrupt=True,
            )
            continue
        rollup = audit_module(
            ev.experiment_dir,
            audit_id,
            source=parsed_source,
            required_slugs=parsed_template.slugs,
        )
        audit.passed = rollup.passed
        audit.awaiting = sum(1 for s in rollup.sections if s.status not in PASSING_STATUSES)


def _gather(experiment_dir: Path) -> _Prelude:
    """Read all five substrates, tolerantly. Never raises for a broken repo."""
    ev = _Prelude(experiment_dir=experiment_dir)
    # interview.json first: the audited_source / packs blocks it carries are
    # inputs to the audit and pack reads.
    _read_interview(ev)
    _read_audits(ev)
    _read_packs(ev)
    _read_axes(ev)
    if len(ev.audits) > 1:
        ev.disclosures.append(
            f"{len(ev.audits)} audits have journals ({', '.join(a.audit_id for a in ev.audits)}); "
            "the ladder reports the first, sorted"
        )
    return ev


# --- scaffolds ---------------------------------------------------------------


def _cli_line(
    experiment_dir: Path,
    target: str,
    spec: dict[str, Any] | None,
    flags: dict[str, str] | None = None,
) -> str:
    """The exact ``hpc-agent`` invocation for *target* — POSIX paths, sorted JSON."""
    parts = ["hpc-agent", target, "--experiment-dir", experiment_dir.as_posix()]
    for name, value in (flags or {}).items():
        parts += [f"--{name.replace('_', '-')}", value]
    if spec is not None:
        parts += ["--spec", f"'{json.dumps(spec, sort_keys=True)}'"]
    return " ".join(parts)


def _scaffold(
    experiment_dir: Path,
    target: str,
    spec: dict[str, Any] | None = None,
    *,
    unresolved: list[str] | None = None,
    flags: dict[str, str] | None = None,
) -> PreludeScaffold:
    """A verb-invocation scaffold (``cli`` filled)."""
    return PreludeScaffold(
        verb=target,
        cli=_cli_line(experiment_dir, target, spec, flags),
        spec=spec,
        unresolved_fields=sorted(unresolved or []),
    )


def _edit_scaffold(
    target: str, spec: dict[str, Any], *, unresolved: list[str] | None = None
) -> PreludeScaffold:
    """A FILE-EDIT scaffold — ``cli`` is null; ``spec`` is the fragment to add."""
    return PreludeScaffold(
        verb=target, cli=None, spec=spec, unresolved_fields=sorted(unresolved or [])
    )


# --- the ladder --------------------------------------------------------------


def _branch(
    rung: int,
    action: str,
    why: str,
    scaffold: PreludeScaffold,
    *,
    audit: _Audit | None = None,
) -> CandidateAction:
    """One resolved rung, packaged as the kernel's chosen candidate.

    *audit* is the audit the rung fired FOR (rungs 2-5), carried on the params so
    the envelope's substrate block reports that audit's state rather than
    re-deriving which audit the reason string mentioned.
    """
    from hpc_agent._wire.fixtures.escalation import CandidateAction

    return CandidateAction(
        action=action,
        params={"rung": rung, "why": why, "scaffold": scaffold, "audit": audit},
        rationale=why,
    )


def _rule_doctor(ev: _Prelude) -> CandidateAction | None:
    """Rung 0 — a substrate could not be read. Disclosed, never a crash."""
    if not ev.corrupt:
        return None
    first = ev.corrupt[0]
    why = (
        f"{len(ev.corrupt)} prelude substrate(s) could not be read — "
        f"{first.substrate}: {first.detail}. Every downstream rung would be "
        "deciding over unknown state."
    )
    return _branch(0, "doctor", why, _scaffold(ev.experiment_dir, "doctor"))


def _rule_pack_optin(ev: _Prelude) -> CandidateAction | None:
    """Rung 1 — the pack bind/opt-in integrity pair (the 2026-07-30 fumble)."""
    if not ev.pack_repairs:
        return None
    # The bound-but-not-opted-in direction first: it is the SILENT one (pack-status
    # starts from the opt-in list, so it never reports this pack at all).
    repair = next(
        (r for r in ev.pack_repairs if r.kind == "bound_not_opted_in"), ev.pack_repairs[0]
    )
    if repair.kind == "bound_not_opted_in":
        why = (
            f"pack {repair.pack!r} bound but not opted in — every pack gate reads it "
            "as absent and silently passes; add the packs entry"
        )
        scaffold = _edit_scaffold(
            "interview.json",
            {"packs": [{"pack": repair.pack, "manifest": _PH_MANIFEST, "receipt_bindings": []}]},
            unresolved=["packs[0].manifest"],
        )
    else:
        why = (
            f"pack {repair.pack!r} is opted in on interview.json but has no current "
            "bind — the gate refuses until it is bound"
        )
        spec: dict[str, Any] = {"manifest": repair.manifest or _PH_MANIFEST, "pack": repair.pack}
        scaffold = _scaffold(
            ev.experiment_dir,
            "pack-bind",
            spec,
            unresolved=[] if repair.manifest else ["manifest"],
        )
    return _branch(1, "pack-optin-repair", why, scaffold)


def _rule_record_config(ev: _Prelude) -> CandidateAction | None:
    """Rung 2 — an audit is open but has no config seat.

    The seat is immutable-per-audit, so recording it LATE moves every view_sha
    and reads prior sign-offs stale. That makes "no seat yet" the earliest
    notebook rung: fix it before more attention is spent signing.
    """
    audit = next((a for a in ev.audits if not a.config_recorded), None)
    if audit is None:
        return None
    why = (
        f"audit {audit.audit_id!r} has a journal but no audit-config seat — the "
        "roots the lint and the handoff intent read are unrecorded (and the seat "
        "is immutable-per-audit, so record it before signing more sections)"
    )
    spec = {
        "kind": "config",
        "audit_id": audit.audit_id,
        "input_roots": [],
        "source_roots": [],
    }
    return _branch(
        2,
        "notebook-record-config",
        why,
        _scaffold(ev.experiment_dir, "notebook-record", spec),
        audit=audit,
    )


def _rule_notebook_status(ev: _Prelude) -> CandidateAction | None:
    """Rung 3 — the audit's source/template live only with the caller.

    A STANDALONE audit records its config on the journal, which carries no
    source/template paths; only interview.json's ``audited_source`` block does.
    Without them the ``passed`` predicate is not recomputable HERE, and inventing
    a journal-only proxy would be a second definition of the gate. So the ladder
    hands the predicate back to the verb that owns it.
    """
    audit = next((a for a in ev.audits if a.passed is None), None)
    if audit is None:
        return None
    why = (
        f"audit {audit.audit_id!r} is open with a config seat, but no durable seat "
        "names its source/template — supply them to notebook-status, which owns "
        "the passed predicate"
    )
    spec = {
        "audit_id": audit.audit_id,
        "source": audit.source or _PH_SOURCE,
        "template": audit.template or _PH_TEMPLATE,
    }
    unresolved = [k for k in ("source", "template") if str(spec[k]).startswith("PLACEHOLDER")]
    return _branch(
        3,
        "notebook-status",
        why,
        _scaffold(ev.experiment_dir, "notebook-status", spec, unresolved=unresolved),
        audit=audit,
    )


def _rule_audit_view(ev: _Prelude) -> CandidateAction | None:
    """Rung 4 — sections are awaiting sign-off; relay the view."""
    audit = next((a for a in ev.audits if a.passed is False), None)
    if audit is None:
        return None
    awaiting = audit.awaiting if audit.awaiting is not None else 0
    why = f"audit {audit.audit_id} has {awaiting} section(s) awaiting sign-off → relay the view"
    spec = {
        "audit_id": audit.audit_id,
        "source": audit.source,
        "template": audit.template,
    }
    return _branch(
        4,
        "notebook-audit-view",
        why,
        _scaffold(ev.experiment_dir, "notebook-audit-view", spec),
        audit=audit,
    )


def _rule_audit_handoff(ev: _Prelude) -> CandidateAction | None:
    """Rung 5 — the audit passed but no interview INTENT has been drafted.

    The plan's shorthand for this rung was "audit passed, no interview.json", but
    that condition CANNOT FIRE and so must not be written: the only durable seat
    naming an audit's source/template is interview.json's ``audited_source``
    block, and without it ``passed`` is not computable at all (rung 3 catches that
    case first). A passed audit therefore always has an interview.json.

    The real gap the shorthand meant is INTENT: the ``audited_source``-only shell
    carries no ``goal`` and nothing was materialized, so the human-owned intent
    fields have never been drafted — exactly what ``audit-handoff`` projects from
    the audit-open seat + an AST scan. Once intent exists (or the interview ran),
    rung 6 owns the state.
    """
    if ev.materialized or ev.interview_intent:
        return None
    audit = next((a for a in ev.audits if a.passed is True), None)
    if audit is None:
        return None
    why = (
        f"audit {audit.audit_id} passed and no interview intent is recorded → run "
        "audit-handoff to project the audit records into a draft InterviewSpec"
    )
    spec = {
        "audit_id": audit.audit_id,
        "source": audit.source,
        "template": audit.template,
    }
    return _branch(
        5,
        "audit-handoff",
        why,
        _scaffold(ev.experiment_dir, "audit-handoff", spec),
        audit=audit,
    )


def _rule_interview(ev: _Prelude) -> CandidateAction | None:
    """Rung 6 — interview.json exists but nothing was materialized from it.

    Reached once intent exists (rung 5 handed off) or when no audit is involved at
    all: the spec is drafted but ``interview`` never ran, so there is no
    ``tasks.py`` / materialized entry point for the submit chain to read.
    """
    if not ev.interview_present or ev.materialized:
        return None
    why = (
        "interview.json exists but carries no _materialized block — the interview "
        "has not produced tasks.py / the entry point yet"
    )
    # The InterviewSpec is the one prelude spec big enough that hand-authoring it
    # is the documented failure mode; scaffold-spec composes it from context.
    return _branch(
        6,
        "interview",
        why,
        _scaffold(ev.experiment_dir, "scaffold-spec", None, flags={"verb": "interview"}),
    )


def _rule_classify_axis(ev: _Prelude) -> CandidateAction | None:
    """Rung 7 — axes.yaml is absent, or its classification is missing/stale.

    Staleness is EXACT, not a timestamp guess: the stored
    ``executors.<run>.run_signature_sha`` is compared against the run's current
    signature from the AST scan — the same key ``axes.yaml`` documents as the
    classification's invalidation trigger.
    """
    if not ev.materialized:
        return None
    run_name = ev.entry_point_run_name
    entry = ev.axes_executors.get(run_name) if run_name else None
    if not ev.axes_present:
        reason = ".hpc/axes.yaml is absent"
    elif run_name is None:
        # Materialized without a run_name (e.g. a python_module entry): there is
        # no executors key to check, so nothing here can fire.
        return None
    elif not isinstance(entry, dict):
        reason = f".hpc/axes.yaml has no classified axis for run {run_name!r}"
    else:
        stored = entry.get("run_signature_sha")
        current = _current_signature_sha(ev, run_name)
        if current is None or not isinstance(stored, str) or stored == current:
            return None
        reason = (
            f".hpc/axes.yaml classified run {run_name!r} at signature "
            f"{stored[:12]} but the current signature is {current[:12]} (stale)"
        )
    why = f"{reason} → classify the series axis before any cluster time is spent"
    # The signature sha is DERIVABLE (a pure AST scan of the entry point), so the
    # scaffold fills it; the axis KIND is the judgment point, pre-filled with the
    # classifier's own fail-safe and always flagged unresolved.
    signature = _current_signature_sha(ev, run_name) if run_name else None
    spec: dict[str, Any] = {
        "run_name": run_name or _PH_RUN_NAME,
        "run_signature_sha": signature or _PH_SIGNATURE_SHA,
        "data_axis": {"kind": _FAILSAFE_AXIS},
        "classified_by": "agent",
    }
    unresolved = ["data_axis"]
    if run_name is None:
        unresolved.append("run_name")
    if signature is None:
        unresolved.append("run_signature_sha")
    return _branch(
        7,
        "classify-axis",
        why,
        _scaffold(ev.experiment_dir, "classify-axis", spec, unresolved=unresolved),
    )


def _rule_cold_start(ev: _Prelude) -> CandidateAction | None:
    """Rung 8 — nothing exists: no audit journal, no interview.json."""
    if ev.audits or ev.interview_present:
        return None
    why = (
        "no notebook audit journal and no interview.json — the prelude has not "
        "started; scaffold the audit template, then draft the source against it"
    )
    spec = {"slugs": [_PH_SLUG], "output_path": _DEFAULT_TEMPLATE_OUT}
    return _branch(
        8,
        "notebook-scaffold-template",
        why,
        _scaffold(ev.experiment_dir, "notebook-scaffold-template", spec, unresolved=["slugs"]),
    )


@primitive(
    name="suggest-prelude-action",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Run the prelude priority ladder over the five prelude substrates "
            "(notebook decision journal + notebook-status's passed predicate, the "
            "audit-config seat, the pack journal / interview.json packs opt-in and "
            "the INTEGRITY of that pair, .hpc/axes.yaml presence+staleness, "
            "interview.json presence/_materialized) and recommend ONE next step: "
            "{rung, action, why, scaffold, findings, substrates}. Total — every "
            "state maps to exactly one suggestion; a corrupt/unreadable substrate "
            "is a disclosed 'run doctor' rung, never a crash. Read-only, no SSH."
        ),
        experiment_dir_arg=True,
    ),
    agent_facing=True,
)
def suggest_prelude_action(experiment_dir: Path) -> SuggestPreludeActionResult:
    """Recommend the ONE next prelude step for *experiment_dir*.

    Priority order (first match wins; the ladder is total, so it never escalates):

    * **0 — ``doctor``**: a substrate could not be read (an invalid
      ``axes.yaml``, an unparseable ``interview.json``, a journal whose every
      line is corrupt, an ``audited_source`` naming a file that will not parse).
      Disclosed as a finding, never raised.
    * **1 — ``pack-optin-repair``**: the pack bind/opt-in pair disagrees. A pack
      ``bound`` but absent from ``interview.json``'s ``packs`` block is the
      SILENT half (``pack-status`` iterates the opt-in list, so it never reports
      that pack and every pack gate passes) — the 2026-07-30 live fumble, now a
      named remedy: "bound but not opted in — add the packs entry". The reverse
      (opted in, never bound) scaffolds ``pack-bind``.
    * **2 — ``notebook-record-config``**: an audit journal exists with no config
      seat. The seat is immutable-per-audit and recording it late moves every
      view_sha, so it precedes further sign-offs.
    * **3 — ``notebook-status``**: the audit's source/template are not named by
      any durable seat (a standalone audit), so ``passed`` is not recomputable
      here — handed back to the verb that owns the predicate rather than
      approximated.
    * **4 — ``notebook-audit-view``**: ``passed`` is false — N sections await
      sign-off; relay the view.
    * **5 — ``audit-handoff``**: the audit passed and no interview INTENT is
      recorded (no ``goal``, nothing materialized) — project the audit records
      into a draft InterviewSpec. NOT "no interview.json": that condition cannot
      fire, because the seat naming the audit's source/template lives ON
      interview.json (see :func:`_rule_audit_handoff`).
    * **6 — ``interview``**: ``interview.json`` exists but carries no
      ``_materialized`` block; the scaffold points at ``scaffold-spec --verb
      interview`` (the spec is the documented hand-authoring failure mode).
    * **7 — ``classify-axis``**: materialized, but ``.hpc/axes.yaml`` is absent,
      carries no entry for the entry point, or carries one whose stored
      ``run_signature_sha`` no longer matches the run's current signature.
    * **8 — ``notebook-scaffold-template``**: nothing exists at all — cold start.
    * **9 — ``submit-s1``** (default): every substrate is settled; the prelude is
      done and the submit chain can start.

    Returns a :class:`~hpc_agent._wire.queries.suggest_prelude_action.SuggestPreludeActionResult`
    carrying the chosen rung, the ``why``, a scaffold of the exact call, every
    finding (including ones a lower rung pre-empted), the disclosures, and the
    evidence vector the decision was made over.

    Raises :class:`errors.SpecInvalid` only when *experiment_dir* is missing —
    a broken repo is REPORTED (rung 0), never raised.
    """
    if experiment_dir is None:
        raise errors.SpecInvalid("experiment_dir is required")

    from pathlib import Path as _Path

    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction

    ev = _gather(_Path(experiment_dir))

    decision = decide(
        "prelude_step",
        ev,
        rules=[
            _rule_doctor,
            _rule_pack_optin,
            _rule_record_config,
            _rule_notebook_status,
            _rule_audit_view,
            _rule_audit_handoff,
            _rule_interview,
            _rule_classify_axis,
            _rule_cold_start,
        ],
        default=CandidateAction(
            action="submit-s1",
            params={
                "rung": 9,
                "why": (
                    "every prelude substrate is settled (audits passed, interview "
                    "materialized, axes classified, packs consistent) → start the "
                    "submit chain"
                ),
                "scaffold": _scaffold(ev.experiment_dir, "block-drive", {"workflow": "submit"}),
                "audit": None,
            },
            rationale="the prelude is complete.",
        ),
    )
    chosen = decision.chosen
    assert chosen is not None  # a total ladder always resolves to a branch

    reported: _Audit | None = chosen.params["audit"]
    return SuggestPreludeActionResult(
        rung=int(chosen.params["rung"]),
        action=chosen.action,  # type: ignore[arg-type]
        why=str(chosen.params["why"]),
        scaffold=chosen.params["scaffold"],
        findings=list(ev.findings),
        disclosures=list(ev.disclosures),
        substrates=PreludeSubstrates(
            audit_ids=[a.audit_id for a in ev.audits],
            audit_id=reported.audit_id if reported is not None else None,
            audit_config_recorded=bool(reported.config_recorded) if reported else False,
            audited_source_seat=bool(reported.audited_source_seat) if reported else False,
            notebook_passed=reported.passed if reported else None,
            sections_awaiting=reported.awaiting if reported else None,
            packs_opted_in=sorted(ev.packs_opted_in),
            packs_bound=list(ev.packs_bound),
            axes_yaml=ev.axes_present,
            interview_json=ev.interview_present,
            materialized=ev.materialized,
            entry_point_run_name=ev.entry_point_run_name,
        ),
    )
