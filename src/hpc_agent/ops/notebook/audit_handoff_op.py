"""``audit-handoff`` — project the durable audit records into a DRAFT InterviewSpec.

The audit→interview bridge (``docs/design/notebook-audit.md`` audit-handoff note).
After a notebook audit passes, the submit interview re-derives facts the audit
flow already holds; the interim fix was a PROSE mapping in ``/new-experiment-hpc``
step 4 — load-bearing prose, the rot class. This read-only ``query`` verb is the
code seat: it emits a DRAFT ``InterviewSpec`` from DURABLE RECORDS ONLY.

Every draft field is either DERIVED (and disclosed) or an explicit PLACEHOLDER the
caller must fill — the verb NEVER guesses. A guessed field would become a
journaled fact once the caller passes the draft to the interview (the ``halo_expr``
failure class: an invented value laundered into provenance). So:

* ``goal`` + ``task_axes`` come from the audit-OPEN intent utterances the human
  typed, journaled on the ``notebook-audit-config`` seat
  (:func:`hpc_agent.state.notebook_audit.read_audit_intent`). No recorded goal →
  a ``goal`` placeholder, never an invented sentence.
* ``audited_source`` is built from the verb inputs (``source`` / ``audit_id`` /
  ``template``) plus the recorded config roots
  (:func:`hpc_agent.ops.notebook.canonical.read_recorded_config`).
* ``entry_point`` is DETECTED by scanning the source for ``@register_run``
  functions — exactly one is filled; zero or several is a disclosed placeholder
  (the ambiguity is surfaced, never resolved by picking).
* ``summary_artifact_candidates`` are DETECTED by an AST scan for writes under
  ``$HPC_RESULT_DIR`` — detected-and-disclosed, never invented; multiple
  candidates are listed and the caller confirms.
* ``task_generator`` / ``task_count`` / ``produced_by`` are ALWAYS placeholders —
  a materializer/count/identity the audit records never hold.

Deterministic: the result is a pure function of the journal records + the source
bytes (sorted candidate lists, fixed placeholder/disclosure order, no
timestamps), so the same records project a byte-identical draft.

Scanner coverage (declared honestly, and pinned by
``tests/ops/notebook/test_audit_handoff.py``): the ``$HPC_RESULT_DIR`` write scan
recognises a result-dir base bound to ``os.environ["HPC_RESULT_DIR"]`` /
``os.environ.get(...)`` / ``os.getenv(...)`` (also ``RESULT_DIR``), optionally
wrapped in ``Path(...)``, plus one hop of name aliasing; and it extracts the
filename joined onto that base in three forms — ``os.path.join(base, "a.json")``,
``Path(base) / "a.json"`` (the ``/`` operator), and ``f"{base}/a.json"``
(f-string). A computed tail (a non-literal join arg, an f-string with a
formatted tail) is DISCLOSED in ``unverifiable_result_writes`` rather than
dropped. NOT covered (and therefore silently a miss, which is safe — a missed
candidate is added by hand, never a false journaled fact): ``str.format`` / ``%``
/ ``+`` construction, ``str.join``, and transitive aliasing through an
intermediate joined directory. The scan does NOT inspect the surrounding call
for a write vocabulary — every path BUILT on ``$HPC_RESULT_DIR`` is a candidate
output (identity + path arithmetic only; naming a write function would cross the
Q1 library-knowledge boundary).

Lives in the ``ops/notebook/`` subject, reaching only same-subject
``ops.notebook.*`` and the ``state.*`` substrate — the subject-imports lint is
satisfied by construction. No SSH, no scheduler; a pure local read.
"""

from __future__ import annotations

import ast
from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.audit_handoff import (
    AuditHandoffResult,
    AuditHandoffSpec,
    HandoffPlaceholder,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.notebook.canonical import read_recorded_config
from hpc_agent.state.notebook_audit import read_audit_intent

__all__ = ["audit_handoff"]

_PRIMITIVE = "audit-handoff"

#: The framework's per-task output-dir env vars (substrate — the framework's own
#: dispatch contract, not third-party knowledge). A path built on either is a
#: candidate output.
_RESULT_DIR_KEYS = frozenset({"HPC_RESULT_DIR", "RESULT_DIR"})

#: Env-reader callables whose FIRST argument is the env-var name.
_ENV_GETTERS = frozenset({"os.environ.get", "environ.get", "os.getenv", "getenv"})

#: ``Path`` constructors that wrap a base expression.
_PATH_CTORS = frozenset({"Path", "pathlib.Path"})


def _read_source(experiment_dir: Path, relpath: str, label: str) -> str:
    """Read an experiment-relative ``.py`` source, or raise a naming SpecInvalid."""
    path = (Path(experiment_dir) / relpath).resolve()
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"{_PRIMITIVE}: {label} not found at {relpath!r} (resolved {path})"
        ) from exc
    except OSError as exc:
        raise errors.SpecInvalid(
            f"{_PRIMITIVE}: {label} at {relpath!r} could not be read: {exc}"
        ) from exc


def _dotted_name(func: ast.expr) -> str | None:
    """The dotted name of a ``Name``/``Attribute`` chain, or ``None`` (the lint idiom)."""
    parts: list[str] = []
    node: ast.expr = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _const_str(node: ast.expr) -> str | None:
    """The value of a ``str`` constant node, or ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


# ── @register_run entry-point scan ───────────────────────────────────────────


def _decorator_is_register_run(dec: ast.expr) -> bool:
    """True iff *dec* is a ``@register_run`` (bare, dotted, or called) decorator."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    dotted = _dotted_name(target) if isinstance(target, ast.Name | ast.Attribute) else None
    return dotted is not None and dotted.split(".")[-1] == "register_run"


def _scan_register_run(tree: ast.Module) -> list[str]:
    """Sorted names of every ``@register_run``-decorated function in the module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            _decorator_is_register_run(d) for d in node.decorator_list
        ):
            names.add(node.name)
    return sorted(names)


# ── $HPC_RESULT_DIR write scan ────────────────────────────────────────────────


def _is_result_env_expr(node: ast.expr) -> bool:
    """True iff *node* reads a result-dir env var (subscript or getter call)."""
    if isinstance(node, ast.Subscript):
        value = node.value
        base = _dotted_name(value) if isinstance(value, ast.Name | ast.Attribute) else None
        if base is not None and base.split(".")[-1] == "environ":
            return _const_str(node.slice) in _RESULT_DIR_KEYS
        return False
    if isinstance(node, ast.Call) and node.args:
        dotted = _dotted_name(node.func)
        if dotted in _ENV_GETTERS:
            return _const_str(node.args[0]) in _RESULT_DIR_KEYS
    return False


def _is_result_base(node: ast.expr, base_names: set[str]) -> bool:
    """True iff *node* denotes the result dir: an env expr, ``Path(env|base)``, or a base name."""
    if _is_result_env_expr(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in base_names
    if isinstance(node, ast.Call) and node.args and _dotted_name(node.func) in _PATH_CTORS:
        return _is_result_base(node.args[0], base_names)
    return False


def _collect_base_names(tree: ast.Module) -> set[str]:
    """Names bound to the result dir — env expr / ``Path(...)`` wrap / one alias hop.

    Fixpoint over assignments so ``d = os.environ["HPC_RESULT_DIR"]`` then
    ``p = Path(d)`` both register. Only single-``Name``-target assignments count
    (a tuple-unpack or attribute target binds no simple result-dir name we track).
    """
    assigns: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assigns.append((target.id, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                assigns.append((node.target.id, node.value))
    base_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, value in assigns:
            if name not in base_names and _is_result_base(value, base_names):
                base_names.add(name)
                changed = True
    return base_names


def _norm_path(*segments: str) -> str:
    """Join *segments* with ``/`` and normalise separators, stripping a leading ``./``."""
    joined = "/".join(s.replace("\\", "/") for s in segments if s)
    while joined.startswith("./"):
        joined = joined[2:]
    return joined.strip("/")


def _segments_from_join(node: ast.Call, base_names: set[str]) -> tuple[str | None, bool]:
    """``os.path.join(base, "a", "b.json")`` → (path, is_candidate).

    Returns ``(literal_path, True)`` when the first arg is a result base and every
    trailing arg is a str literal; ``(unparsed, False)`` when a trailing arg is
    non-literal (a computed tail, disclosed as unverifiable); ``(None, False)``
    when it is not a result-dir join.
    """
    if _dotted_name(node.func) not in ("os.path.join", "path.join") or len(node.args) < 2:
        return None, False
    if not _is_result_base(node.args[0], base_names):
        return None, False
    segments: list[str] = []
    for arg in node.args[1:]:
        literal = _const_str(arg)
        if literal is None:
            return ast.unparse(node), False  # computed tail → unverifiable
        segments.append(literal)
    path = _norm_path(*segments)
    return (path or None), bool(path)


def _segments_from_binop(node: ast.BinOp, base_names: set[str]) -> tuple[str | None, bool]:
    """``Path(base) / "a" / "b.json"`` → (path, is_candidate).

    Flattens the left-associative ``/`` chain: the innermost left operand must be
    a result base; each ``/`` right operand must be a str literal. A non-literal
    segment makes the whole expression unverifiable.
    """
    if not isinstance(node.op, ast.Div):
        return None, False
    right_segments: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        literal = _const_str(cur.right)
        if literal is None:
            # A computed segment: only disclose if the chain is rooted at a base.
            if _binop_rooted_at_base(node, base_names):
                return ast.unparse(node), False
            return None, False
        right_segments.append(literal)
        cur = cur.left
    if not _is_result_base(cur, base_names):
        return None, False
    right_segments.reverse()
    path = _norm_path(*right_segments)
    return (path or None), bool(path)


def _binop_rooted_at_base(node: ast.BinOp, base_names: set[str]) -> bool:
    """True iff the innermost left operand of a ``/`` chain is a result base."""
    cur: ast.expr = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        cur = cur.left
    return _is_result_base(cur, base_names)


def _segments_from_fstring(node: ast.JoinedStr, base_names: set[str]) -> tuple[str | None, bool]:
    """``f"{base}/a/b.json"`` → (path, is_candidate).

    Finds the first ``{base}`` placeholder, then reads the literal tail after it.
    A further placeholder in the tail (a computed filename) is unverifiable.
    """
    base_idx = -1
    for i, value in enumerate(node.values):
        if isinstance(value, ast.FormattedValue) and _is_result_base(value.value, base_names):
            base_idx = i
            break
    if base_idx == -1:
        return None, False
    tail = node.values[base_idx + 1 :]
    segments: list[str] = []
    for value in tail:
        if isinstance(value, ast.FormattedValue):
            return ast.unparse(node), False  # computed tail → unverifiable
        literal = _const_str(value)
        if literal is not None:
            segments.append(literal)
    path = _norm_path(*segments)
    return (path or None), bool(path)


def _scan_result_writes(tree: ast.Module) -> tuple[list[str], list[str]]:
    """Scan for writes under ``$HPC_RESULT_DIR``.

    Returns ``(candidates, unverifiable)`` — sorted, deduped result-relative
    literal paths, and the computed expressions the scanner could not reduce.
    Every path built on the result dir is a candidate output (no write-function
    vocabulary — the boundary posture).
    """
    base_names = _collect_base_names(tree)
    candidates: set[str] = set()
    unverifiable: set[str] = set()
    # A ``/`` chain ``base / "a" / "b"`` nests as ``(base / "a") / "b"`` — only the
    # OUTERMOST BinOp is processed (the inner ``base / "a"`` is the left child of
    # another Div BinOp and would otherwise be counted as a second candidate).
    inner_div: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = node.left
            if isinstance(left, ast.BinOp) and isinstance(left.op, ast.Div):
                inner_div.add(id(left))
    for node in ast.walk(tree):
        path: str | None = None
        is_candidate = False
        if isinstance(node, ast.Call):
            path, is_candidate = _segments_from_join(node, base_names)
        elif isinstance(node, ast.BinOp):
            if id(node) in inner_div:
                continue
            path, is_candidate = _segments_from_binop(node, base_names)
        elif isinstance(node, ast.JoinedStr):
            path, is_candidate = _segments_from_fstring(node, base_names)
        if path is None:
            continue
        if is_candidate:
            candidates.add(path)
        else:
            unverifiable.add(path)
    return sorted(candidates), sorted(unverifiable)


# ── the projection ────────────────────────────────────────────────────────────


@primitive(
    name=_PRIMITIVE,
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Project the durable audit records into a DRAFT InterviewSpec (the "
            "audit->interview bridge). Reads the audit-open intent (goal + "
            "task_axes) and config roots off the notebook-audit-config seat, and "
            "AST-scans the source for @register_run entry points and "
            "$HPC_RESULT_DIR write candidates. Every field is DERIVED-and-disclosed "
            "or an explicit PLACEHOLDER the caller must fill - the verb never "
            "guesses (a guessed field becomes a journaled fact via the interview). "
            "summary_artifact and entry-point candidates are detected-and-disclosed "
            "(multiple = listed, caller confirms). Deterministic: same records -> "
            "byte-identical draft. No SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=AuditHandoffSpec,
        schema_ref=SchemaRef(input="audit_handoff"),
    ),
    agent_facing=True,
)
def audit_handoff(*, experiment_dir: Path, spec: AuditHandoffSpec) -> AuditHandoffResult:
    """Project the ``audit_id``'s durable records into a DRAFT ``InterviewSpec``.

    Reads the audit-open intent (``goal`` / ``task_axes``) and the recorded config
    roots, scans the source ``.py`` for ``@register_run`` entry points and
    ``$HPC_RESULT_DIR`` write candidates, and returns a draft whose non-derivable
    fields are explicit placeholders (never guessed). Deterministic — a pure
    function of the records + source bytes.

    Raises :class:`errors.SpecInvalid` on an unreadable source path or a source
    that is not parseable Python.
    """
    experiment_dir = Path(experiment_dir)
    source_text = _read_source(experiment_dir, spec.source, "source")
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise errors.SpecInvalid(
            f"{_PRIMITIVE}: source {spec.source!r} is not parseable Python: {exc}"
        ) from exc

    cfg = read_recorded_config(experiment_dir, spec.audit_id)
    goal, task_axes = read_audit_intent(experiment_dir, spec.audit_id)
    entry_candidates = _scan_register_run(tree)
    summary_candidates, unverifiable = _scan_result_writes(tree)

    audited_source: dict[str, object] = {
        "source": spec.source,
        "audit_id": spec.audit_id,
        "template": spec.template,
        "input_roots": list(cfg.input_roots),
        "source_roots": list(cfg.source_roots),
        "output_roots": list(cfg.output_roots),
    }
    if cfg.attention_order is not None:
        audited_source["attention_order"] = list(cfg.attention_order)

    disclosures: list[str] = []

    # entry_point: exactly one @register_run is fillable; zero/several is a
    # disclosed placeholder (the ambiguity is surfaced, never resolved by picking).
    entry_point: dict[str, object] | None = None
    if len(entry_candidates) == 1:
        entry_point = {"kind": "register_run", "run_name": entry_candidates[0]}
    elif not entry_candidates:
        disclosures.append(
            "no @register_run-decorated function found in the source — declare the "
            "entry point in the interview (register_run run_name, or a "
            "shell_command wrapper)."
        )
    else:
        disclosures.append(
            f"{len(entry_candidates)} @register_run functions found "
            f"({', '.join(entry_candidates)}) — the caller picks the entry point; "
            "audit-handoff never chooses across candidates."
        )

    # placeholders — ALWAYS the three the audit records never hold, plus goal /
    # entry_point when not derivable. Fixed order for determinism.
    placeholders: list[HandoffPlaceholder] = []
    if goal is None:
        placeholders.append(
            HandoffPlaceholder(
                field="goal",
                reason=(
                    "no goal was recorded at audit open (the notebook-audit-config "
                    "seat carried none) — the caller states the one-line goal; it "
                    "is never invented."
                ),
            )
        )
        disclosures.append("no goal recorded at audit open — emitted as a placeholder.")
    placeholders.append(
        HandoffPlaceholder(
            field="task_generator",
            reason=(
                "a materializer recipe is never derivable from the audit records — "
                "the caller supplies the shape + params"
                + (f" (recorded axes: {', '.join(task_axes)})" if task_axes else "")
                + "."
            ),
        )
    )
    placeholders.append(
        HandoffPlaceholder(
            field="task_count",
            reason="the fan-out count is a property of the task_generator the caller supplies.",
        )
    )
    placeholders.append(
        HandoffPlaceholder(
            field="produced_by",
            reason="the interview provenance (agent session / human operator) is set at commit.",
        )
    )
    if entry_point is None:
        placeholders.append(
            HandoffPlaceholder(
                field="entry_point",
                reason=(
                    "zero or several @register_run functions were found — the "
                    "caller declares the entry point (see disclosures / "
                    "entry_point_candidates)."
                ),
            )
        )

    if len(summary_candidates) > 1:
        disclosures.append(
            f"{len(summary_candidates)} $HPC_RESULT_DIR write candidates detected "
            f"({', '.join(summary_candidates)}) — the caller confirms which is the "
            "citable summary artifact; audit-handoff never picks."
        )
    elif not summary_candidates and not unverifiable:
        disclosures.append(
            "no $HPC_RESULT_DIR write detected by the scan — if the source writes "
            "its summary through an uncovered form (str.format / % / +), declare "
            "summary_artifact by hand."
        )
    if unverifiable:
        disclosures.append(
            f"{len(unverifiable)} computed $HPC_RESULT_DIR path expression(s) could "
            "not be reduced to a literal — see unverifiable_result_writes."
        )
    if not task_axes:
        disclosures.append(
            "no task axes recorded at audit open — the caller states what varies "
            "across tasks in the task_generator."
        )

    return AuditHandoffResult(
        audit_id=spec.audit_id,
        goal=goal,
        entry_point=entry_point,
        entry_point_candidates=entry_candidates,
        audited_source=audited_source,
        task_axes=task_axes,
        summary_artifact_candidates=summary_candidates,
        unverifiable_result_writes=unverifiable,
        placeholders=placeholders,
        disclosures=disclosures,
    )
