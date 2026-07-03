r"""CI lint: every telemetry field declares its kind (cumulative vs delta).

Sibling to ``lint_no_raw_ssh.py`` / ``lint_no_blocklisted_commands.py``. Where
those keep a raw affordance out of the agent-facing prose, this one keeps the
**monitor telemetry** legible: every field emitted in the per-tick record and
every count field the summary/diff renderers consume must carry a *declared
kind* — ``cumulative`` (a running total) vs ``delta`` (a per-tick change) vs
``label`` (identifier / state / metadata).

Why it matters: the confusion class is the ``told 0 · complete 39/40`` bug —
a per-tick delta (``newly complete = 0`` this tick) read as a cumulative total
(``0 complete``), or a cumulative count read as a delta. Today the only thing
distinguishing the two on the wire is an informal ``+`` prefix in one renderer.
The design (``docs/design/human-amplification-blocks.md`` §5, "Tick telemetry
legibility is lintable") promotes that from convention to contract: every field
is labeled, rendering routes through the label, and *this lint is how "labeled"
is mechanized*. Per the determinism principle
(``docs/internals/engineering-principles.md``), a rule the code relies on is
enforced, not merely documented.

What it flags
-------------

A telemetry field that reaches a renderer or the tick record **without** an
entry in the single-source-of-truth registry
:data:`~hpc_agent.ops.monitor.summary.FIELD_KIND`. Concretely, on the two
owned source files it fires when:

* ``ops/monitor/tick_log.py`` emits a ``record`` dict key absent from
  ``FIELD_KIND`` (a new tick-record field shipped without declaring its kind —
  literally the ``told 0`` failure: a ``told`` count added with no cumulative
  vs delta label), or
* ``ops/monitor/summary.py`` renders a field — a ``_render_scalar("field", …)``
  argument, or a ``summary.get("field")`` / ``diff.get("field")`` key in a
  count renderer — that is absent from ``FIELD_KIND``, or
* the ``FIELD_KIND`` registry itself is missing (nothing declares any kind).

Scope
-----

* ``src/hpc_agent/ops/monitor/summary.py``  (registry + renderers)
* ``src/hpc_agent/ops/monitor/tick_log.py`` (tick-record emitter)

Every violation prints ``path: telemetry field '…' …`` and the script exits 1.
The fire path is exercised in ``tests/scripts/test_lint_telemetry_labels.py``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Repo-relative so the test can point ``main`` at a synthetic tree.
_SUMMARY_REL = Path("src/hpc_agent/ops/monitor/summary.py")
_TICK_LOG_REL = Path("src/hpc_agent/ops/monitor/tick_log.py")

# The registry variable, the render router, and the tick-record builder the
# lint keys off structurally.
_REGISTRY_NAME = "FIELD_KIND"
# Named render helpers whose first string-literal argument is a telemetry field
# that must be declared in FIELD_KIND. ``_render_scalar`` renders the cumulative
# counts + per-tick deltas; ``_format_kill_count`` renders the §5 kill ledger
# ("N requested, M confirmed gone"). A new render helper that reaches a field
# name must be added here so the lint keeps covering it.
_RENDER_FNS = frozenset({"_render_scalar", "_format_kill_count"})
_TICK_RECORD_VAR = "record"
_TICK_RECORD_FN = "_append_tick"
# Local names bound to the cumulative / delta count blocks in the renderers;
# their ``.get("field")`` keys are telemetry fields that must be declared.
_BLOCK_NAMES = frozenset({"summary", "diff"})


def _parse(path: Path) -> ast.Module | None:
    """Parse *path* to an AST module, or None if unreadable / not valid Python."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None


def _dict_str_keys(node: ast.Dict) -> set[str]:
    """String-literal keys of a dict literal (skips ``**unpack`` entries)."""
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _field_kind_keys(tree: ast.Module) -> set[str] | None:
    """Keys declared in the module-level ``FIELD_KIND`` mapping.

    Returns ``None`` when no ``FIELD_KIND`` binding exists (itself a violation:
    nothing declares any kind). A ``FIELD_KIND`` bound to a non-dict yields an
    empty set (present but declares nothing).
    """
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        else:
            continue
        if target != _REGISTRY_NAME:
            continue
        return _dict_str_keys(value) if isinstance(value, ast.Dict) else set()
    return None


def _tick_record_keys(tree: ast.Module) -> set[str]:
    """String keys of the ``record = {...}`` dict built in ``_append_tick``."""
    keys: set[str] = set()
    for fn in ast.walk(tree):
        if not (
            isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == _TICK_RECORD_FN
        ):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == _TICK_RECORD_VAR for t in node.targets)
            ):
                keys |= _dict_str_keys(node.value)
    return keys


def _referenced_fields(tree: ast.Module) -> set[str]:
    """Telemetry field names the renderers reference.

    Two forms, matching how the count renderers are written:

    * ``_render_scalar("field", …)`` — the render router's first argument.
    * ``summary.get("field")`` / ``diff.get("field")`` — a ``.get`` on a name
      bound to a cumulative / delta count block.
    """
    fields: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        func = node.func
        is_render_call = isinstance(func, ast.Name) and func.id in _RENDER_FNS
        is_block_get = (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id in _BLOCK_NAMES
        )
        if is_render_call or is_block_get:
            fields.add(first.value)
    return fields


def lint(repo: Path) -> list[str]:
    """Return one message per telemetry field emitted without a declared kind."""
    summary_tree = _parse(repo / _SUMMARY_REL)
    if summary_tree is None:
        return [f"{_SUMMARY_REL.as_posix()}: could not parse the telemetry registry source"]

    kinds = _field_kind_keys(summary_tree)
    if kinds is None:
        return [
            f"{_SUMMARY_REL.as_posix()}: no {_REGISTRY_NAME} registry — every telemetry "
            f"field must declare cumulative vs delta (design §5)"
        ]

    findings: list[str] = []
    for field in sorted(_referenced_fields(summary_tree)):
        if field not in kinds:
            findings.append(
                f"{_SUMMARY_REL.as_posix()}: telemetry field {field!r} rendered but absent "
                f"from {_REGISTRY_NAME} — declare it cumulative or delta"
            )

    tick_tree = _parse(repo / _TICK_LOG_REL)
    if tick_tree is not None:
        for field in sorted(_tick_record_keys(tick_tree)):
            if field not in kinds:
                findings.append(
                    f"{_TICK_LOG_REL.as_posix()}: tick-record field {field!r} emitted but "
                    f"absent from {_REGISTRY_NAME} — declare its kind (cumulative | delta | label)"
                )
    return findings


def main(repo: Path | None = None) -> int:
    root = repo if repo is not None else REPO
    findings = lint(root)
    for msg in findings:
        print(msg)
    if findings:
        print(
            f"\n{len(findings)} telemetry field(s) emitted without a declared kind. Every "
            f"field in the tick record and the summary/diff renderers must be declared "
            f"cumulative | delta | label in {_REGISTRY_NAME} ({_SUMMARY_REL.as_posix()}) so a "
            f"cumulative count never renders as a delta and a delta always carries its `+` "
            f"marker (the `told 0 · complete 39/40` confusion class, design §5).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
