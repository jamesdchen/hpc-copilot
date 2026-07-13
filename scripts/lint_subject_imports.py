"""CI lint: subjects under ``ops/`` and ``meta/`` must not cross-import.

In the post-reorg 5-role layout, each top-level directory under
``src/hpc_agent/ops/`` and ``src/hpc_agent/meta/`` is a *subject* — a
self-contained vertical slice (e.g. ``ops/jobs/``, ``ops/files/``,
``meta/registry/``). Subjects compose horizontally via the shared
``hpc_agent.infra.*`` and ``hpc_agent.state.*`` substrate; they MUST NOT
reach sideways into each other's internals.

This lint enforces that rule by AST-scanning every ``.py`` file under
``src/hpc_agent/ops/<subject>/`` and ``src/hpc_agent/meta/<subject>/``
and rejecting any ``from hpc_agent.<role>.<other_subject>...`` import
where ``<other_subject>`` differs from the file's own subject. The
evasive spellings are covered too: relative imports are resolved against
the importing file's package (``from ...meta.registry import x`` climbs
parents and crosses subjects like its absolute spelling), and
``from hpc_agent.<role> import <subject>`` binds the subject through an
alias without its dotted path ever appearing in the ``from`` clause.
EVERY candidate is checked against the real subject directories: only a
directory under a role root is a subject, so re-exported functions and
role-root MODULE files (shared op-level surface like
``ops/evidence_embed.py`` — design-pinned homes importable from any
subject) don't false-positive.

Allowed cross-cutting roots (these aren't subjects, they're substrate):

* ``hpc_agent.infra.*``
* ``hpc_agent.state.*``

The script handles absent role roots gracefully (post-reorg both
``ops/`` and ``meta/`` exist; the absent-role branch survives so the
script stays useful if a future role root is added late). Every
per-file import violation surfaces a ``path:lineno: cross-subject
import: ...`` line and the script exits 1.

Directional role rules
----------------------

Beyond the symmetric within-role cross-subject rule above, the layering
also has *directional* rules between whole role roots. A substrate role
must never reach UP into the vertical slices it underlies:

* ``infra`` must not import ``ops`` (infra is the bottom substrate).
* ``incorporation`` must not import ``ops`` or ``meta`` (the packaging /
  spec-building role feeds submit; the reverse edge is a cycle).
* ``_kernel`` must not import ``ops`` — with an ENUMERATED sanctioned
  allowlist: a handful of kernel→ops seams are inherent (the drive loop
  routes ops verbs; stop-hook guards probe ops capability helpers).
* ``state`` must not import ``ops`` — with an enumerated allowlist for
  the one documented inversion (the run-story renderer's lazy
  consumption-ledger read).

Each direction carries its own allowlist of sanctioned
``(source_file, target_module_prefix)`` exceptions. Allowlist entries
flagged ``# TEMP`` are known violations being removed in sibling
layering work; they must be deleted (and the lint re-run) once that
work lands. See :data:`DIRECTIONAL_RULES`.

Role-root ``composes=`` pass
----------------------------

Files sitting DIRECTLY under ``ops/`` (not inside a subject directory)
are the composition layer — workflows, block builders, and views that
assemble subjects. They are allowed to reach into subjects, but only the
subjects they actually compose. This pass reads each root file's own
``@primitive(composes=[...])`` declaration (the registry data, resolved
from the file's own import bindings) and flags any cross-subject import
whose subject is neither composed nor on the enumerated
:data:`ROLE_ROOT_ALLOW` inventory of sanctioned shared-helper reaches.
It ratchets: a NEW cross-subject reach from a root file must be declared
via ``composes=`` or added to the inventory explicitly.
"""

from __future__ import annotations

import ast
import dataclasses
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src" / "hpc_agent"

# Top-level role directories under ``src/hpc_agent/`` whose immediate
# children are subjects. Add new role roots here when the reorg grows.
SUBJECT_ROLES: tuple[str, ...] = ("ops", "meta")

# Per-role allowed import prefixes that aren't themselves subjects.
# Imports under these prefixes are always fine regardless of which
# subject the importing file lives in.
ALLOWED_NON_SUBJECT_ROOTS: tuple[str, ...] = (
    "hpc_agent.infra",
    "hpc_agent.state",
)


def _subject_of(path: Path, role_root: Path) -> str | None:
    """Return the subject name for a file under ``role_root``, or None
    if the file isn't inside a subject directory (e.g. it's directly in
    ``role_root`` itself, not in a child)."""
    try:
        rel = path.resolve().relative_to(role_root.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        # ``role_root/<file>.py`` — not inside a subject.
        return None
    return parts[0]


def _imported_subject(module: str, role: str) -> str | None:
    """If ``module`` is a ``hpc_agent.<role>.<subject>...`` import,
    return ``<subject>``. Otherwise return None.

    Examples (role=``ops``):

    * ``hpc_agent.ops.jobs.api`` → ``"jobs"``
    * ``hpc_agent.ops.jobs``     → ``"jobs"``
    * ``hpc_agent.ops``          → None (no subject in the path)
    * ``hpc_agent.meta.registry``→ None (different role)
    """
    prefix = f"hpc_agent.{role}."
    if module == f"hpc_agent.{role}":
        return None
    if not module.startswith(prefix):
        return None
    rest = module[len(prefix) :]
    head = rest.split(".", 1)[0]
    return head or None


def _is_allowed_non_subject(module: str) -> bool:
    for root in ALLOWED_NON_SUBJECT_ROOTS:
        if module == root or module.startswith(root + "."):
            return True
    return False


def _iter_imports(tree: ast.AST, module_package: str) -> list[tuple[int, str]]:
    """Yield ``(lineno, module_name)`` for every module an import
    statement in ``tree`` could bind.

    Relative imports are resolved against *module_package* — a
    ``from ...meta.registry import x`` climbs parents and crosses subjects
    exactly like its absolute spelling, so it must not be skipped. For
    ``from pkg import name``, ``pkg.name`` is additionally yielded per
    alias: when ``name`` is a subject package, that form binds it just
    like ``import pkg.name``. The caller checks every candidate against
    the real subject directories, so an alias that is a re-exported
    function never false-positives.
    """
    out: list[tuple[int, str]] = []
    pkg_parts = module_package.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module: str | None
            if node.level:
                if node.level > len(pkg_parts):
                    continue  # climbs above the distribution root — broken import
                base = ".".join(pkg_parts[: len(pkg_parts) - (node.level - 1)])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module
            if not module:
                continue
            out.append((node.lineno, module))
            out.extend(
                (node.lineno, f"{module}.{alias.name}") for alias in node.names if alias.name != "*"
            )
    return out


def lint_file(
    path: Path,
    own_role: str,
    own_subject: str,
    module_package: str,
    subjects_by_role: dict[str, set[str]],
) -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` per cross-subject import violation."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[tuple[int, str]] = []
    # ``from pkg.sub import name`` yields both ``pkg.sub`` and
    # ``pkg.sub.name`` candidates for one line — report each crossing once.
    seen: set[tuple[int, str, str]] = set()
    for lineno, module in _iter_imports(tree, module_package):
        if _is_allowed_non_subject(module):
            continue
        # Check both roles — a file in ``ops/foo`` may not import from
        # ``meta/bar`` either (different role still counts as a
        # different subject for cross-import purposes).
        for role in SUBJECT_ROLES:
            other = _imported_subject(module, role)
            if other is None:
                continue
            if role == own_role and other == own_subject:
                # Same subject as ourselves — allowed.
                continue
            if other not in subjects_by_role.get(role, set()):
                # Not a subject DIRECTORY: a role-root module file
                # (``from hpc_agent.ops.evidence_embed import ...``) or a
                # re-exported helper bound via
                # ``from hpc_agent.<role> import <name>`` — subjects are
                # directories, so neither is a subject crossing.
                continue
            if (lineno, role, other) in seen:
                continue
            seen.add((lineno, role, other))
            findings.append(
                (
                    lineno,
                    f"cross-subject import: {own_role}/{own_subject} "
                    f"imports {role}/{other} ({module})",
                )
            )
    findings.sort(key=lambda f: f[0])
    return findings


def iter_targets(scan_root: Path) -> list[tuple[Path, str, str]]:
    """Yield ``(file, role, subject)`` for every ``.py`` file inside a
    subject directory under ``scan_root/<role>/<subject>/``."""
    targets: list[tuple[Path, str, str]] = []
    for role in SUBJECT_ROLES:
        role_root = scan_root / role
        if not role_root.exists():
            continue
        for subject_dir in sorted(p for p in role_root.iterdir() if p.is_dir()):
            subject = subject_dir.name
            for py in sorted(subject_dir.rglob("*.py")):
                if not py.is_file():
                    continue
                targets.append((py, role, subject))
    return targets


def _subjects_by_role(scan_root: Path) -> dict[str, set[str]]:
    """The real subject directories per role — the reference set for
    deciding whether an alias-derived import names a subject."""
    return {
        role: {p.name for p in (scan_root / role).iterdir() if p.is_dir()}
        for role in SUBJECT_ROLES
        if (scan_root / role).exists()
    }


def _module_package(path: Path, scan_root: Path) -> str:
    """Dotted package containing the module at *path* — anchors
    relative-import resolution (the scan root corresponds to ``hpc_agent``)."""
    rel = path.resolve().relative_to(scan_root.resolve())
    return ".".join(["hpc_agent", *rel.parts[:-1]])


# ---------------------------------------------------------------------------
# Directional role rules (P1 + N3)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DirectionalRule:
    """A substrate role that must not reach up into higher role roots.

    ``allow`` enumerates sanctioned ``(source_file, target_module_prefix)``
    exceptions, where ``source_file`` is the POSIX path relative to the
    scan root (``src/hpc_agent``) and a candidate import matches when its
    module equals the prefix or is nested under it. ``temporary`` marks a
    rule whose allowlist holds KNOWN violations being removed by sibling
    layering work — those entries must be deleted once that work lands.
    """

    source_role: str
    target_roles: tuple[str, ...]
    allow: frozenset[tuple[str, str]]
    temporary: bool = False


# infra is the bottom substrate — it must never import an ops slice.
# No sanctioned exceptions: the former transport→ops.transfer edge was the
# only one, cleared when the manifest/prune helpers moved into infra/.
_INFRA_TO_OPS = DirectionalRule(
    source_role="infra",
    target_roles=("ops",),
    allow=frozenset(),
    temporary=False,
)

# incorporation feeds submit; the reverse EAGER edge into ops/meta is a cycle.
# The submit_spec<->submit_flow cycle was broken (the executor-shape guards
# moved to infra/executor_guard.py). One sanctioned LAZY exception remains:
# the auto-classifier invokes the classify-axis preflight VERB at call time
# (imports nested in function bodies, not import-time), the same call-time
# posture the state->ops run-story reader is sanctioned under.
_INCORPORATION_TO_OPS_META = DirectionalRule(
    source_role="incorporation",
    target_roles=("ops", "meta"),
    allow=frozenset(
        {
            ("incorporation/classify_axis_auto.py", "hpc_agent.ops.classify_axis_preflight"),
            # Lazy call-time read of the campaign manifest for campaign-shaped
            # submits (nested in a function try-block; a non-campaign submit
            # never reaches it) — the same sanctioned posture as the two edges
            # above, not the eager submit_spec<->submit_flow cycle (broken).
            ("incorporation/build/submit_spec.py", "hpc_agent.meta.campaign.manifest"),
        }
    ),
    temporary=False,
)

# _kernel must not import ops, EXCEPT the enumerated inherent seams:
# the drive loop routes ops verbs and stop-hook guards probe ops
# capability helpers. This allowlist is sanctioned + permanent; new
# kernel→ops edges are violations that must earn an explicit entry.
_KERNEL_TO_OPS = DirectionalRule(
    source_role="_kernel",
    target_roles=("ops",),
    allow=frozenset(
        {
            # Drive-loop verb routing (block_drive is the kernel drive loop).
            ("_kernel/lifecycle/block_drive.py", "hpc_agent.ops.field_ownership"),
            ("_kernel/lifecycle/block_drive.py", "hpc_agent.ops.overnight"),
            ("_kernel/lifecycle/block_drive.py", "hpc_agent.ops.block_gate"),
            # MCP surface exposes a few ops atoms directly; the elicitation /
            # render-digest half (which reaches these two) split into
            # mcp_elicitation.py.
            ("_kernel/extension/mcp_elicitation.py", "hpc_agent.ops.overnight"),
            ("_kernel/extension/mcp_elicitation.py", "hpc_agent.ops.notebook_view"),
            # Stop-hook / alert guards probe ops capability + notify helpers.
            ("_kernel/hooks/alert_count.py", "hpc_agent.ops.recover.notify"),
            (
                "_kernel/hooks/decision_rendezvous_stop_guard.py",
                "hpc_agent.ops.harness_capabilities",
            ),
            ("_kernel/hooks/skill_return_stop_guard.py", "hpc_agent.ops.harness_capabilities"),
            # relay_audit_stop is a subpackage; the two sanctioned seams live in
            # the entry (__init__) and the contradiction audit submodule.
            (
                "_kernel/hooks/relay_audit_stop/__init__.py",
                "hpc_agent.ops.harness_capabilities",
            ),
            (
                "_kernel/hooks/relay_audit_stop/_contradiction.py",
                "hpc_agent.ops.decision.verify_relay",
            ),
        }
    ),
    temporary=False,
)

# state must not import ops, EXCEPT the one documented inversion: the
# run-story renderer lazily reads the overnight consumption ledger.
_STATE_TO_OPS = DirectionalRule(
    source_role="state",
    target_roles=("ops",),
    allow=frozenset(
        {
            ("state/run_story.py", "hpc_agent.ops.overnight"),
        }
    ),
    temporary=False,
)

DIRECTIONAL_RULES: tuple[DirectionalRule, ...] = (
    _INFRA_TO_OPS,
    _INCORPORATION_TO_OPS_META,
    _KERNEL_TO_OPS,
    _STATE_TO_OPS,
)


def _targets_role(module: str, role: str) -> bool:
    """True if ``module`` is ``hpc_agent.<role>`` or nested under it."""
    return module == f"hpc_agent.{role}" or module.startswith(f"hpc_agent.{role}.")


def _dir_allowed(rule: DirectionalRule, source_rel: str, module: str) -> bool:
    for allow_rel, allow_prefix in rule.allow:
        if source_rel != allow_rel:
            continue
        if module == allow_prefix or module.startswith(allow_prefix + "."):
            return True
    return False


def lint_directional(scan_root: Path, rule: DirectionalRule) -> list[tuple[Path, int, str]]:
    """Return ``(file, lineno, message)`` per forbidden directional import."""
    out: list[tuple[Path, int, str]] = []
    role_root = scan_root / rule.source_role
    if not role_root.exists():
        return out
    targets = "/".join(rule.target_roles)
    for py in sorted(role_root.rglob("*.py")):
        if not py.is_file():
            continue
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        package = _module_package(py, scan_root)
        source_rel = py.resolve().relative_to(scan_root.resolve()).as_posix()
        # Group every target-role candidate module by (lineno, hit-role). One
        # ``from hpc_agent.ops import notebook_view`` line yields both the bare
        # ``hpc_agent.ops`` and the specific ``hpc_agent.ops.notebook_view``
        # candidate; the specific one is what the allowlist is keyed on, so we
        # judge the line by its most-specific candidates and only fall back to
        # the bare-role token when that is the ONLY thing imported.
        by_line: dict[tuple[int, str], list[str]] = {}
        for lineno, module in _iter_imports(tree, package):
            hit = next((tr for tr in rule.target_roles if _targets_role(module, tr)), None)
            if hit is None:
                continue
            by_line.setdefault((lineno, hit), []).append(module)
        for (lineno, hit), modules in sorted(by_line.items()):
            bare = f"hpc_agent.{hit}"
            specific = [m for m in modules if m != bare]
            judged = specific or modules
            if all(_dir_allowed(rule, source_rel, m) for m in judged):
                continue
            reported = next((m for m in judged if not _dir_allowed(rule, source_rel, m)), judged[0])
            out.append(
                (
                    py,
                    lineno,
                    f"forbidden layering import: {rule.source_role} must not import "
                    f"{targets} ({reported})",
                )
            )
    return out


# ---------------------------------------------------------------------------
# Role-root ``composes=`` pass (N2)
# ---------------------------------------------------------------------------

# Sanctioned cross-subject reaches from ``ops/`` role-root files that are
# NOT covered by an in-file ``@primitive(composes=[...])`` declaration.
# Keyed by ``(root_file_basename, target_role, target_subject)``. This is
# the enumerated inventory of shared-helper compositions; it ratchets, so
# a new root-file reach into a subject not listed here (and not declared
# via ``composes=``) fails the lint until it earns an entry.
ROLE_ROOT_ALLOW: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("aggregate_blocks.py", "ops", "aggregate"),
        ("aggregate_blocks.py", "ops", "monitor"),
        ("aggregate_flow.py", "ops", "aggregate"),
        ("aggregate_flow.py", "ops", "monitor"),
        ("attention_queue.py", "ops", "decision"),
        ("attention_queue.py", "ops", "recover"),
        ("attention_queue.py", "ops", "registration"),
        ("audit_preflight.py", "ops", "notebook"),
        ("audit_preflight.py", "ops", "recover"),
        ("auto_resume_flow.py", "ops", "recover"),
        ("campaign_refill.py", "meta", "campaign"),
        ("dag_frontier.py", "ops", "validate"),
        ("field_ownership.py", "ops", "submit"),
        ("monitor_flow.py", "ops", "aggregate"),
        ("monitor_flow.py", "ops", "monitor"),
        ("notebook_view.py", "ops", "notebook"),
        ("overnight.py", "meta", "campaign"),
        ("overnight.py", "ops", "recover"),
        ("pack_gate.py", "ops", "pack"),
        ("recover_flow.py", "ops", "recover"),
        ("registration_view.py", "ops", "registration"),
        ("resolve_and_recover_flow.py", "ops", "recover"),
        ("resolve_resources.py", "ops", "submit"),
        ("resolve_submit_inputs.py", "ops", "monitor"),
        ("revise_resolved.py", "ops", "submit"),
        ("scaffold_spec.py", "meta", "campaign"),
        ("scaffold_spec.py", "ops", "submit"),
        ("settle_run.py", "ops", "monitor"),
        ("status_blocks.py", "ops", "monitor"),
        ("status_blocks.py", "ops", "recover"),
        ("submit_blocks.py", "ops", "monitor"),
        ("submit_blocks.py", "ops", "recover"),
        ("submit_flow.py", "ops", "validate"),
        ("submit_pipeline.py", "ops", "validate"),
        ("supersession.py", "ops", "monitor"),
        ("verify_canary.py", "ops", "aggregate"),
        ("walk_submit_ambiguities.py", "ops", "submit"),
        ("write_run_sidecar.py", "ops", "monitor"),
    }
)


def _subject_target(module: str, subjects_by_role: dict[str, set[str]]) -> tuple[str, str] | None:
    """If ``module`` names a real subject directory, return
    ``(role, subject)``; else None."""
    for role in SUBJECT_ROLES:
        other = _imported_subject(module, role)
        if other is None:
            continue
        if other in subjects_by_role.get(role, set()):
            return (role, other)
    return None


def _composed_subjects(
    tree: ast.AST, name_to_subject: dict[str, tuple[str, str]]
) -> set[tuple[str, str]]:
    """Subjects a file declares it composes, resolved from its own
    ``@primitive(composes=[...])`` decorators against the file's import
    bindings. String-name ``composes=`` entries (bare primitive names)
    can't be resolved without the live registry, so they contribute
    nothing here — such reaches must earn a :data:`ROLE_ROOT_ALLOW` entry.
    """
    composed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if fname != "primitive":
            continue
        for kw in node.keywords:
            if kw.arg == "composes" and isinstance(kw.value, ast.List | ast.Tuple):
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Name):
                        composed.add(elt.id)
    return {name_to_subject[name] for name in composed if name in name_to_subject}


def lint_role_root(
    scan_root: Path,
    role: str,
    subjects_by_role: dict[str, set[str]],
    allowlist: frozenset[tuple[str, str, str]],
) -> list[tuple[Path, int, str]]:
    """Return ``(file, lineno, message)`` per undeclared cross-subject
    reach from a ``<role>/`` role-root MODULE file."""
    out: list[tuple[Path, int, str]] = []
    role_root = scan_root / role
    if not role_root.exists():
        return out
    for py in sorted(p for p in role_root.glob("*.py") if p.is_file() and p.name != "__init__.py"):
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        package = _module_package(py, scan_root)
        pkg_parts = package.split(".")
        name_to_subject: dict[str, tuple[str, str]] = {}
        crossings: dict[tuple[int, str, str], str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(pkg_parts):
                        continue
                    base = ".".join(pkg_parts[: len(pkg_parts) - (node.level - 1)])
                    module = f"{base}.{node.module}" if node.module else base
                else:
                    module = node.module
                if not module:
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    full = f"{module}.{alias.name}"
                    tgt = _subject_target(full, subjects_by_role) or _subject_target(
                        module, subjects_by_role
                    )
                    if tgt is None:
                        continue
                    crossings[(node.lineno, tgt[0], tgt[1])] = module
                    name_to_subject[alias.asname or alias.name] = tgt
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    tgt = _subject_target(alias.name, subjects_by_role)
                    if tgt is None:
                        continue
                    crossings[(node.lineno, tgt[0], tgt[1])] = alias.name
        allowed = _composed_subjects(tree, name_to_subject)
        for (lineno, trole, tsubject), module in sorted(crossings.items()):
            if (trole, tsubject) in allowed:
                continue
            if (py.name, trole, tsubject) in allowlist:
                continue
            out.append(
                (
                    py,
                    lineno,
                    f"role-root cross-subject import not declared via composes=: "
                    f"{role}/{py.name} imports {trole}/{tsubject} ({module})",
                )
            )
    return out


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    subjects = _subjects_by_role(root)
    failures = 0
    # Pass 1: within-role cross-subject imports (subjects under ops/meta).
    for path, role, subject in iter_targets(root):
        package = _module_package(path, root)
        for lineno, hint in lint_file(path, role, subject, package, subjects):
            print(f"{_rel(path)}:{lineno}: {hint}")
            failures += 1
    # Pass 2: directional role rules (infra/incorporation/_kernel/state).
    for rule in DIRECTIONAL_RULES:
        for path, lineno, hint in lint_directional(root, rule):
            print(f"{_rel(path)}:{lineno}: {hint}")
            failures += 1
    # Pass 3: role-root composes= governance for ops/ module files.
    for path, lineno, hint in lint_role_root(root, "ops", subjects, ROLE_ROOT_ALLOW):
        print(f"{_rel(path)}:{lineno}: {hint}")
        failures += 1
    if failures:
        print(
            f"\n{failures} layering import violation(s). Cross-subject reaches must "
            f"route through hpc_agent.infra.* / hpc_agent.state.*; substrate roles "
            f"(infra, incorporation, _kernel, state) must not import up into ops/meta; "
            f"role-root files must declare subject reaches via composes=.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
