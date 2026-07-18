"""``notebook-dry-run`` — the drafting-loop PREVIEW run over a small slice of data.

The maintainer's affordance (verbatim intent): the human "needs to be able to run
it on small pieces of data to see what it will do." Experiment-AGNOSTIC — some code
the LLM drafts never ends up on a cluster run, so the source need not be bound to
any audit (``audit_id`` optional). This verb executes an audited (or standalone)
percent-format ``.py`` SECTION BY SECTION in ONE namespace, in the CURRENT LOCAL
environment, and returns a deterministic, code-rendered per-section outcome: ran /
raised (with the verbatim traceback tail), which declared ``assert`` lines actually
RAN and their verdict, and the declared observables measured in the final namespace.
The LLM never interprets the run — the render is the artifact.

**This slice changes NO trust semantics (the maintainer's explicit boundary).** A
dry-run is a SAMPLE, not a proof. It journals its render receipts with
``execution_scope="sampled"``
(:data:`~hpc_agent.state.notebook_audit.EXECUTION_SCOPE_SAMPLED`), and
:func:`~hpc_agent.state.notebook_audit.read_render_receipts` — the ONE reader
feeding the D-attention tier / ``notebook-auto-clear`` / the graduation gate —
filters that class out. So a sampled run can NEVER green / auto-clear an
assertion-bearing section the way a full run (``notebook-render --execute``) can.

**Sample bounding is a DISCLOSED CONTRACT.** ``sample_n`` is exposed to the source
via the ``HPC_NOTEBOOK_SAMPLE_N`` env var; core cannot mechanically truncate an
arbitrary source's inputs safely (that would need a reader-function vocabulary the
Q1 boundary forbids core to grow), so the env var + a prominent ``sample_disclosure``
IS the contract — never a silent full run.

Lives inside the ``notebook`` subject (beside the parser + the static assertion
extractor it reuses), reaching only same-subject ``ops.notebook.*`` and the
``state.*`` substrate.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.notebook_dry_run import (
    NotebookDryRunAssertion,
    NotebookDryRunObservable,
    NotebookDryRunResult,
    NotebookDryRunSection,
    NotebookDryRunSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.audit_view import _assertions as _static_assertions
from hpc_agent.ops.notebook.canonical import read_recorded_config
from hpc_agent.state import notebook_audit
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.data_trace import stdlib_measure

if TYPE_CHECKING:
    from hpc_agent.state.audit_source import ParsedModule, Section

__all__ = ["notebook_dry_run"]

_PRIMITIVE = "notebook-dry-run"

#: The documented env var carrying the advisory sample cap to the source.
_SAMPLE_ENV_VAR = "HPC_NOTEBOOK_SAMPLE_N"

#: The namespace name the assert-reached marker is injected under (dunder so it
#: cannot shadow a user name).
_REACHED_FN = "__hpc_dry_run_assert_reached__"

#: How many trailing chars of a traceback / stdout to retain (bounded envelope).
_TAIL_CHARS = 4000


def _read_source_file(experiment_dir: Path, relpath: str) -> str:
    """Read the caller-declared source ``.py``, or raise SpecInvalid loudly."""
    path = Path(relpath)
    if not path.is_absolute():
        path = experiment_dir / path
    if not path.is_file():
        raise errors.SpecInvalid(f"notebook-dry-run source file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(
            f"notebook-dry-run source file could not be read: {path} ({exc})"
        ) from exc


def _run_cutoff(sections: tuple[Section, ...], wanted: list[str] | None) -> int:
    """The index of the last section to execute (inclusive), per the filter.

    ``wanted=None`` runs everything (cutoff = last index). A filter runs every
    section in source order up to AND including the last named one (so a named
    section's earlier dependencies still run); a named slug the source lacks is a
    loud spec_invalid.
    """
    if wanted is None:
        return len(sections) - 1
    index = {s.slug: i for i, s in enumerate(sections)}
    missing = [slug for slug in wanted if slug not in index]
    if missing:
        raise errors.SpecInvalid(
            f"notebook-dry-run section filter names slug(s) not in the source: {missing}"
        )
    return max(index[slug] for slug in wanted)


def _instrument(section_src: str, filename: str) -> Any:
    """Compile *section_src* with a reached-marker inserted before every ``assert``.

    The marker (``__hpc_dry_run_assert_reached__(<lineno>)``) runs immediately
    BEFORE the real assert, recording that the assert line was REACHED without
    touching its semantics (the real ``assert`` still evaluates its test exactly
    once and raises on failure). Raises ``SyntaxError`` on unparseable source (the
    caller treats that as a raised section — a compile failure IS the crash).
    """
    tree = ast.parse(section_src)

    class _Marker(ast.NodeTransformer):
        def visit_Assert(self, node: ast.Assert) -> Any:
            marker = ast.Expr(
                ast.Call(
                    func=ast.Name(id=_REACHED_FN, ctx=ast.Load()),
                    args=[ast.Constant(value=node.lineno)],
                    keywords=[],
                )
            )
            return [marker, node]

    new = ast.fix_missing_locations(_Marker().visit(tree))
    return compile(new, filename, "exec")


def _fail_lineno(exc: BaseException, filename: str) -> int | None:
    """The deepest line in *filename*'s own frame where *exc* was raised, or None."""
    lineno: int | None = None
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == filename:
            lineno = tb.tb_lineno
        tb = tb.tb_next
    return lineno


def _assertion_outcomes(
    section_src: str, reached: set[int], fail_lineno: int | None
) -> list[NotebookDryRunAssertion]:
    """Map the section's static asserts to EXECUTED verdicts.

    ``failed`` when the assert line is exactly where execution raised; else
    ``passed`` when the line was reached; else ``not_run`` (never reached — an
    earlier raise, or an untaken branch). Distinct from the static assertion table:
    this reflects what actually RAN in this sampled execution.
    """
    out: list[NotebookDryRunAssertion] = []
    for a in _static_assertions(section_src):
        if fail_lineno is not None and a.lineno == fail_lineno:
            outcome = "failed"
        elif a.lineno in reached:
            outcome = "passed"
        else:
            outcome = "not_run"
        out.append(
            NotebookDryRunAssertion(test=a.test, lineno=a.lineno, msg=a.msg, outcome=outcome)
        )
    return out


def _execute_section(section: Section, ns: dict[str, Any]) -> NotebookDryRunSection:
    """Execute one section into the shared namespace *ns*; return its outcome.

    Captures stdout, times the run, tracks which asserts were reached, and — on a
    raise — retains the verbatim traceback tail. Never re-raises: a raising section
    is a RESULT, not a crash of the verb (the caller stops the run after it).
    """
    filename = f"<hpc-dry-run:{section.slug}>"
    reached: set[int] = set()
    ns[_REACHED_FN] = lambda lineno: reached.add(int(lineno))
    stdout = io.StringIO()
    started = time.monotonic()
    error = False
    outcome = "ran"
    tb_tail: str | None = None
    fail_lineno: int | None = None
    try:
        code = _instrument(section.source, filename)
    except SyntaxError as exc:
        error = True
        outcome = "raised"
        tb_tail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[
            -_TAIL_CHARS:
        ]
        elapsed = round(time.monotonic() - started, 6)
        return NotebookDryRunSection(
            slug=section.slug,
            outcome=outcome,
            ran=False,
            error=True,
            elapsed_sec=elapsed,
            traceback_tail=tb_tail,
            stdout_tail=None,
            output_sha=_sha(""),
            assertions=[],
        )
    try:
        with contextlib.redirect_stdout(stdout):
            exec(code, ns)  # noqa: S102 — the sampled preview lane (in-process, one namespace)
    except BaseException as exc:  # noqa: BLE001 — relay the source's own crash verbatim
        error = True
        outcome = "raised"
        fail_lineno = _fail_lineno(exc, filename)
        tb_tail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[
            -_TAIL_CHARS:
        ]
    elapsed = round(time.monotonic() - started, 6)
    captured = stdout.getvalue()
    return NotebookDryRunSection(
        slug=section.slug,
        outcome=outcome,
        ran=not error,
        error=error,
        elapsed_sec=elapsed,
        traceback_tail=tb_tail,
        stdout_tail=captured[-_TAIL_CHARS:] if captured else None,
        output_sha=_sha(captured),
        assertions=_assertion_outcomes(section.source, reached, fail_lineno),
    )


def _sha(text: str) -> str:
    """sha256 of *text* (utf-8) — the receipt's opaque, deterministic output_sha."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_sections(
    to_run: list[Section], ns: dict[str, Any], timeout_sec: int
) -> tuple[dict[str, NotebookDryRunSection], str | None, bool]:
    """Run *to_run* in order in a worker thread bounded by *timeout_sec*.

    Returns ``(outcomes_by_slug, in_progress_slug, timed_out)``. A raising section
    STOPS the run (later sections depend on it). On timeout the worker is abandoned
    (daemon) and the in-progress slug is returned so the caller can mark it — the
    verb always returns within the bound.
    """
    outcomes: dict[str, NotebookDryRunSection] = {}
    holder: dict[str, str | None] = {"current": None}

    def _worker() -> None:
        for section in to_run:
            holder["current"] = section.slug
            result = _execute_section(section, ns)
            outcomes[section.slug] = result
            holder["current"] = None
            if result.error:
                break  # a raised section stops the run

    thread = threading.Thread(target=_worker, name="hpc-dry-run", daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)
    timed_out = thread.is_alive()
    in_progress = holder["current"] if timed_out else None
    return outcomes, in_progress, timed_out


def _measure_observables(
    ns: dict[str, Any], observables: list[str] | None
) -> list[NotebookDryRunObservable]:
    """Measure each declared observable PRESENT in the final namespace (best-effort).

    Uses core's frame-blind :func:`stdlib_measure` (the pack's frame-aware measurer
    is not wired into this preview lane). A declared-but-absent name is skipped
    silently — its absence is the disclosure. Declared order preserved.
    """
    if not observables:
        return []
    out: list[NotebookDryRunObservable] = []
    for name in observables:
        if name not in ns:
            continue
        atoms = stdlib_measure(ns[name])
        if atoms is None:
            continue
        out.append(NotebookDryRunObservable(name=name, section=None, atoms=dict(atoms)))
    return out


def _sample_disclosure(sample_n: int) -> str:
    """The prominent sample-bounding disclosure (the disclosed contract)."""
    return (
        f"Sample cap {sample_n} was exposed to the source via {_SAMPLE_ENV_VAR}={sample_n}. "
        "This cap is ADVISORY: the source must read the env var to honor it — core does "
        "NOT mechanically truncate arbitrary inputs (a silent full run is never done), so "
        "whether the sampled slice was actually applied depends on the source."
    )


def _render_markdown(result: NotebookDryRunResult) -> str:
    """Deterministic, code-authored markdown — the artifact (no timing, no prose)."""
    lines: list[str] = ["# Notebook dry-run (SAMPLED preview)", ""]
    lines.append(f"- scope: **{result.executed_scope}** — this run clears / signs NOTHING")
    lines.append(f"- {result.env_disclosure}")
    lines.append(f"- {result.sample_disclosure}")
    if result.timed_out:
        lines.append("- **TIMED OUT** — a section was abandoned at the timeout cap")
    if result.audit_id is not None:
        recorded = ", ".join(result.receipts_recorded) or "(none)"
        lines.append(f"- audit_id: {result.audit_id}; sampled receipts journaled for: {recorded}")
    lines.append("")

    lines.append("## sections")
    lines.append("")
    if not result.sections:
        lines.append("(no sections)")
        lines.append("")
    for sec in result.sections:
        lines.append(f"### {sec.slug} — {sec.outcome}")
        lines.append("")
        if sec.assertions:
            for a in sec.assertions:
                lines.append(f"- assert `{a.test}` (line {a.lineno}) -> {a.outcome}")
        else:
            lines.append("- (no declared assertions)")
        if sec.stdout_tail:
            lines.append("")
            lines.append("stdout:")
            lines.append("```")
            lines.append(sec.stdout_tail.rstrip("\n"))
            lines.append("```")
        if sec.traceback_tail:
            lines.append("")
            lines.append("traceback (verbatim):")
            lines.append("```")
            lines.append(sec.traceback_tail.rstrip("\n"))
            lines.append("```")
        lines.append("")

    if result.observables:
        lines.append("## observables (final namespace)")
        lines.append("")
        for obs in result.observables:
            lines.append(f"- {obs.name}: {obs.atoms}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_result(
    *,
    audit_id: str | None,
    parsed: ParsedModule,
    cutoff: int,
    outcomes: dict[str, NotebookDryRunSection],
    in_progress: str | None,
    timed_out: bool,
    sample_n: int,
    observables: list[NotebookDryRunObservable],
    receipts_recorded: list[str],
) -> NotebookDryRunResult:
    """Assemble the ordered per-section list + disclosures into the wire result."""
    sections: list[NotebookDryRunSection] = []
    for i, sect in enumerate(parsed.sections):
        if sect.slug in outcomes:
            sections.append(outcomes[sect.slug])
        elif i > cutoff:
            sections.append(_skipped(sect.slug, "skipped"))
        elif sect.slug == in_progress:
            sections.append(_skipped(sect.slug, "timeout"))
        else:
            # After a raised/timed-out section within the run window: not executed.
            sections.append(_skipped(sect.slug, "skipped"))

    env_disclosure = (
        f"Ran in the CURRENT LOCAL environment (interpreter {sys.executable}), NOT the cluster. "
        "The source executes in this process: any global state it mutates "
        "(os.environ, cwd, sys.modules) is NOT restored afterward."
    )
    result = NotebookDryRunResult(
        audit_id=audit_id,
        executed_scope=notebook_audit.EXECUTION_SCOPE_SAMPLED,
        env_disclosure=env_disclosure,
        interpreter=sys.executable,
        sample_n=sample_n,
        sample_env_var=_SAMPLE_ENV_VAR,
        sample_disclosure=_sample_disclosure(sample_n),
        sample_cap_consumed=None,
        timed_out=timed_out,
        sections=sections,
        receipts_recorded=receipts_recorded,
        observables=observables,
        markdown="",
    )
    return result.model_copy(update={"markdown": _render_markdown(result)})


def _skipped(slug: str, outcome: str) -> NotebookDryRunSection:
    """A section that did not execute (filtered, post-raise, or timed out)."""
    return NotebookDryRunSection(
        slug=slug,
        outcome=outcome,
        ran=False,
        error=False,
        elapsed_sec=0.0,
        traceback_tail=None,
        stdout_tail=None,
        output_sha=None,
        assertions=[],
    )


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect(
            "file_write",
            "<experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl "
            "(SAMPLED render receipts, only when audit_id is given)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    # Append-only, and only when audit_id is given (a sampled receipt per executed
    # section). Standalone mode writes nothing. Not byte-idempotent — like the
    # other receipt writers, a re-run appends fresh sampled receipts.
    idempotent=False,
    cli=CliShape(
        help=(
            "PREVIEW-run an audited (or standalone) percent-format .py over a small "
            "slice of data in the CURRENT LOCAL env, to see what it will do before "
            "committing to the audit. Executes section-by-section in one namespace, "
            "reporting per-section ran/raised (verbatim traceback tail), which "
            "declared assert lines actually RAN and their verdict, and — with an "
            "audit_id whose config names observables — the declared observables "
            "measured in the final namespace. The sample cap is exposed via the "
            "HPC_NOTEBOOK_SAMPLE_N env var (advisory; the source must honor it — core "
            "never silently truncates inputs). SAMPLE, NOT PROOF: receipts are "
            "journaled with execution_scope='sampled', which the clearing path "
            "filters out, so a dry-run can never auto-clear an assertion-bearing "
            "section (a full run does that). Standalone mode (no audit_id) touches no "
            ".hpc state. The result includes markdown — the code-rendered projection "
            "relayed VERBATIM; the LLM never interprets the run."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookDryRunSpec,
        schema_ref=SchemaRef(input="notebook_dry_run"),
    ),
    agent_facing=True,
)
def notebook_dry_run(*, experiment_dir: Path, spec: NotebookDryRunSpec) -> NotebookDryRunResult:
    """Execute *spec.source* over a bounded sample and return the per-section outcome.

    Parses the source, runs sections in order up to the filter cutoff in ONE
    namespace under a wall-clock timeout, journals a SAMPLED (non-clearing) render
    receipt per executed section when ``audit_id`` is given, measures the recorded
    observation plan's observables (audit mode), and returns the deterministic
    code-rendered projection.

    Raises :class:`errors.SpecInvalid` on an unreadable source, a malformed
    percent-format module, or a section filter naming a slug the source lacks.
    """
    experiment_dir = Path(experiment_dir)
    parsed = parse_percent_source(_read_source_file(experiment_dir, spec.source))
    cutoff = _run_cutoff(parsed.sections, spec.sections)

    # Roots/observables come from the recorded audit config in audit mode; standalone
    # mode reads nothing (conservative empty config).
    observable_names: list[str] | None = None
    if spec.audit_id is not None:
        observable_names = read_recorded_config(experiment_dir, spec.audit_id).observables

    to_run = list(parsed.sections[: cutoff + 1])

    ns: dict[str, Any] = {"__name__": "__hpc_dry_run__"}
    # Preamble (imports / setup before the first section) runs first, so section
    # dependencies resolve — its outcome is not reported (it owns no section).
    if parsed.preamble.strip():
        # A broken preamble is swallowed here; the dependent sections then surface
        # the failure per-section (NameError etc.) where the human can see it.
        with (
            contextlib.suppress(BaseException),  # noqa: BLE001
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exec(  # noqa: S102 — the sampled preview lane
                compile(parsed.preamble, "<hpc-dry-run:preamble>", "exec"), ns
            )

    prior = os.environ.get(_SAMPLE_ENV_VAR)
    os.environ[_SAMPLE_ENV_VAR] = str(spec.sample_n)
    try:
        outcomes, in_progress, timed_out = _run_sections(to_run, ns, spec.timeout_sec)
    finally:
        if prior is None:
            os.environ.pop(_SAMPLE_ENV_VAR, None)
        else:
            os.environ[_SAMPLE_ENV_VAR] = prior

    # Journal one SAMPLED (non-clearing) receipt per EXECUTED section, bound to its
    # fresh section sha — provenance the human can inspect, never clearing evidence.
    receipts_recorded: list[str] = []
    if spec.audit_id is not None:
        by_slug = {s.slug: s for s in parsed.sections}
        for slug, outcome in outcomes.items():
            section = by_slug[slug]
            notebook_audit.record_render_receipt(
                experiment_dir,
                audit_id=spec.audit_id,
                section=slug,
                section_sha=section.section_sha,
                recompute=section.section_sha,
                output_sha=outcome.output_sha or _sha(""),
                error=outcome.error,
                execution_scope=notebook_audit.EXECUTION_SCOPE_SAMPLED,
            )
            receipts_recorded.append(slug)

    observables = [] if timed_out else _measure_observables(ns, observable_names)

    return _build_result(
        audit_id=spec.audit_id,
        parsed=parsed,
        cutoff=cutoff,
        outcomes=outcomes,
        in_progress=in_progress,
        timed_out=timed_out,
        sample_n=spec.sample_n,
        observables=observables,
        receipts_recorded=receipts_recorded,
    )
