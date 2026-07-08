"""``notebook-lint`` primitive — four structural checks over an audit source.

Notebook-audit substrate, Wave B / T4 (see ``docs/design/notebook-audit.md``).
A read-only ``validate`` verb over a jupytext percent-format ``.py`` (parsed by
:mod:`hpc_agent.state.audit_source`, the ONE reader of that grammar) plus its
template and caller-declared, OPAQUE path/import roots. Four rules:

1. **structural completeness** — the template's marker slugs must appear in the
   source's slugs as an ORDER-PRESERVING SUBSEQUENCE. Missing and reordered
   slugs are reported. Slugs are OPAQUE — no content-meaning check (Q1 flag).
2. **executes-live** — path-shaped STRING LITERALS (defined purely
   syntactically: a ``str`` constant that contains a path separator, or that
   resolves under a declared ``input_root`` — NEVER a reader-function vocabulary
   like ``read_csv``, which the Q1 flags ban) are checked to exist under the
   caller-declared ``input_roots``; a missing one is a finding. A COMPUTED path
   expression (an f-string / a ``+``-concatenation carrying a separator) cannot
   be verified and is recorded in ``unverifiable_paths`` — an honest gap, never
   silently skipped.
3. **linked_sources** — ``ast.Import`` / ``ast.ImportFrom`` that resolve to a
   file under a caller ``source_root`` are reported as
   ``{module, file, module_sha}`` (``module_sha`` via
   :func:`hpc_agent.state.audit_source.sha256_normalized`). Judges import ORIGIN
   IDENTITY only — never import content/semantics (Q1 flag). Unresolvable
   imports (stdlib / site-packages) are simply not linked, never findings.
4. **template_import_shadowed** — a SOURCE section that defines (``def`` /
   ``async def`` / ``class``) or rebinds (a top-level assignment, or an import
   with a DIFFERENT origin) a name the TEMPLATE imports is reported. The shadow
   list is derived ONLY from the template's own import statements (an AST walk)
   — no hardcoded name lists, no configuration knob, no domain vocabulary: the
   template's imports ARE the caller's declared engines, and the rule
   mechanizes "call the engine, never re-derive" as POINTING (a shadowing
   section is already modified/added → human-required; this finding NAMES the
   hazard at sign-off instead of hiding it in a diff).

Findings are REPORTED, never raised — the graduation gate refuses, the lint
reports. Only a malformed spec or unparseable source raises
:class:`hpc_agent.errors.SpecInvalid` (T1's precedent). Pure, stdlib-only
(``ast`` + the shared hashing primitive): no jupytext, no third-party import.
"""

from __future__ import annotations

import ast
from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.actions.notebook_lint import (
    LinkedSource,
    NotebookLintFinding,
    NotebookLintInput,
    NotebookLintResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized

_PRIMITIVE = "notebook-lint"

#: The two path separators a literal may carry. ``os.sep`` is deliberately NOT
#: used — a source authored on POSIX and linted on Windows (or vice-versa) must
#: recognise both separators identically (the cross-platform posture T1 keeps
#: for hashing).
_PATH_SEPARATORS = ("/", "\\")


def _read_source_file(experiment_dir: Path, relpath: str, *, kind: str) -> str:
    """Read a caller-declared ``.py`` (source or template) or raise SpecInvalid.

    A missing file is a malformed spec (it points at a file that is not there),
    NOT a finding — findings describe the audited CONTENT, and there is no
    content to audit. Loud, per T1's ``SpecInvalid`` precedent.
    """
    path = Path(relpath)
    if not path.is_absolute():
        path = experiment_dir / path
    if not path.is_file():
        raise errors.SpecInvalid(f"notebook-lint {kind} file not found: {path}")
    return path.read_text(encoding="utf-8")


def _parse_ast(text: str) -> ast.Module:
    """Parse *text* to an AST, mapping a SyntaxError to SpecInvalid.

    Unparseable source is a malformed input (the LLM drafted invalid Python),
    not a finding — the executes-live / linked-sources walks cannot run.
    """
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        raise errors.SpecInvalid(f"notebook-lint source is not parseable Python: {exc}") from exc


def _section_slug_for_line(sections: list[tuple[int, int, str]], lineno: int) -> str | None:
    """Return the section slug covering 1-based *lineno*, or ``None`` (preamble).

    *sections* is ``(start_line0, end_line0, slug)`` half-open spans in 0-based
    line indices (T1's ``Section.start_line`` is 0-based); an AST ``lineno`` is
    1-based, so it lands in a span iff ``start <= lineno-1 < end``.
    """
    idx = lineno - 1
    for start, end, slug in sections:
        if start <= idx < end:
            return slug
    return None


def _build_section_spans(source_text: str) -> list[tuple[int, int, str]]:
    """Half-open ``(start_line0, end_line0, slug)`` spans for line attribution."""
    parsed = parse_percent_source(source_text)
    total_lines = len(source_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    spans: list[tuple[int, int, str]] = []
    secs = parsed.sections
    for j, sec in enumerate(secs):
        end = secs[j + 1].start_line if j + 1 < len(secs) else total_lines
        spans.append((sec.start_line, end, sec.slug))
    return spans


# ── rule 1: structural completeness ─────────────────────────────────────────


def _check_structural_completeness(
    template_slugs: tuple[str, ...],
    source_slugs: tuple[str, ...],
) -> list[NotebookLintFinding]:
    """Report template slugs missing from — or reordered within — the source.

    The contract: ``template_slugs`` must be an ORDER-PRESERVING SUBSEQUENCE of
    ``source_slugs``. A template slug absent from the source entirely is
    ``missing``; a template slug present but not reachable in order (a two-pointer
    walk over the source can't match it after the prior match) is ``reordered``.
    Slugs are opaque identifiers — no content is inspected.
    """
    findings: list[NotebookLintFinding] = []
    source_set = set(source_slugs)
    # Two-pointer subsequence walk: advance a cursor through the source for each
    # template slug that IS present, matching at or after the previous position.
    cursor = 0
    for slug in template_slugs:
        if slug not in source_set:
            findings.append(
                NotebookLintFinding(
                    rule="structural_completeness",
                    section=slug,
                    detail=f"template section {slug!r} is missing from the source",
                    evidence={"slug": slug, "kind": "missing"},
                )
            )
            continue
        try:
            pos = source_slugs.index(slug, cursor)
        except ValueError:
            # Present in the source but only BEFORE the cursor → out of order.
            findings.append(
                NotebookLintFinding(
                    rule="structural_completeness",
                    section=slug,
                    detail=(
                        f"template section {slug!r} appears out of order in the "
                        "source (not an order-preserving subsequence)"
                    ),
                    evidence={
                        "slug": slug,
                        "kind": "reordered",
                        "template_order": list(template_slugs),
                        "source_order": list(source_slugs),
                    },
                )
            )
            continue
        cursor = pos + 1
    return findings


# ── rule 2: executes-live ────────────────────────────────────────────────────


def _resolve_candidates(experiment_dir: Path, root_dirs: list[Path], literal: str) -> list[Path]:
    """Candidate on-disk paths a path literal could denote.

    Permissive by design (a false-positive "missing" finding is worse than a
    miss): an absolute literal is itself; a relative literal is tried both
    root-relative (``experiment_dir/literal`` — the literal already carries its
    root, e.g. ``inputs/data.csv``) and as a leaf under each declared root
    (``root/literal`` — a bare ``data.csv`` under ``input_root=inputs``).
    """
    p = Path(literal)
    if p.is_absolute():
        return [p]
    candidates = [experiment_dir / literal]
    candidates.extend(root / literal for root in root_dirs)
    return candidates


def _is_path_shaped(experiment_dir: Path, root_dirs: list[Path], literal: str) -> bool:
    """Purely-syntactic path-shape test (no reader-function vocabulary).

    A literal is path-shaped iff it carries a path separator OR it resolves under
    a declared ``input_root`` (a bare filename that exists under a root). The
    second clause only ever fires on an EXISTING file, so it can never manufacture
    a missing-path finding — it just widens what counts as a checked path.
    """
    if any(sep in literal for sep in _PATH_SEPARATORS):
        return True
    return any((root / literal).exists() for root in root_dirs)


def _joinedstr_has_separator(node: ast.JoinedStr) -> bool:
    """True if any constant chunk of an f-string carries a path separator."""
    return any(
        isinstance(v, ast.Constant)
        and isinstance(v.value, str)
        and any(sep in v.value for sep in _PATH_SEPARATORS)
        for v in node.values
    )


def _binop_has_separator_constant(node: ast.BinOp) -> bool:
    """True if a ``+`` concatenation has a str-constant operand with a separator."""
    if not isinstance(node.op, ast.Add):
        return False
    for operand in (node.left, node.right):
        if (
            isinstance(operand, ast.Constant)
            and isinstance(operand.value, str)
            and any(sep in operand.value for sep in _PATH_SEPARATORS)
        ):
            return True
    return False


def _check_executes_live(
    tree: ast.Module,
    experiment_dir: Path,
    root_dirs: list[Path],
    sections: list[tuple[int, int, str]],
) -> tuple[list[NotebookLintFinding], list[str]]:
    """Check path-shaped literals exist; record computed paths as unverifiable.

    Returns ``(findings, unverifiable_paths)``. A literal path that does not
    resolve under any declared root is a finding attributed to its section; a
    computed path expression (f-string / ``+`` with a separator) is appended to
    ``unverifiable_paths`` (the honest gap).
    """
    findings: list[NotebookLintFinding] = []
    unverifiable: list[str] = []
    # First pass: flag computed path expressions and mark their constant chunks
    # as consumed, so a separator-bearing string INSIDE an f-string / concat
    # (e.g. the ``"inputs/"`` in ``f"inputs/{x}"``) is not re-counted as a
    # standalone literal path.
    consumed_const_ids: set[int] = set()
    for node in ast.walk(tree):
        is_computed_path = (isinstance(node, ast.JoinedStr) and _joinedstr_has_separator(node)) or (
            isinstance(node, ast.BinOp) and _binop_has_separator_constant(node)
        )
        if is_computed_path:
            unverifiable.append(ast.unparse(node))
            for child in ast.walk(node):
                if isinstance(child, ast.Constant):
                    consumed_const_ids.add(id(child))
    # Second pass: literal string paths (skipping consumed chunks).
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in consumed_const_ids:
                continue
            literal = node.value
            if not _is_path_shaped(experiment_dir, root_dirs, literal):
                continue
            candidates = _resolve_candidates(experiment_dir, root_dirs, literal)
            if any(c.exists() for c in candidates):
                continue
            findings.append(
                NotebookLintFinding(
                    rule="executes_live",
                    section=_section_slug_for_line(sections, getattr(node, "lineno", 0)),
                    detail=(
                        f"path literal {literal!r} does not exist under the declared input_roots"
                    ),
                    evidence={
                        "path": literal,
                        "line": getattr(node, "lineno", None),
                        "input_roots": [str(r) for r in root_dirs],
                    },
                )
            )
    return findings, unverifiable


# ── rule 3: linked_sources ───────────────────────────────────────────────────


def _resolve_module_file(module: str, root_dirs: list[Path]) -> Path | None:
    """Resolve a dotted *module* name to a file under one of *root_dirs*.

    ``foo.bar`` → ``foo/bar.py`` or ``foo/bar/__init__.py`` under each root; the
    first hit (roots in declared order) wins. ``None`` when nothing resolves —
    an unresolvable import is stdlib / site-packages, never a link.
    """
    rel = Path(*module.split("."))
    for root in root_dirs:
        for candidate in (root / rel.with_suffix(".py"), root / rel / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def _imported_modules(tree: ast.Module) -> list[str]:
    """Dotted module names an ``import`` / ``from`` statement brings in.

    For ``from pkg import name`` both ``pkg`` and ``pkg.name`` are candidates
    (``name`` may be a submodule file); the resolver keeps whichever exists.
    Relative imports (``level > 0``) are skipped — a relative origin is inside
    the same package, not a cross-``source_root`` link this rule reports.
    """
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0 or not node.module:
                continue
            modules.append(node.module)
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _check_linked_sources(
    tree: ast.Module,
    experiment_dir: Path,
    root_dirs: list[Path],
) -> list[LinkedSource]:
    """Report imports resolving to a file under a declared ``source_root``.

    Deduped by resolved file (two import forms can name one origin). ``module_sha``
    is the shared hashing primitive over the file text — the exact value T9
    recomputes to drift-check the link.
    """
    seen_files: set[Path] = set()
    linked: list[LinkedSource] = []
    for module in _imported_modules(tree):
        resolved = _resolve_module_file(module, root_dirs)
        if resolved is None:
            continue
        resolved = resolved.resolve()
        if resolved in seen_files:
            continue
        seen_files.add(resolved)
        try:
            rel = str(resolved.relative_to(experiment_dir.resolve()))
        except ValueError:
            rel = str(resolved)
        linked.append(
            LinkedSource(
                module=module,
                file=rel,
                module_sha=sha256_normalized(resolved.read_text(encoding="utf-8")),
            )
        )
    return linked


# ── rule 4: template_import_shadowed ─────────────────────────────────────────

#: The pseudo-slug attributed to a template import that sits OUTSIDE any section
#: (in the module preamble before the first ``# hpc-audit-section:`` marker).
_MODULE_PREAMBLE = "module-preamble"

#: An import binding's ORIGIN — what the bound name actually points at, so an
#: IDENTICAL re-import (the normal verbatim copy of a template section) compares
#: equal and only a DIFFERENT binding of the same name reads as a shadow.
#: ``("import", module)`` for ``import``; ``("from", level, module, orig_name)``
#: for ``from … import``.
_ImportOrigin = tuple[str, ...] | tuple[str, int, str, str]


def _parse_tolerant(text: str) -> ast.Module | None:
    """``ast.parse`` tolerant of a mid-draft :class:`SyntaxError` (→ ``None``).

    Mirrors ``audit_view._assertions``: a section that does not parse contributes
    NOTHING to this rule — the lint's structural refusal (:func:`_parse_ast` over
    the whole source) owns unparseable input; a per-section walk never raises.
    """
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _import_bindings(node: ast.AST) -> dict[str, _ImportOrigin]:
    """Bound name → origin for every import statement under *node* (first wins).

    ``import X`` → ``"X"``; ``import X.Y`` → ``"X"`` (the top-level package is
    what's bound, and its origin is that same package — ``import X.Y`` and
    ``import X.Z`` bind the SAME object); ``import X as Y`` → ``"Y"`` with
    origin ``X``; ``from M import a, b as c`` → ``"a"``, ``"c"`` with origins
    ``(M, a)`` / ``(M, b)``. ``from M import *`` binds no statically-known name
    and is skipped.
    """
    bindings: dict[str, _ImportOrigin] = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Import):
            for alias in sub.names:
                if alias.asname:
                    bindings.setdefault(alias.asname, ("import", alias.name))
                else:
                    top = alias.name.split(".")[0]
                    bindings.setdefault(top, ("import", top))
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                bindings.setdefault(bound, ("from", sub.level or 0, sub.module or "", alias.name))
    return bindings


def _template_import_map(
    template_preamble: str,
    template_sections: list[tuple[str, str]],
) -> dict[str, tuple[_ImportOrigin, str]]:
    """Bound name → ``(origin, template_slug)`` for every template import.

    The shadow list is derived ONLY from the template's import statements — the
    AGNOSTIC boundary (no hardcoded names, no config knob, no domain vocabulary).
    Imports in the preamble are attributed to :data:`_MODULE_PREAMBLE`; a name
    imported in several places keeps its FIRST occurrence (document order —
    deterministic). A segment that does not parse contributes nothing.
    """
    out: dict[str, tuple[_ImportOrigin, str]] = {}
    segments = [(_MODULE_PREAMBLE, template_preamble), *template_sections]
    for slug, text in segments:
        tree = _parse_tolerant(text)
        if tree is None:
            continue
        for name, origin in _import_bindings(tree).items():
            out.setdefault(name, (origin, slug))
    return out


def _section_rebindings(tree: ast.Module) -> list[tuple[str, str, _ImportOrigin | None]]:
    """``(name, kind, import_origin)`` for every TOP-LEVEL binding in a section.

    Walks the section body's top-level statements ONLY — a name defined inside a
    function body shadows nothing at module scope, and an attribute / subscript
    assignment (``obj.x = …``) binds no module name. Kinds: ``def`` / ``class``
    (definitions), ``assignment`` (a ``Name`` target of an assignment), and
    ``import`` (carries its origin so an identical re-import compares clean).
    """
    events: list[tuple[str, str, _ImportOrigin | None]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            events.append((stmt.name, "def", None))
        elif isinstance(stmt, ast.ClassDef):
            events.append((stmt.name, "class", None))
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                elts = target.elts if isinstance(target, ast.Tuple | ast.List) else [target]
                events.extend(
                    (elt.id, "assignment", None) for elt in elts if isinstance(elt, ast.Name)
                )
        elif isinstance(stmt, ast.AnnAssign):
            # ``x: int`` alone annotates without binding; only a valued one rebinds.
            if stmt.value is not None and isinstance(stmt.target, ast.Name):
                events.append((stmt.target.id, "assignment", None))
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                events.append((stmt.target.id, "assignment", None))
        elif isinstance(stmt, ast.Import | ast.ImportFrom):
            events.extend(
                (name, "import", origin) for name, origin in _import_bindings(stmt).items()
            )
    return events


def _check_template_import_shadowed(
    source_sections: list[tuple[str, str]],
    template_preamble: str,
    template_sections: list[tuple[str, str]],
) -> list[NotebookLintFinding]:
    """Report SOURCE sections that shadow a name the TEMPLATE imports.

    The template's imports ARE the caller's declared engines; this rule
    mechanizes "call the engine, never re-derive" as POINTING — a shadowing
    section is already modified/added (→ human-required), so the finding's job
    is to NAME the hazard at sign-off instead of leaving it buried in a diff.
    The shadow list is derived ONLY from the template's own import statements
    (agnostic: no name lists, no knob, no domain vocabulary).

    A shadow is: a ``def`` / ``async def`` / ``class`` defining the name, a
    top-level assignment rebinding it, or an import binding it to a DIFFERENT
    origin (an IDENTICAL import statement is the normal verbatim copy — clean).
    Per section a shadowed name is reported once (its first shadowing event);
    findings are sorted by ``(slug, name)`` so the view_sha downstream stays
    stable. Sections that do not parse contribute nothing (the structural rules
    own refusal).
    """
    imports = _template_import_map(template_preamble, template_sections)
    if not imports:
        return []
    findings: list[NotebookLintFinding] = []
    for slug, text in source_sections:
        tree = _parse_tolerant(text)
        if tree is None:
            continue
        reported: set[str] = set()
        for name, kind, origin in _section_rebindings(tree):
            if name not in imports or name in reported:
                continue
            template_origin, template_slug = imports[name]
            if kind == "import" and origin == template_origin:
                continue  # verbatim copy of the template's own import — clean.
            reported.add(name)
            findings.append(
                NotebookLintFinding(
                    rule="template_import_shadowed",
                    section=slug,
                    detail=(
                        f"section {slug!r} shadows {name!r}, which the template "
                        f"imports in {template_slug!r} — the template's imports "
                        "are the declared engines; call the engine, never re-derive"
                    ),
                    evidence={
                        "name": name,
                        "template_slug": template_slug,
                        "kind": kind,
                    },
                )
            )
    findings.sort(key=lambda f: (f.section or "", f.evidence["name"]))
    return findings


def _root_dirs(experiment_dir: Path, roots: list[str]) -> list[Path]:
    """Resolve caller-declared roots to directories (relative → experiment_dir)."""
    out: list[Path] = []
    for r in roots:
        p = Path(r)
        out.append(p if p.is_absolute() else experiment_dir / p)
    return out


@primitive(
    name=_PRIMITIVE,
    verb="validate",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Lint a notebook-audit source .py against its template and "
            "caller-declared, opaque path/import roots. Four read-only checks: "
            "structural completeness (template marker slugs as an order-preserving "
            "subsequence of the source), executes-live (path-shaped string "
            "literals exist under input_roots; computed paths recorded as "
            "unverifiable), linked_sources (imports resolving under "
            "source_roots reported with their module_sha), and "
            "template_import_shadowed (a source section defining or rebinding a "
            "name the template imports — the template's imports are the declared "
            "engines; a verbatim re-import is clean). Findings are REPORTED, "
            "never raised — the graduation gate refuses, the lint reports."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookLintInput,
        schema_ref=SchemaRef(input="notebook_lint"),
    ),
    agent_facing=True,
)
def notebook_lint(*, experiment_dir: Path, spec: NotebookLintInput) -> NotebookLintResult:
    """Run the four notebook-audit lint rules; return a report of findings.

    Findings are reported (never raised) in :class:`NotebookLintResult`;
    ``unverifiable_paths`` holds computed path expressions the executes-live rule
    could not check, and ``linked_sources`` holds imports resolved under the
    caller ``source_roots`` with their ``module_sha``. A section with zero
    findings is one auto-clear precondition for T5's tier computation.

    Raises
    ------
    :class:`hpc_agent.errors.SpecInvalid`
        The ``source`` / ``template`` file is missing, the source is not
        parseable Python, or a marker/slug in either is malformed (surfaced by
        :func:`hpc_agent.state.audit_source.parse_percent_source`).
    """
    experiment_dir = Path(experiment_dir)
    source_text = _read_source_file(experiment_dir, spec.source, kind="source")
    template_text = _read_source_file(experiment_dir, spec.template, kind="template")

    # parse_percent_source raises SpecInvalid on a malformed marker/slug — let it
    # propagate (a malformed input, not a finding).
    source_module = parse_percent_source(source_text)
    template_module = parse_percent_source(template_text)

    tree = _parse_ast(source_text)
    section_spans = _build_section_spans(source_text)
    input_root_dirs = _root_dirs(experiment_dir, spec.input_roots)
    source_root_dirs = _root_dirs(experiment_dir, spec.source_roots)

    findings: list[NotebookLintFinding] = []
    findings.extend(_check_structural_completeness(template_module.slugs, source_module.slugs))
    live_findings, unverifiable = _check_executes_live(
        tree, experiment_dir, input_root_dirs, section_spans
    )
    findings.extend(live_findings)
    findings.extend(
        _check_template_import_shadowed(
            [(s.slug, s.source) for s in source_module.sections],
            template_module.preamble,
            [(s.slug, s.source) for s in template_module.sections],
        )
    )
    linked = _check_linked_sources(tree, experiment_dir, source_root_dirs)

    return NotebookLintResult(
        findings=findings,
        unverifiable_paths=unverifiable,
        linked_sources=linked,
    )
