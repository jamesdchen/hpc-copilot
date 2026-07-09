"""Shared helpers for the ``hpc-agent`` CLI surface — the adapter contract.

This module is the **public adapter contract** for both host-internal CLI
modules (``hpc_agent.cli.*``) and external plugins. Plugins import these
symbols (``_ok``, ``_err``, ``_load_spec``, ``_err_from_hpc``, …)
to build their own CLI subcommands — a plugin's ``register_cli`` hook is
where this pattern is used. The underscore prefix is historical;
**these are the extension SDK and rename will require a release**.

The helpers split into two boundaries that frame every cmd_*:

* **Input boundary** — ``_load_spec``, ``_validate_against_schema``,
  ``_add_experiment_dir`` / ``_add_run_id`` /
  ``_add_spec_and_dry_run``. argparse args + JSON spec files →
  validated Python kwargs.
* **Output boundary** — ``_emit``, ``_ok``, ``_err``, ``_err_from_hpc``,
  ``EXIT_OK`` / ``EXIT_USER_ERROR`` / ``EXIT_CLUSTER_ERROR`` /
  ``EXIT_INTERNAL``. Primitive return value → JSON envelope on stdout.

A cmd_* is "input boundary → primitive → output boundary." The 80% of
adapters that fit this exact shape are candidates for the future
registry-driven dispatcher (``_dispatch.py``); the other 20% have real
branching logic and stay hand-written.
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent.infra.ssh_agent import agent_available, agent_detail

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_CLUSTER_ERROR = 2
EXIT_INTERNAL = 3

# error_code → exit code mapping. Stable contract; documented in docs/reference/cli-spec.md.
_EXIT_CODE_BY_CATEGORY = {
    "user": EXIT_USER_ERROR,
    "cluster": EXIT_CLUSTER_ERROR,
    "network": EXIT_CLUSTER_ERROR,
    "internal": EXIT_INTERNAL,
}


# ─── envelope helpers ──────────────────────────────────────────────────────


def _emit(envelope: dict[str, Any]) -> None:
    """Print a single-line JSON envelope to stdout."""
    print(json.dumps(envelope, sort_keys=True), flush=True)


@functools.cache
def _meta_idempotent(name: str) -> bool:
    """Look up a primitive's idempotency declaration from the catalog.

    B4 rewire: callers used to hardcode ``_ok(idempotent=True/False, ...)``
    which forked the truth between the @primitive decorator (consumed by
    docs / lint) and the runtime envelope (consumed by caller policy).
    Routing through the catalog collapses both to the decorator.

    Cached because the catalog walks every primitive's frontmatter on
    first call. Falls back to True on miss (consistent with the
    pre-B4 default for query-style commands; the cross-validation test
    in tests/test_idempotency.py guards against silent drift).
    """
    try:
        from hpc_agent._kernel.registry.operations import operations_catalog

        for entry in operations_catalog():
            if entry.get("name") == name:
                return bool(entry.get("idempotent", True))
    except (LookupError, KeyError, FileNotFoundError):
        # Narrow catch so programmer errors (e.g. registry queried
        # before register_primitives()) surface in main() rather than
        # being silently coerced to ``idempotent=True``.
        pass
    return True


def _ok(
    data: dict[str, Any],
    *,
    idempotent: bool | None = None,
    name: str | None = None,
    partial_errors: list[dict[str, str]] | None = None,
    escalation: dict[str, Any] | None = None,
) -> None:
    """Emit an ok-true envelope.

    *idempotent* (B4 rewire): preferred spelling is to pass *name* — the
    primitive's catalog name — and let the envelope read the
    ``idempotent`` flag from ``operations_catalog()``. The legacy
    ``idempotent=True/False`` kwarg is still honoured for callsites that
    don't have a primitive mapping (e.g. cmd_aggregate which wraps a
    pure mapreduce reduce). When both are supplied, the explicit kwarg
    wins so callers can opt out of the catalog lookup if needed.

    *partial_errors*: optional list of ``{code, detail}`` dicts surfaced
    at the top level of the envelope — distinct from any per-primitive
    error list that lives inside the ``data`` block.
    Used by primitives like ``inspect-cluster`` whose underlying data
    source can be partially degraded (qhost timed out, sacct
    unavailable) without the operation as a whole failing.

    *escalation*: optional 'needs a decision' block (#231) for the
    succeeded-but-decide case (e.g. campaign-advance reaching a stop
    decision, or a stage-out quota gate). 'Needs a decision' is orthogonal
    to ``ok``, so it rides as data on a success envelope rather than as a
    third wire state.
    """
    if idempotent is None:
        idempotent = _meta_idempotent(name) if name else True
    if name:
        from hpc_agent._kernel.contract.schema import validate_output

        validate_output(data, name)
    env: dict[str, Any] = {"ok": True, "idempotent": idempotent, "data": data}
    if partial_errors:
        env["partial_errors"] = list(partial_errors)
    if escalation is not None:
        env["escalation"] = escalation
    _emit(env)


def _err(
    *,
    error_code: str,
    message: str,
    category: str,
    retry_safe: bool,
    remediation: str | None = None,
    failure_features: dict[str, Any] | None = None,
    escalation: dict[str, Any] | None = None,
    spec_skeleton: Any = None,
) -> int:
    payload = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "category": category,
        "retry_safe": retry_safe,
    }
    if remediation is not None:
        payload["remediation"] = remediation
    if failure_features is not None:
        payload["failure_features"] = failure_features
    if escalation is not None:
        payload["escalation"] = escalation
    if spec_skeleton is not None:
        # A minimal valid instance of the failing schema (refusals-carry-a-valid-
        # skeleton). Only ever set on a spec_invalid schema-validation refusal.
        payload["spec_skeleton"] = spec_skeleton
    _emit(payload)
    return _EXIT_CODE_BY_CATEGORY.get(category, EXIT_INTERNAL)


def _spec_invalid_failure_features(exc: Exception | None) -> dict[str, Any]:
    """Build the structured ``failure_features`` block for a spec_invalid envelope.

    Populates the two fields a caller can act on without re-parsing prose:

    * ``error_class`` — the framework-owned ``FailureCategory`` value
      ``"code_bug"``. A malformed / schema-failing ``--spec`` is a caller-input
      bug that IS deterministically classifiable, so it is never the
      ``"unknown"`` escape hatch (which means "the classifier could not
      categorize"). Reuses the existing vocabulary; introduces no new taxonomy.
    * ``error_class_raw`` — the producer's raw signature, preserved verbatim:
      the exception type, plus (for a pydantic ``ValidationError``) each
      offending field path and its pydantic error ``type`` from
      ``exc.errors()`` — the missing / extra / invalid fields the caller must
      fix. Open and ungoverned per the ``failure_features`` contract.

    The ``exc.errors()`` extraction is duck-typed (``getattr`` + a callable
    check) so it works for a pydantic ``ValidationError`` without importing
    pydantic here, and degrades to the bare type name for any other exception.
    """
    if exc is None:
        return {"error_class": "code_bug", "error_class_raw": "spec_invalid"}
    raw = type(exc).__name__
    errors_fn = getattr(exc, "errors", None)
    if callable(errors_fn):
        try:
            details = list(errors_fn())
        except Exception:  # noqa: BLE001 — not a pydantic ValidationError; keep the bare name
            details = []
        parts: list[str] = []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            loc = ".".join(str(p) for p in detail.get("loc", ())) or "<root>"
            parts.append(f"{loc} ({detail.get('type', 'invalid')})")
        if parts:
            raw = f"{raw}: " + "; ".join(parts)
    return {"error_class": "code_bug", "error_class_raw": raw}


def _err_from_hpc(exc: errors.HpcError) -> int:
    remediation = exc.remediation
    # No hard pre-flight agent gate any more: ``ssh_run`` uses
    # ``BatchMode=yes`` so a missing/usable-auth failure fails fast on its
    # own (no hang), and a precheck would reject valid IdentityFile-based
    # auth that needs no agent at all (submit-flow has always relied on
    # this). Instead, when an SSH op DOES fail and no agent is reachable,
    # append the agent state — the user keeps the actionable hint the old
    # precheck gave without the false negative. ``agent_detail()`` also
    # describes the Windows named-pipe agent (which never sets
    # SSH_AUTH_SOCK).
    if isinstance(exc, errors.SshUnreachable) and not agent_available():
        hint = (
            f"No SSH agent reachable ({agent_detail()}). If you authenticate "
            "via an IdentityFile in ~/.ssh/config this is fine; otherwise load "
            "a key — Unix/macOS: `ssh-add ~/.ssh/<key>` (and forward "
            "SSH_AUTH_SOCK into spawned envs); Windows: `Start-Service "
            "ssh-agent; ssh-add ~/.ssh/<key>`."
        )
        remediation = f"{remediation} {hint}" if remediation else hint
    # Structured evidence for the spec_invalid class (WS3/WS4): the pydantic
    # dispatch seam attaches a rich ``failure_features`` (naming the offending
    # field paths) onto the exception; every other SpecInvalid raise site
    # (required-spec, not-a-dict, jsonschema) carries none, so synthesize the
    # default here so EVERY spec_invalid envelope names its failure class.
    failure_features = getattr(exc, "failure_features", None)
    if failure_features is None and exc.error_code == "spec_invalid":
        failure_features = _spec_invalid_failure_features(exc)
    # A schema-validation SpecInvalid attaches a code-generated minimal valid
    # instance (``build_spec_skeleton``); ride it into the refusal envelope so
    # the caller fills it in instead of reconstructing the shape from the schema.
    return _err(
        error_code=exc.error_code,
        message=str(exc),
        category=exc.category,
        retry_safe=exc.retry_safe,
        remediation=remediation,
        failure_features=failure_features,
        spec_skeleton=getattr(exc, "spec_skeleton", None),
    )


# ─── shared option helpers ─────────────────────────────────────────────────


def _add_experiment_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Path to the experiment repo (default: current working directory).",
    )


def _add_run_id(parser: argparse.ArgumentParser) -> None:
    """Add the canonical ``--run-id`` argument (always required)."""
    parser.add_argument("--run-id", required=True)


def _add_spec_and_dry_run(
    parser: argparse.ArgumentParser,
    *,
    schema_hint: str,
    dry_run_help: str,
) -> None:
    """Add the ``--spec`` (required) + ``--dry-run`` pair used by the
    workflow-flow subcommands (``submit-flow``, ``monitor-flow``,
    ``aggregate-flow``).

    *schema_hint* is the schema filename mentioned in the spec help
    (e.g. ``"schemas/submit_flow.input.json"``); *dry_run_help* lets
    each subcommand explain what dry-run skips.
    """
    parser.add_argument(
        "--spec",
        type=Path,
        required=True,
        help=f"JSON spec file ({schema_hint})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=dry_run_help,
    )


def _load_spec(spec_path: Path | None, *, schema_name: str | None = None) -> dict[str, Any]:
    """Load and (optionally) JSON-Schema-validate ``--spec`` input.

    Validation is opt-in via *schema_name* so callers without a matching
    schema (e.g. ad-hoc dicts) still work, but every CLI subcommand that
    has one in ``hpc_agent/schemas/<name>.input.json`` should pass
    it.  Validation failures map to ``SpecInvalid`` with the schema
    field path in the message — far more useful to a calling agent than
    the Python ``int("abc")`` traceback we used to surface.
    """
    if spec_path is None:
        return {}
    raw_arg = str(spec_path)
    if raw_arg.lstrip()[:1] in ("{", "["):
        # Inline JSON passed where a file path belongs. Classify it BEFORE
        # touching the filesystem: on Windows, ``read_text`` on a string
        # containing ``"`` / ``:`` raises OSError(22) (invalid argument), not
        # FileNotFoundError — and an unclassified OSError escapes the adapter
        # as an ``internal`` envelope (proving-run-3 papercut: ``wait-detached
        # --spec '{"run_id": ...}'``). NOTE: argparse's ``type=Path`` has
        # already normalized separators, so we cannot reliably recover the
        # original JSON here — diagnose, don't parse.
        preview = raw_arg[:100] + ("…" if len(raw_arg) > 100 else "")
        raise errors.SpecInvalid(
            f"--spec takes a FILE PATH, not inline JSON (got: {preview}). "
            "Write the JSON to a file and pass its path, e.g. "
            "--spec spec.json"
        )
    try:
        loaded = json.loads(spec_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(f"--spec file not found: {spec_path}") from exc
    except OSError as exc:
        # Not-a-readable-file for any other reason (invalid characters in the
        # path on Windows, a directory, permissions). Same user-error class as
        # file-not-found — never an ``internal`` envelope.
        raise errors.SpecInvalid(
            f"--spec path is not a readable file: {spec_path} ({exc})"
        ) from exc
    except json.JSONDecodeError as exc:
        raise errors.SpecInvalid(f"--spec is not valid JSON ({spec_path}): {exc}") from exc
    if not isinstance(loaded, dict):
        raise errors.SpecInvalid(f"--spec must be a JSON object; got {type(loaded).__name__}")
    if schema_name is not None:
        _validate_against_schema(loaded, schema_name)
    return loaded


# ─── spec skeleton (refusals carry a valid skeleton) ───────────────────────
#
# notebook-audit.md queue item 14 / Addendum 9 (run-#11): a spec_invalid refusal
# that only names the failing JSON path made the agent burn describe|grep
# round-trips reconstructing the shape. So a schema-validation refusal now
# carries ``spec_skeleton`` — a code-generated MINIMAL VALID instance of the
# schema (defaults where declared, typed placeholders for required-without-
# default, optional fields omitted). Pure, deterministic, no LLM.

_SKELETON_MAX_DEPTH = 6
#: Serialized-size cap for the embedded skeleton (context-budget principle):
#: a pathological/huge schema is truncated to its top-level keys with a note
#: rather than flooding the refusal envelope.
_SKELETON_BYTES_CAP = 4096


def _resolve_local_ref(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Resolve an in-document ``{"$ref": "#/$defs/..."}`` against *root*.

    Only same-document JSON-pointer refs (``#/...``) are followed — the
    self-contained Pydantic-emitted schemas this serves use exactly those.
    An external/unresolvable ref is left as-is (the caller then falls through
    to an untyped placeholder).
    """
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    target: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or part not in target:
            return node
        target = target[part]
    return target if isinstance(target, dict) else node


def _merge_all_of(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge an ``allOf`` node's branches (properties + required)."""
    merged = {k: v for k, v in node.items() if k != "allOf"}
    props: dict[str, Any] = dict(merged.get("properties", {}))
    required: list[Any] = list(merged.get("required", []))
    for sub in node.get("allOf", []):
        if not isinstance(sub, dict):
            continue
        sub = _resolve_local_ref(sub, root)
        if "allOf" in sub:
            sub = _merge_all_of(sub, root)
        props.update(sub.get("properties", {}))
        required.extend(sub.get("required", []))
        for key, value in sub.items():
            if key not in ("properties", "required", "allOf"):
                merged.setdefault(key, value)
    if props:
        merged["properties"] = props
    if required:
        merged["required"] = list(dict.fromkeys(required))
    return merged


def _string_placeholder(node: dict[str, Any]) -> str:
    hint = node.get("description") or node.get("title") or "value"
    snippet = " ".join(str(hint).split())[:48]
    return f"<string: {snippet}>"


def _number_placeholder(node: dict[str, Any], *, integer: bool) -> Any:
    """A minimum-aware numeric placeholder so the skeleton validates directly.

    Numbers are not placeholder strings, so pick a value inside the declared
    bound (``minimum`` / ``exclusiveMinimum``) rather than a blind ``0`` that a
    ``minimum: 1`` field would reject.
    """
    if "minimum" in node and isinstance(node["minimum"], (int, float)):
        val: float = node["minimum"]
    elif "exclusiveMinimum" in node and isinstance(node["exclusiveMinimum"], (int, float)):
        val = node["exclusiveMinimum"] + 1
    else:
        val = 0
    return int(val) if integer else val


def _skeleton(node: Any, root: dict[str, Any], depth: int) -> Any:
    """Recursive core of :func:`build_spec_skeleton`."""
    if not isinstance(node, dict):
        return None
    if depth > _SKELETON_MAX_DEPTH:
        # Bounded recursion: a self-referential/pathological schema stops here.
        return None
    node = _resolve_local_ref(node, root)
    if "allOf" in node:
        node = _merge_all_of(node, root)
    # Union: take the first branch, preferring a non-null one (oneOf per the
    # spec; anyOf is how Pydantic renders nullable/union fields).
    for union_key in ("oneOf", "anyOf"):
        branches = node.get(union_key)
        if isinstance(branches, list) and branches:
            dict_branches = [b for b in branches if isinstance(b, dict)]
            if not dict_branches:
                return None
            non_null = [b for b in dict_branches if b.get("type") != "null"]
            chosen = dict(non_null[0] if non_null else dict_branches[0])
            for carry in ("default", "description", "title"):
                if carry in node and carry not in chosen:
                    chosen[carry] = node[carry]
            return _skeleton(chosen, root, depth)
    if "default" in node:
        return copy.deepcopy(node["default"])
    if "const" in node:
        return copy.deepcopy(node["const"])
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        return copy.deepcopy(enum[0])
    typ = node.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), typ[0] if typ else None)
    if typ == "object" or (typ is None and "properties" in node):
        return _object_skeleton(node, root, depth)
    if typ == "array":
        # Required arrays: an empty list (context-budget — we do not fabricate
        # example members). A caller-required non-empty array is named in the
        # message's JSON path.
        return []
    if typ == "string":
        return _string_placeholder(node)
    if typ == "integer":
        return _number_placeholder(node, integer=True)
    if typ == "number":
        return _number_placeholder(node, integer=False)
    if typ == "boolean":
        return False
    if typ == "null":
        return None
    return None


def _object_skeleton(node: dict[str, Any], root: dict[str, Any], depth: int) -> dict[str, Any]:
    """Build the required-only instance of an object schema."""
    props = node.get("properties")
    required = node.get("required", [])
    out: dict[str, Any] = {}
    if not isinstance(props, dict):
        return out
    for name in required:
        if name in props:
            out[name] = _skeleton(props[name], root, depth + 1)
        else:
            out[name] = None
    return out


def build_spec_skeleton(schema: dict[str, Any]) -> Any:
    """Code-generate a MINIMAL VALID instance of a JSON schema.

    Deterministic, pure, no LLM. Defaults are filled where the schema declares
    them; required fields without a default get a typed placeholder
    (``"<string: ...>"``, a bound-aware number, ``false``, ``[]`` / a nested
    required-only ``{}``); optional fields are omitted. In-document ``$ref``
    (``#/$defs/...``) and ``allOf`` are merged; ``oneOf`` / ``anyOf`` take the
    first (non-null-preferring) branch; recursion is depth-capped so a
    pathological schema cannot blow up.

    Returns ``None`` when *schema* is not a dict.
    """
    if not isinstance(schema, dict):
        return None
    return _skeleton(schema, schema, 0)


def _bounded_skeleton(skeleton: Any) -> Any:
    """Cap the skeleton's serialized size (context-budget principle).

    Under the cap: returned unchanged. Over it: truncated to its top-level
    keys, each value replaced by a pointer note, with a ``_truncated`` marker —
    the caller still learns the top-level shape without flooding the envelope.
    """
    text = json.dumps(skeleton, sort_keys=True)
    if len(text.encode("utf-8")) <= _SKELETON_BYTES_CAP:
        return skeleton
    if isinstance(skeleton, dict):
        truncated: dict[str, Any] = {
            "_truncated": (
                f"spec_skeleton exceeded {_SKELETON_BYTES_CAP}B; showing top-level "
                "required keys only — read the schema for the nested shape"
            )
        }
        for key in skeleton:
            truncated[key] = "<see schema>"
        return truncated
    return {"_truncated": f"spec_skeleton exceeded {_SKELETON_BYTES_CAP}B"}


def _validate_against_schema(payload: Any, schema_name: str) -> None:
    """Validate *payload* against ``hpc_agent/schemas/<schema_name>.input.json``.

    Raises :class:`errors.SpecInvalid` on schema mismatch.  When the
    ``jsonschema`` library is unavailable (older installs that haven't
    picked up the runtime dep), this falls back to a no-op so the CLI
    keeps working — schema validation is defence in depth, not the only
    line of defence (``submit_and_record`` etc. still validate inputs).

    Cross-file ``$ref`` (rare post-Pydantic-migration — most
    schemas are now self-contained with constraints inlined from
    :mod:`hpc_agent._wire._shared`) resolves through the
    shared registry in :mod:`hpc_agent._kernel.contract.schema`.
    """
    try:
        import jsonschema  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        # Warn once so missing-dep installs (minimal venv, broken pip
        # state) don't silently bypass the defence-in-depth layer. The
        # Pydantic-driven inner validation still runs.
        import warnings as _warnings

        _warnings.warn(
            "jsonschema not installed; skipping wire-schema validation. "
            "Install with `pip install hpc-agent[<extras>]` or `pip install jsonschema>=4.18`.",
            RuntimeWarning,
            stacklevel=2,
        )
        return
    # Search the core schema package first, then every plugin-contributed
    # schema root. A plugin-owned primitive (e.g. a forecasting plugin's
    # ``predict_queue_wait``) keeps its schema in the plugin's own
    # ``schemas/`` tree; without consulting those roots the bare
    # ``hpc_agent.schemas`` lookup would silently no-op and this
    # defence-in-depth layer would never fire for any plugin primitive.
    # ``plugin_schema_roots`` resolves each loaded plugin's root by
    # convention (or its explicit ``schema_assets``), so the host stays
    # agnostic to which plugins are installed.
    from hpc_agent._kernel.registry.plugins import plugin_schema_roots

    schema_text: str | None = None
    roots = (_resource_files("hpc_agent.schemas"), *plugin_schema_roots())
    for root in roots:
        try:
            schema_text = (root / f"{schema_name}.input.json").read_text(encoding="utf-8")
            break
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            continue
    if schema_text is None:
        return
    schema = json.loads(schema_text)
    from hpc_agent._kernel.contract.schema import validate as _validate

    try:
        _validate(payload, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        # Schema names are underscored (interview, submit_flow); CLI verbs are
        # hyphenated (interview, submit-flow). The mapping is mechanical.
        verb = schema_name.replace("_", "-")
        spec_invalid = errors.SpecInvalid(
            f"--spec failed schema {schema_name}.input.json at {path}: {exc.message}",
            remediation=(
                f"Inspect the schema: `hpc-agent describe {verb}` (returns the "
                f"input_schema name) or read hpc_agent/schemas/"
                f"{schema_name}.input.json directly. Failing JSON path: {path}. "
                "The refusal carries a spec_skeleton — a minimal valid instance "
                "(placeholders for required fields, defaults where declared) to "
                "fill in rather than reconstruct the shape."
            ),
        )
        # Refusals carry a valid skeleton (notebook-audit.md item 14 / run-#11):
        # a code-generated minimal valid instance the caller fills in, capped so
        # a huge schema can't flood the envelope.
        try:
            skeleton = build_spec_skeleton(schema)
        except Exception:  # noqa: BLE001 — the skeleton is a best-effort hint; never mask the real refusal
            skeleton = None
        if skeleton is not None:
            spec_invalid.spec_skeleton = _bounded_skeleton(skeleton)  # type: ignore[attr-defined]
        raise spec_invalid from exc
