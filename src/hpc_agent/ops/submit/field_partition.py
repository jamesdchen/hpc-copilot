"""The single source of truth for the submit-input field partition.

Replaces the prose tables in ``hpc-submit/SKILL.md`` and
``hpc-wrap-entry-point/SKILL.md`` with one code object so the partition
can no longer drift between the two skills (or between a skill and the
resolution code). Every submit-input field is in exactly one of two
classes:

* :data:`REQUIRED_CALLER_FIELDS` — genuine judgment the LLM relays
  verbatim (``goal``) or a caller-supplied artifact the framework cannot
  invent (``task_generator``). These have NO safe default: absence is an
  escalation, never an auto-resolution. Inventing a ``task_generator``
  from "safe_defaults" was incident 1b — the partition makes that
  structurally impossible, because no code path can attach a default to a
  field in this set.
* :data:`AUTO_RESOLVABLE_FIELDS` — fields a deterministic rule (a cluster
  default, a runtime prior, a recommendation primitive, a glob) can fill.
  Only these may carry an :class:`Ambiguity` ``safe_default``.

The :class:`Ambiguity` guard (``__post_init__``) is the fireable lock:
constructing an ``Ambiguity`` with a ``safe_default`` on a
``REQUIRED_CALLER_FIELDS`` member raises ``ValueError`` immediately. This
is a real guard, not a provenance marker the LLM can stamp — the LLM
never gets to set ``safe_default`` on a required field because the object
refuses to exist (see the "Explicitly rejected" note in the plan: a
provenance/authenticity gate the assembler sets cannot fire).

Imported by :mod:`hpc_agent.ops.resolve_resources` (drift guard),
:mod:`hpc_agent.ops.scaffold_spec` (reconciling its ``unresolved``
skeleton), and the Surface-2 verbs (``walk-submit-ambiguities``,
``apply-safe-defaults``).
"""

from __future__ import annotations

import dataclasses
from typing import Any

__all__ = [
    "AUTO_RESOLVABLE_FIELDS",
    "REQUIRED_CALLER_FIELDS",
    "Ambiguity",
    "may_have_safe_default",
]


# Genuine-judgment / caller-supplied fields. NEVER carry a safe_default —
# absence is an escalation. ``goal`` is free-text intent the LLM relays;
# ``task_generator`` is the sweep recipe the framework cannot fabricate
# (incident 1b: an autonomous agent invented one, justified by
# "safe_defaults"). The Ambiguity guard refuses to attach a default here.
REQUIRED_CALLER_FIELDS: frozenset[str] = frozenset({"goal", "task_generator"})

# Fields a deterministic rule can fill — a cluster default, a runtime
# prior, the recommend-partition / recommend-pe primitives, or a
# convention glob. Only these may carry an Ambiguity safe_default.
AUTO_RESOLVABLE_FIELDS: frozenset[str] = frozenset(
    {
        "cluster",
        "walltime_sec",
        "gpu_type",
        "partition",
        "mpi_pe",
        "data_axis",
        "homogeneous_axes",
        "frozen_configs",
        "entry_point",
        "uncovered_param",
    }
)


def may_have_safe_default(field: str) -> bool:
    """Return whether *field* is allowed to carry a safe default.

    True only for :data:`AUTO_RESOLVABLE_FIELDS` members. A
    :data:`REQUIRED_CALLER_FIELDS` member (or any field outside both sets)
    returns False — it must be escalated to the caller, never auto-filled.
    The partition is exhaustive over the submit-input vocabulary; a name
    in neither set is not auto-resolvable by construction.
    """
    return field in AUTO_RESOLVABLE_FIELDS


@dataclasses.dataclass(frozen=True)
class Ambiguity:
    """One unresolved submit-input field, in the ``needs_resolution`` shape.

    Mirrors the dict the ``hpc-submit`` SKILL accumulated by hand
    (``{field, candidates, depends_on, safe_default, context}``) — now a
    typed object whose ``__post_init__`` enforces the partition invariant
    the prose only asserted.

    ``safe_default`` is the load-bearing slot: an autonomous caller fills
    ``field`` with it (see ``apply-safe-defaults``). It is permitted ONLY
    on an :data:`AUTO_RESOLVABLE_FIELDS` member. Setting it on a
    :data:`REQUIRED_CALLER_FIELDS` member raises ``ValueError`` here, at
    construction — the guard that makes a fabricated ``task_generator``
    impossible to express.

    ``uncovered_param``'s safe_default is dict-shaped (``{param: None}``
    is a *present but unknown* slot, not an absent one). The guard tests
    ``is not None``, so a ``{param: None}`` default is correctly treated
    as present and is allowed (``uncovered_param`` is auto-resolvable);
    it is the field's membership, not the value's truthiness, that gates.
    """

    field: str
    candidates: Any = None
    depends_on: tuple[str, ...] = ()
    safe_default: Any = None
    context: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # The fireable guard: a safe_default on a required-caller field is
        # a fabricated value (the incident-1b shape). Refuse at
        # construction so no code path — and no LLM — can express it. Use
        # ``is not None`` (NOT truthiness): uncovered_param's {param: None}
        # default is a present slot, and a falsy-but-present default (0,
        # "", []) on an auto-resolvable field is still a real default.
        if self.safe_default is not None and self.field in REQUIRED_CALLER_FIELDS:
            raise ValueError(
                f"Ambiguity for required-caller field {self.field!r} must not carry a "
                f"safe_default ({self.safe_default!r}); {self.field!r} is in "
                "REQUIRED_CALLER_FIELDS — it is genuine judgment / a caller-supplied "
                "artifact the framework cannot invent. Absence is an escalation, not "
                "an auto-resolution. (This is the incident-1b lock: a task_generator "
                "synthesized from 'safe_defaults' is exactly what this refuses.)"
            )

    def to_dict(self) -> dict[str, Any]:
        """Project to the ``needs_resolution`` ambiguity dict shape.

        ``depends_on`` becomes a list (the wire/JSON form); ``context`` is
        omitted when ``None`` so the shape matches the SKILL's hand-built
        entries (which carried ``context`` only on ``uncovered_param``).
        """
        out: dict[str, Any] = {
            "field": self.field,
            "candidates": self.candidates,
            "depends_on": list(self.depends_on),
            "safe_default": self.safe_default,
        }
        if self.context is not None:
            out["context"] = self.context
        return out
