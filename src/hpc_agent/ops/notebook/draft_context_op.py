"""``notebook-draft-context`` — the drafting projection (run-#10 mechanization).

The verb that turns run #10's hand-written "drafting brief" into ONE deterministic
artifact (``docs/design/draft-context.md``). The drafting agent read N discovery
greps — engine signatures, call sites, a config's identity — every one derivable
mechanically from the template + declared roots. This ``query`` verb derives them
all and renders a code-authored, trusted-display markdown the agent reads instead.

Four sections, all AST / stat, never executing an import:

1. **template sections** — the template's slugs + cell prose, verbatim;
2. **resolved engines** — each name the template imports, resolved to its file
   under ``source_roots`` via the SAME machinery ``notebook-lint`` uses
   (:mod:`hpc_agent.ops.notebook.linked_sources` — one resolution definition),
   with the symbol's ``path:lineno``, its signature (``ast.unparse`` of the def's
   args), first docstring line, and ``module_sha``;
3. **name-match call sites** — ``Call`` nodes whose name matches an engine across
   ``source_roots``, capped with the cap DISCLOSED (no silent caps);
4. **inventory** — files + sha12 + size under ``input_roots`` and each
   ``inventory_roots`` entry; a file recorded in ``.hpc/data_manifest.json`` is
   CITED, not re-hashed (the Phase-1a reuse seam, read defensively by shape).

Altitude boundary: the projection LISTS, never NOMINATES — no "baseline" config,
no section ranking. Roots are OPAQUE; core never learns what a "config" is.
Read-only: the only write is the disposable content-keyed cache in the standard
cache home (:mod:`hpc_agent.state.draft_context_cache`); nothing under the
experiment dir is ever written.

Lives inside the ``notebook`` subject beside the lint whose resolution it shares,
reaching only same-subject ``ops.notebook.*`` and the ``state.*`` substrate.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.notebook_draft_context import (
    EngineCallSites,
    InventoryEntry,
    InventoryListing,
    NotebookDraftContextResult,
    NotebookDraftContextSpec,
    ResolvedEngine,
    TemplateSection,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.canonical import read_recorded_config
from hpc_agent.ops.notebook.linked_sources import resolve_module_file
from hpc_agent.state import draft_context_cache
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized

__all__ = ["notebook_draft_context"]

_PRIMITIVE = "notebook-draft-context"

#: Max name-match call sites listed per engine; the total is always reported and
#: ``truncated`` discloses when more existed (the no-silent-caps rule).
_CALL_SITE_CAP = 50

#: The data-manifest home + documented record shape (docs/design/data-manifest.md).
_DATA_MANIFEST_REL = ".hpc/data_manifest.json"


# ── I/O + resolution helpers ─────────────────────────────────────────────────


def _read_text(experiment_dir: Path, relpath: str, *, kind: str) -> str:
    """Read a caller-declared ``.py`` (relative → experiment_dir) or raise SpecInvalid."""
    path = Path(relpath)
    if not path.is_absolute():
        path = experiment_dir / path
    if not path.is_file():
        raise errors.SpecInvalid(f"{_PRIMITIVE} {kind} file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.SpecInvalid(
            f"{_PRIMITIVE} {kind} file could not be read: {path} ({exc})"
        ) from exc


def _root_dirs(experiment_dir: Path, roots: list[str]) -> list[Path]:
    """Resolve caller-declared roots to directories (relative → experiment_dir)."""
    out: list[Path] = []
    for r in roots:
        p = Path(r)
        out.append(p if p.is_absolute() else experiment_dir / p)
    return out


def _rel(experiment_dir: Path, path: Path) -> str:
    """Experiment-relative POSIX path when under the dir, else the absolute POSIX path."""
    try:
        return path.resolve().relative_to(experiment_dir.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _parse_tolerant(text: str) -> ast.Module | None:
    """``ast.parse`` returning ``None`` on a SyntaxError (a file mid-edit contributes nothing)."""
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _first_doc_line(node: ast.AST) -> str | None:
    """First non-blank line of a def/class/module docstring, or ``None``."""
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    if not doc:
        return None
    for line in doc.splitlines():
        if line.strip():
            return line.strip()
    return None


# ── section 1: template imports (the declared engines) ───────────────────────


def _template_engine_imports(tree: ast.Module) -> list[tuple[str, str, str | None]]:
    """``(bound_name, module, symbol|None)`` for every top-level import (first-wins).

    ``import M`` / ``import M.sub as x`` → ``(bound, full_module, None)`` (the whole
    module is the engine); ``from M import f as g`` → ``(g, M, f)``. Relative
    imports (``level > 0``) and ``import *`` are skipped — the ``notebook-lint``
    boundary. Document order, first occurrence of a bound name wins.
    """
    out: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound not in seen:
                    seen.add(bound)
                    out.append((bound, alias.name, None))
        elif isinstance(stmt, ast.ImportFrom):
            if (stmt.level and stmt.level > 0) or not stmt.module:
                continue
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                if bound not in seen:
                    seen.add(bound)
                    out.append((bound, stmt.module, alias.name))
    return out


def _locate_symbol(tree: ast.Module, symbol: str) -> tuple[int, str | None, str | None] | None:
    """``(lineno, signature, first_doc_line)`` for a top-level def/class named *symbol*.

    Signature is ``ast.unparse`` of a function's argument list; a class has none
    (``None``). ``None`` when the symbol is not a top-level definition in *tree*.
    """
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and stmt.name == symbol:
            return stmt.lineno, ast.unparse(stmt.args), _first_doc_line(stmt)
        if isinstance(stmt, ast.ClassDef) and stmt.name == symbol:
            return stmt.lineno, None, _first_doc_line(stmt)
    return None


def _resolve_engine(
    experiment_dir: Path,
    source_root_dirs: list[Path],
    bound: str,
    module: str,
    symbol: str | None,
) -> ResolvedEngine:
    """Resolve one template import to a :class:`ResolvedEngine` (never executes it)."""
    resolved = resolve_module_file(module, source_root_dirs)
    if resolved is None:
        # stdlib / site-packages / external — listed honestly, never dropped.
        return ResolvedEngine(name=bound, module=module, symbol=symbol, resolved=False)
    text = resolved.read_text(encoding="utf-8")
    tree = _parse_tolerant(text)
    engine = ResolvedEngine(
        name=bound,
        module=module,
        symbol=symbol,
        resolved=True,
        file=_rel(experiment_dir, resolved),
        module_sha=sha256_normalized(text),
    )
    if tree is None:
        return engine
    if symbol is not None:
        located = _locate_symbol(tree, symbol)
        if located is not None:
            engine = engine.model_copy(
                update={
                    "symbol_lineno": located[0],
                    "signature": located[1],
                    "doc": located[2],
                }
            )
    else:
        engine = engine.model_copy(update={"doc": _first_doc_line(tree)})
    return engine


# ── section 3: name-match call sites ─────────────────────────────────────────


def _iter_py_files(root_dirs: list[Path]) -> list[Path]:
    """Every ``.py`` file under the roots, deduped + sorted (deterministic)."""
    seen: set[Path] = set()
    for root in root_dirs:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path.is_file():
                seen.add(path.resolve())
    return sorted(seen)


def _called_name(node: ast.Call) -> str | None:
    """The called simple name: ``f(...)`` → ``f``; ``obj.f(...)`` → ``f``; else ``None``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _collect_call_sites(experiment_dir: Path, source_root_dirs: list[Path]) -> dict[str, list[str]]:
    """Map called-name → sorted ``path:lineno`` sites across the source trees."""
    by_name: dict[str, list[str]] = {}
    for path in _iter_py_files(source_root_dirs):
        tree = _parse_tolerant(path.read_text(encoding="utf-8"))
        if tree is None:
            continue
        rel = _rel(experiment_dir, path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _called_name(node)
                if name is not None:
                    by_name.setdefault(name, []).append(f"{rel}:{node.lineno}")
    for name in by_name:
        by_name[name].sort()
    return by_name


def _engine_call_sites(
    engines: list[ResolvedEngine], sites_by_name: dict[str, list[str]]
) -> list[EngineCallSites]:
    """Cap-disclosed call-site groups, one per distinct engine name (source order)."""
    out: list[EngineCallSites] = []
    seen: set[str] = set()
    for engine in engines:
        if engine.name in seen:
            continue
        seen.add(engine.name)
        all_sites = sites_by_name.get(engine.name, [])
        out.append(
            EngineCallSites(
                name=engine.name,
                sites=all_sites[:_CALL_SITE_CAP],
                count=len(all_sites),
                cap=_CALL_SITE_CAP,
                truncated=len(all_sites) > _CALL_SITE_CAP,
            )
        )
    return out


# ── section 4: inventory ─────────────────────────────────────────────────────


def _load_manifest(experiment_dir: Path) -> dict[str, dict]:
    """The data manifest as ``{relpath: {sha256, size, ...}}``, or ``{}`` if absent.

    Read DEFENSIVELY by the documented shape (Phase 1a is built in parallel): any
    value that is a mapping carrying a ``sha256`` is a file record; anything else
    (a doc-sha meta field, a malformed entry) is skipped. Absent / unreadable /
    non-object file → ``{}`` (fall back to hashing).
    """
    path = experiment_dir / _DATA_MANIFEST_REL
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, dict] = {}
    for rel, entry in doc.items():
        if isinstance(entry, dict) and isinstance(entry.get("sha256"), str):
            out[rel] = entry
    return out


def _inventory_entry(experiment_dir: Path, path: Path, manifest: dict[str, dict]) -> InventoryEntry:
    """One file's inventory entry — cited from the manifest when recorded, else hashed."""
    rel = _rel(experiment_dir, path)
    record = manifest.get(rel)
    if record is not None:
        size = record.get("size")
        return InventoryEntry(
            relpath=rel,
            sha12=str(record["sha256"])[:12],
            size=int(size) if isinstance(size, int) else path.stat().st_size,
            cited=True,
        )
    sha12 = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return InventoryEntry(relpath=rel, sha12=sha12, size=path.stat().st_size, cited=False)


def _listing_for_root(
    experiment_dir: Path, root: str, kind: str, manifest: dict[str, dict]
) -> InventoryListing:
    """Files under one declared root, sorted, cited-or-hashed."""
    root_dir = Path(root) if Path(root).is_absolute() else experiment_dir / root
    entries: list[InventoryEntry] = []
    if root_dir.is_dir():
        files = sorted(p.resolve() for p in root_dir.rglob("*") if p.is_file())
        entries = [_inventory_entry(experiment_dir, p, manifest) for p in files]
    return InventoryListing(
        root=root,
        kind=kind,
        entries=entries,
        manifest_cited=any(e.cited for e in entries),
    )


# ── render ───────────────────────────────────────────────────────────────────


def _render_markdown(result: NotebookDraftContextResult) -> str:
    """Deterministic, code-authored markdown (trusted-display; no LLM prose)."""
    lines: list[str] = ["# Notebook draft context", ""]

    lines.append("## template sections")
    lines.append("")
    if not result.template_sections:
        lines.append("(no sections)")
        lines.append("")
    for sec in result.template_sections:
        lines.append(f"### {sec.slug}")
        lines.append("")
        lines.append("```python")
        lines.append(sec.source.rstrip("\n"))
        lines.append("```")
        lines.append("")

    lines.append("## resolved engines")
    lines.append("")
    if not result.resolved_engines:
        lines.append("(the template imports nothing)")
        lines.append("")
    for eng in result.resolved_engines:
        if not eng.resolved:
            lines.append(
                f"- {eng.name} ({eng.module}) — unresolved under source_roots (external/stdlib)"
            )
            continue
        loc = f"{eng.file}:{eng.symbol_lineno}" if eng.symbol_lineno is not None else eng.file
        sig = f"  `{eng.signature}`" if eng.signature else ""
        lines.append(f"- {eng.name} ({eng.module}) -> {loc}{sig}")
        if eng.doc:
            lines.append(f"  - {eng.doc}")
        lines.append(f"  - module_sha: {eng.module_sha}")
    lines.append("")

    lines.append("## name-match call sites")
    lines.append("")
    lines.append("(name-match = AST call-name identity, not type resolution)")
    lines.append("")
    for grp in result.call_sites:
        header = f"### {grp.name} — {grp.count} site(s)"
        if grp.truncated:
            header += f" (showing first {grp.cap})"
        lines.append(header)
        lines.append("")
        for site in grp.sites:
            lines.append(f"- {site}")
        if not grp.sites:
            lines.append("- (none)")
        lines.append("")

    lines.append("## inventory")
    lines.append("")
    if not result.inventory:
        lines.append("(no input_roots or inventory_roots declared)")
        lines.append("")
    for listing in result.inventory:
        cite = " [manifest-cited]" if listing.manifest_cited else ""
        lines.append(f"### {listing.root} ({listing.kind}){cite}")
        lines.append("")
        for entry in listing.entries:
            tag = " [cited]" if entry.cited else ""
            lines.append(f"- {entry.relpath}  {entry.sha12}  {entry.size}B{tag}")
        if not listing.entries:
            lines.append("- (empty)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── the projection ───────────────────────────────────────────────────────────


def _compute(
    experiment_dir: Path,
    template_relpath: str,
    source_roots: list[str],
    input_roots: list[str],
    inventory_roots: list[str],
) -> NotebookDraftContextResult:
    """Build the projection from scratch (cache miss path)."""
    template_text = _read_text(experiment_dir, template_relpath, kind="template")
    parsed = parse_percent_source(template_text)
    template_sections = [TemplateSection(slug=s.slug, source=s.source) for s in parsed.sections]

    template_tree = _parse_tolerant(template_text)
    source_root_dirs = _root_dirs(experiment_dir, source_roots)
    engines: list[ResolvedEngine] = []
    if template_tree is not None:
        for bound, module, symbol in _template_engine_imports(template_tree):
            engines.append(_resolve_engine(experiment_dir, source_root_dirs, bound, module, symbol))

    sites_by_name = _collect_call_sites(experiment_dir, source_root_dirs)
    call_sites = _engine_call_sites(engines, sites_by_name)

    manifest = _load_manifest(experiment_dir)
    inventory: list[InventoryListing] = []
    for root in input_roots:
        inventory.append(_listing_for_root(experiment_dir, root, "input", manifest))
    for root in inventory_roots:
        inventory.append(_listing_for_root(experiment_dir, root, "inventory", manifest))

    result = NotebookDraftContextResult(
        template_sections=template_sections,
        resolved_engines=engines,
        call_sites=call_sites,
        inventory=inventory,
        source_roots=source_roots,
        input_roots=input_roots,
    )
    return result.model_copy(update={"markdown": _render_markdown(result)})


def _stat_files(
    experiment_dir: Path,
    template_relpath: str,
    source_roots: list[str],
    input_roots: list[str],
    inventory_roots: list[str],
) -> list[Path]:
    """Every file whose bytes feed the projection — the cache fingerprint's stat set."""
    files: list[Path] = []
    tpl = Path(template_relpath)
    files.append(tpl if tpl.is_absolute() else experiment_dir / tpl)
    files.extend(_iter_py_files(_root_dirs(experiment_dir, source_roots)))
    for root in input_roots + inventory_roots:
        root_dir = Path(root) if Path(root).is_absolute() else experiment_dir / root
        if root_dir.is_dir():
            files.extend(sorted(p.resolve() for p in root_dir.rglob("*") if p.is_file()))
    files.append(experiment_dir / _DATA_MANIFEST_REL)
    return files


@primitive(
    name=_PRIMITIVE,
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Render the deterministic DRAFTING context for a notebook-audit "
            "template: the template's slugs + cell prose verbatim; each engine "
            "the template imports resolved to its defining file under source_roots "
            "(path:lineno, signature, first docstring line, module_sha - the same "
            "resolution notebook-lint uses, never executing an import); name-match "
            "Call sites across source_roots (path:lineno, count, capped with the "
            "cap disclosed); and an inventory of files + sha12 + size under "
            "input_roots and each inventory_roots entry (a file recorded in "
            ".hpc/data_manifest.json is cited, not re-hashed). Roots default from "
            "the audit's recorded config when audit_id is given. The projection "
            "LISTS, never nominates - no baseline config, no section ranking. "
            "Read-only: the only write is the disposable content-keyed cache in "
            "the standard cache home. The result includes markdown - the "
            "code-rendered projection the drafting agent reads and the skill "
            "relays VERBATIM."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=NotebookDraftContextSpec,
        schema_ref=SchemaRef(input="notebook_draft_context"),
    ),
    agent_facing=True,
)
def notebook_draft_context(
    *, experiment_dir: Path, spec: NotebookDraftContextSpec
) -> NotebookDraftContextResult:
    """Build the drafting projection for *template* against the declared roots.

    ``source_roots`` / ``input_roots`` default from the audit's recorded
    configuration when ``spec.audit_id`` is given and the field is ``null``
    (the one-declaration reuse rule). Content-keyed cached in the standard cache
    home (recompute-on-read; a stat change misses and recomputes). Read-only —
    nothing under the experiment dir is written.

    Raises :class:`errors.SpecInvalid` on an unreadable template or a malformed
    percent-format module (the parser's own boundary guards).
    """
    experiment_dir = Path(experiment_dir)

    # Effective roots: explicit list wins; null falls back to the recorded config
    # (interview.json audited_source, else the journaled record) when audit_id is
    # given; else empty — the one "what are my inputs" declaration reused.
    recorded = read_recorded_config(experiment_dir, spec.audit_id)
    source_roots = (
        spec.source_roots if spec.source_roots is not None else list(recorded.source_roots)
    )
    input_roots = spec.input_roots if spec.input_roots is not None else list(recorded.input_roots)
    inventory_roots = list(spec.inventory_roots)

    # Loud existence check with the verb's own wording before the cache lookup
    # (a missing template is a malformed spec, not a cache miss).
    _read_text(experiment_dir, spec.template, kind="template")

    spec_key = {
        "template": spec.template,
        "source_roots": source_roots,
        "input_roots": input_roots,
        "inventory_roots": inventory_roots,
        "audit_id": spec.audit_id,
    }
    stat_files = _stat_files(
        experiment_dir, spec.template, source_roots, input_roots, inventory_roots
    )
    key = draft_context_cache.fingerprint(spec_key, stat_files)
    cached = draft_context_cache.load(key)
    if cached is not None:
        try:
            return NotebookDraftContextResult.model_validate(cached)
        except Exception:  # noqa: BLE001 — a stale/incompatible payload just recomputes
            pass

    result = _compute(experiment_dir, spec.template, source_roots, input_roots, inventory_roots)
    draft_context_cache.store(key, result.model_dump(mode="json"))
    return result
