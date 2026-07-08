"""``audit-preflight`` — the GO/NO-GO brief for the notebook-audit loop (Phase 1b).

Design: ``docs/design/audit-preflight.md``. Precedent: ``submit_preflight`` (the
top-of-submit composite) — this is its notebook-audit analogue: it collapses the
hand-verification the run-#10 demo spent its opening turns on (template committed?
version skew? roots declared? resuming or fresh?) into ONE read-only query so the
kickoff prose collapses to "run audit-preflight; if GO, begin" — a sentence that
cannot rot because it delegates to code.

Every check is a COMPOSITION of existing machinery; this verb detects nothing new:

1. **Template present + adopted** — the file parses via
   :func:`~hpc_agent.state.audit_source.parse_percent_source` and is
   git-committed clean at its declared path. An uncommitted/dirty/untracked
   template is an "unsigned template" NO-GO (the commit IS the signature). Git
   awareness reuses the one bounded, fail-open :func:`~hpc_agent._build_info.git_output`
   helper — never an ad-hoc shell-out.
2. **Version skew** — reuses ``doctor``'s existing skew detection
   (:func:`hpc_agent.ops.recover.doctor._detect_version_skew`): the running CLI's
   embedded build sha vs the HEAD of the hpc-agent source repo. A stale tool is a
   substrate reason not to begin.
3. **Roots validity** — the declared ``input_roots`` / ``source_roots`` (from the
   spec, else defaulted from the audit's recorded configuration via
   :func:`~hpc_agent.ops.notebook.canonical.read_recorded_config`) each exist and
   are non-empty. The data-manifest drift-count DISCLOSURE the plan wants here
   rides :func:`_manifest_drift_disclosure` — a Phase-1a seam (see its docstring).
4. **Prior audit state** — whether ``audit_id`` already has a journal (resuming vs
   fresh), read through the audit-journal reader
   (:func:`hpc_agent.state.decision_journal.read_decisions`). Informational — never
   a blocker.

Boundary (from the plan): composes existing checks, blocks nothing itself (it is a
query — the gates it predicts remain the enforcement), and holds no
process-not-substrate prereqs ("envs refreshed tonight" stays in kickoff prose).
Pure local read: no SSH, no scheduler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from hpc_agent import errors
from hpc_agent._build_info import git_output
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.audit_preflight import (
    AuditPreflightResult,
    AuditPreflightSpec,
    PreflightBlocker,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.canonical import read_recorded_config
from hpc_agent.ops.recover import doctor as _doctor
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import read_decisions

__all__ = ["audit_preflight"]

_UNSIGNED_REMEDY = "commit it; that commit IS the signature"


def _abs(experiment_dir: Path, relpath: str) -> Path:
    """Resolve *relpath* against *experiment_dir* (an absolute path is kept)."""
    p = Path(relpath)
    return p if p.is_absolute() else experiment_dir / p


def _template_git_state(experiment_dir: Path, template_rel: str) -> str:
    """The template's git state: ``clean`` / ``dirty`` / ``untracked`` / ``no_git``.

    Reuses the one bounded, fail-open :func:`~hpc_agent._build_info.git_output`
    helper (no ad-hoc shell-out). ``git status --porcelain`` on the single
    pathspec is empty ⇔ committed-clean; a leading ``??`` marks an untracked
    file; anything else is a tracked-but-dirty working copy. When no git repo
    backs *experiment_dir* the commit-signature cannot be verified → ``no_git``
    (treated as unsigned by the caller — the audit's signature model needs git).
    ``git_output`` returns ``None`` on BOTH an empty (clean) porcelain read and a
    hard failure, so ``ls-files`` disambiguates the clean case from a nonexistent
    pathspec.
    """
    # Forward slashes: git accepts them on every platform; a Windows backslash
    # pathspec would not match.
    pathspec = template_rel.replace("\\", "/")
    if git_output(["rev-parse", "--show-toplevel"], cwd=experiment_dir) is None:
        return "no_git"
    porcelain = git_output(["status", "--porcelain", "--", pathspec], cwd=experiment_dir)
    if porcelain is not None and porcelain.strip():
        first = porcelain.strip().splitlines()[0]
        return "untracked" if first.startswith("??") else "dirty"
    # Empty porcelain = clean; confirm the path is actually TRACKED (a path git
    # has never seen also reads clean).
    if git_output(["ls-files", "--", pathspec], cwd=experiment_dir) is None:
        return "untracked"
    return "clean"


def _check_template(experiment_dir: Path, template_rel: str) -> tuple[str, PreflightBlocker | None]:
    """Check 1 — template present, parses, and git-committed-clean.

    Returns ``(template_state, blocker_or_None)``. The state string is surfaced
    on the result even when there is no blocker (a clean template still reports
    ``clean``).
    """
    abs_path = _abs(experiment_dir, template_rel)
    if not abs_path.is_file():
        return "missing", PreflightBlocker(
            check="template",
            blocker=f"template not found at {template_rel}",
            remedy=(
                "create the template .py at that path (e.g. build-template --shape "
                "notebook) and commit it; that commit IS the signature"
            ),
        )
    try:
        parse_percent_source(abs_path.read_text(encoding="utf-8"))
    except errors.SpecInvalid as exc:
        return "unparseable", PreflightBlocker(
            check="template",
            blocker=f"template does not parse: {exc}",
            remedy=(
                "fix the percent-format cell delimiters / hpc-audit-section markers "
                "so parse_percent_source accepts it, then commit it"
            ),
        )
    except OSError as exc:
        return "unreadable", PreflightBlocker(
            check="template",
            blocker=f"template could not be read: {exc}",
            remedy="ensure the template .py is readable, then commit it",
        )

    state = _template_git_state(experiment_dir, template_rel)
    if state == "clean":
        return state, None
    if state == "no_git":
        return state, PreflightBlocker(
            check="template",
            blocker=(
                f"cannot verify the template at {template_rel} is committed — no git "
                "repo backs the experiment directory"
            ),
            remedy=(
                "run the audit inside a git repo and commit the template; "
                "that commit IS the signature"
            ),
        )
    return state, PreflightBlocker(
        check="template",
        blocker=f"unsigned template: {template_rel} is {state} (uncommitted changes)",
        remedy=_UNSIGNED_REMEDY,
    )


def _check_version_skew(experiment_dir: Path) -> PreflightBlocker | None:
    """Check 2 — version skew, reusing ``doctor``'s existing detection.

    Fail-open by construction (the reused detector returns ``None`` when no git,
    no embedded sha, or experiment_dir is not the hpc-agent source repo). A
    resolved divergence is a substrate reason not to begin — the installed tool
    is stale.
    """
    skew = _doctor._detect_version_skew(experiment_dir)
    if skew is None:
        return None
    return PreflightBlocker(
        check="version_skew",
        blocker=skew.warning,
        remedy=(
            "reinstall the CLI from the repo (e.g. `uv tool install --reinstall .`) "
            "or rerun the release install flow so the tool matches the source"
        ),
    )


def _check_roots(
    experiment_dir: Path, source_roots: list[str], input_roots: list[str]
) -> list[PreflightBlocker]:
    """Check 3 — each declared root exists and is non-empty.

    A declared root that is missing (or not a directory) or empty is a blocker;
    an empty roots LIST is a vacuous pass (nothing declared, nothing to validate).
    """
    blockers: list[PreflightBlocker] = []
    for label, roots in (("source_roots", source_roots), ("input_roots", input_roots)):
        for root in roots:
            abs_root = _abs(experiment_dir, root)
            fix = f", or correct the declared {label} in the audit config"
            if not abs_root.is_dir():
                blockers.append(
                    PreflightBlocker(
                        check="roots",
                        blocker=(
                            f"declared {label} entry {root!r} does not exist "
                            "(or is not a directory)"
                        ),
                        remedy=f"create {root}{fix}",
                    )
                )
            elif not any(abs_root.iterdir()):
                blockers.append(
                    PreflightBlocker(
                        check="roots",
                        blocker=f"declared {label} entry {root!r} is empty",
                        remedy=f"populate {root}{fix}",
                    )
                )
    return blockers


def _manifest_drift_disclosure(experiment_dir: Path, input_roots: list[str]) -> str:
    """The data-manifest drift DISCLOSURE line (never a blocker).

    Wired to the Phase-1a substrate (:func:`hpc_agent.state.data_manifest.compute_drift`
    — the verdict-FREE projection: counts + identities, humans conclude). No
    manifest = the standing disclosure per the attention contract. The line is
    DISCLOSURE only and NEVER flips the verdict; ``compute_drift`` is read-only
    (never re-mints, never refreshes the cache), so the preflight stays a pure
    query.
    """
    from hpc_agent.state.data_manifest import compute_drift

    report = compute_drift(experiment_dir)
    if report.unmanifested:
        return (
            "data-manifest drift: no manifest recorded "
            "(runs invisible to data-drift attribution) — disclosure only, never a blocker"
        )
    c = report.counts
    return (
        f"data-manifest drift: {c['matched']} match, {c['drifted']} drifted, "
        f"{c['new']} new, {c['missing']} missing — disclosure only, never a blocker"
    )


def _prior_audit_state(experiment_dir: Path, audit_id: str | None) -> tuple[bool, int]:
    """Check 4 — resuming vs fresh, via the audit-journal reader.

    Returns ``(resuming, journal_record_count)``. ``audit_id=None`` is a fresh
    standalone preflight (nothing to resume). Informational — never a blocker.
    """
    if not audit_id:
        return False, 0
    records = read_decisions(experiment_dir, "notebook", audit_id)
    return (len(records) > 0), len(records)


def _render_brief(
    *,
    verdict: str,
    audit_id: str | None,
    template: str,
    template_state: str,
    resuming: bool,
    journal_records: int,
    source_roots: list[str],
    input_roots: list[str],
    blockers: list[PreflightBlocker],
    disclosures: list[str],
) -> str:
    """The D8 decision-ready brief — code-rendered, relayed verbatim.

    Pure, deterministic formatting of the verb's own fields (the ``relay_render``
    / ``audit_view.render_markdown`` posture): GO, or NO-GO with each blocker
    named and its remedy pre-drafted. No LLM-freeform prose enters here.
    """
    lines: list[str] = [f"# audit-preflight — {verdict}", ""]
    lines.append(f"- audit_id: {audit_id or '(standalone / no audit_id)'}")
    lines.append(f"- template: {template} ({template_state})")
    state = (
        f"resuming — {journal_records} prior journal record(s)"
        if resuming
        else "fresh audit (no prior journal)"
    )
    lines.append(f"- prior audit state: {state}")
    lines.append(f"- source_roots: {source_roots or '(none)'}")
    lines.append(f"- input_roots: {input_roots or '(none)'}")
    lines.append("")

    if verdict == "GO":
        lines.append(
            "GO — every substrate prerequisite is satisfied. Begin the audit loop: "
            "draft/revise the source, then notebook-lint, notebook-auto-clear, "
            "notebook-audit-view (relay verbatim), typed sign-off, notebook-status."
        )
    else:
        lines.append(f"NO-GO — {len(blockers)} blocker(s) must clear before the audit can begin:")
        lines.append("")
        for i, b in enumerate(blockers, start=1):
            lines.append(f"{i}. [{b.check}] {b.blocker}")
            lines.append(f"   remedy: {b.remedy}")
    lines.append("")

    if disclosures:
        lines.append("disclosures (never blockers):")
        for d in disclosures:
            lines.append(f"- {d}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


@primitive(
    name="audit-preflight",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "GO/NO-GO preflight for the notebook-audit loop. Composes existing "
            "substrate checks — template present + parses + git-committed-clean "
            "(uncommitted = unsigned), version skew (reused from doctor), declared "
            "roots exist and are non-empty, and prior audit state (resuming vs "
            "fresh) — into one decision-ready brief. Read-only, no SSH; detects "
            "nothing new and blocks nothing itself (the gates it predicts remain "
            "the enforcement). The kickoff collapses to 'run audit-preflight; if "
            "GO, begin'."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=AuditPreflightSpec,
        schema_ref=SchemaRef(input="audit_preflight"),
    ),
    agent_facing=True,
)
def audit_preflight(*, experiment_dir: Path, spec: AuditPreflightSpec) -> AuditPreflightResult:
    """Run the four substrate checks and render the GO/NO-GO brief.

    Roots resolve to the spec's when supplied, else default from the audit's
    recorded configuration when ``audit_id`` names an existing audit (the
    one-declaration rule). The verb never blocks anything itself — a NO-GO is a
    prediction the human acts on, not an enforcement.
    """
    experiment_dir = Path(experiment_dir)

    cfg = read_recorded_config(experiment_dir, spec.audit_id)
    source_roots = (
        list(spec.source_roots) if spec.source_roots is not None else list(cfg.source_roots)
    )
    input_roots = list(spec.input_roots) if spec.input_roots is not None else list(cfg.input_roots)

    blockers: list[PreflightBlocker] = []
    template_state, template_blocker = _check_template(experiment_dir, spec.template)
    if template_blocker is not None:
        blockers.append(template_blocker)
    skew_blocker = _check_version_skew(experiment_dir)
    if skew_blocker is not None:
        blockers.append(skew_blocker)
    blockers.extend(_check_roots(experiment_dir, source_roots, input_roots))

    resuming, journal_records = _prior_audit_state(experiment_dir, spec.audit_id)

    disclosures = [_manifest_drift_disclosure(experiment_dir, input_roots)]
    if resuming:
        disclosures.append(
            f"resuming audit {spec.audit_id!r}: {journal_records} prior journal "
            "record(s) — the loop continues where it left off"
        )

    verdict: Literal["GO", "NO-GO"] = "NO-GO" if blockers else "GO"
    brief = _render_brief(
        verdict=verdict,
        audit_id=spec.audit_id,
        template=spec.template,
        template_state=template_state,
        resuming=resuming,
        journal_records=journal_records,
        source_roots=source_roots,
        input_roots=input_roots,
        blockers=blockers,
        disclosures=disclosures,
    )

    return AuditPreflightResult(
        verdict=verdict,
        audit_id=spec.audit_id,
        template=spec.template,
        template_state=template_state,
        resuming=resuming,
        journal_records=journal_records,
        source_roots=source_roots,
        input_roots=input_roots,
        blockers=blockers,
        disclosures=disclosures,
        brief=brief,
    )
