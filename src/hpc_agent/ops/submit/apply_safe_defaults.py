"""``apply-safe-defaults`` primitive — autonomous fill of resolvable ambiguities.

Surface 2, incident 1b. Replaces the SKILL's "the autonomous caller
applies safe_defaults" prose with code. Consumes the ``{resolved,
ambiguities}`` envelope from ``walk-submit-ambiguities`` and fills each
ambiguity from its own ``safe_default``.

The defense-in-depth lock: even though ``walk-submit-ambiguities`` never
attaches a ``safe_default`` to a REQUIRED_CALLER_FIELDS member (the
:class:`~hpc_agent.ops.submit.field_partition.Ambiguity` guard refuses
one at construction), this verb re-checks
:func:`~hpc_agent.ops.submit.field_partition.may_have_safe_default` for
EVERY ambiguity it would fill. A ``task_generator`` ambiguity that somehow
carried a ``safe_default`` (a hand-tampered envelope) raises
``spec_invalid`` here rather than silently fabricating a sweep. The
partition makes the structure unfillable; this check makes a tampered
structure loud.
"""

from __future__ import annotations

from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.apply_safe_defaults import (
    ApplySafeDefaultsInput,
    ApplySafeDefaultsResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.submit.field_partition import may_have_safe_default

__all__ = ["apply_safe_defaults"]


def _apply_safe_defaults_result_post(result: ApplySafeDefaultsResult) -> dict[str, Any]:
    """Project the typed result into the envelope ``data`` dict."""
    return result.model_dump(mode="json")


@primitive(
    name="apply-safe-defaults",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Consume a {resolved, ambiguities} envelope and fill each ambiguity "
            "from its safe_default. Structurally cannot fill task_generator "
            "(no slot, by the field partition); re-checks may_have_safe_default "
            "and raises spec_invalid if a required-caller field carries a "
            "safe_default. Reports what stayed unresolved (chiefly goal / "
            "task_generator)."
        ),
        spec_arg=True,
        schema_ref=SchemaRef(input="apply_safe_defaults"),
        spec_model=ApplySafeDefaultsInput,
        result_post=_apply_safe_defaults_result_post,
        requires_ssh=False,
    ),
    agent_facing=True,
)
def apply_safe_defaults(
    *,
    spec: ApplySafeDefaultsInput,
) -> ApplySafeDefaultsResult:
    """Auto-fill every resolvable ambiguity from its ``safe_default``.

    Walks *spec*'s ambiguities. For each:

    * a field NOT in AUTO_RESOLVABLE_FIELDS (i.e. a REQUIRED_CALLER_FIELDS
      member like ``task_generator`` / ``goal``) is left unresolved — and
      if it nonetheless carries a non-``None`` ``safe_default`` (a tampered
      envelope), raise :class:`errors.SpecInvalid` (defense-in-depth);
    * an auto-resolvable field with a present (``is not None``)
      ``safe_default`` is filled into ``resolved``; the ``uncovered_param``
      ``{param: None}`` slot is correctly treated as present;
    * an auto-resolvable field whose ``safe_default`` is absent stays
      unresolved (nothing to default to).

    Returns the merged ``resolved`` plus ``applied`` / ``still_unresolved``.
    """
    resolved: dict[str, Any] = dict(spec.resolved)
    applied: dict[str, Any] = {}
    still_unresolved: list[str] = []

    for amb in spec.ambiguities:
        field = amb.get("field")
        if not isinstance(field, str):
            raise errors.SpecInvalid(f"ambiguity entry missing a string 'field': {amb!r}")
        # `is not None`, NOT truthiness — uncovered_param's {param: None} is
        # a present slot, and a falsy-but-present default (0, "", [], {}) on
        # an auto-resolvable field is still a real default to apply.
        has_default = "safe_default" in amb and amb["safe_default"] is not None

        if not may_have_safe_default(field):
            # REQUIRED_CALLER_FIELDS (or any non-auto-resolvable field). It must
            # NOT be auto-filled. A safe_default on it is the incident-1b shape
            # — refuse loudly (defense-in-depth behind the Ambiguity guard).
            if has_default:
                raise errors.SpecInvalid(
                    f"ambiguity for field {field!r} carries a safe_default "
                    f"({amb['safe_default']!r}) but {field!r} is not auto-resolvable "
                    "(it is a REQUIRED_CALLER_FIELDS member). apply-safe-defaults "
                    "refuses to auto-fill it — a fabricated value here is exactly "
                    "the incident-1b failure. The caller must supply it."
                )
            still_unresolved.append(field)
            continue

        if has_default:
            resolved[field] = amb["safe_default"]
            applied[field] = amb["safe_default"]
        else:
            # Auto-resolvable but nothing to default to (e.g. cluster with no
            # configured candidates) — stays for the caller.
            still_unresolved.append(field)

    return ApplySafeDefaultsResult(
        resolved=resolved,
        applied=applied,
        still_unresolved=still_unresolved,
        all_resolved=not still_unresolved,
    )
