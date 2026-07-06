"""The single source of truth for the submit-input field partition.

Replaces the prose tables in ``hpc-submit/SKILL.md`` and
``hpc-wrap-entry-point/SKILL.md`` with one code object so the partition
can no longer drift between the two skills (or between a skill and the
resolution code). Every submit-input field is in exactly one of three
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
* :data:`CODE_DERIVED_FIELDS` — outputs the framework RE-DERIVES from the
  input delta (``executor`` from the interview's materialized entry,
  ``job_env``/activation from the cluster, ``run_id``/``cmd_sha`` from the
  task list, …). NO agent-facing surface may accept a hand-authored value
  for one: ``revise-resolved`` refuses a patch naming one, and
  ``append-decision`` refuses a ``resolved`` dict committing one (run #6
  finding F1: a hand-authored sidecar ``executor`` of the bare token
  ``monte_carlo_pi`` shelled verbatim and exited 127).

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
    "CALLER_OVERRIDABLE_DERIVED_FIELDS",
    "CODE_DERIVED_FIELDS",
    "JOURNAL_UNAUTHORABLE_FIELDS",
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


# CODE-DERIVED fields — the third partition class (run #6 finding F1). Each is
# an OUTPUT the framework recomputes from the input delta; an LLM must never
# hand-author one at any agent-facing surface. Moved here from
# ``ops/revise_resolved.py``'s private ``_DERIVED_FIELDS`` so the single source
# of truth serves every guard (revise-resolved's patch refusal, append-
# decision's resolved refusal) — the run-#6 incident was exactly a surface
# (the sidecar / a hand-authored resolve spec) where ``executor`` was still
# authorable: the agent wrote the bare extension-less token ``monte_carlo_pi``
# and the dispatcher shelled it verbatim (exit 127, canary_failed).
CODE_DERIVED_FIELDS: frozenset[str] = frozenset(
    {
        "job_env",  # derived from (cluster, run identity) at build-submit-spec
        "run_id",  # derived by compute-run-id (<run_name>-<cmd_sha[:8]>)
        "cmd_sha",  # derived by compute-run-id (hash of the task list)
        "executor",  # the per-task command — from the interview's materialized entry
        "ssh_target",  # derived from the cluster's user@host (clusters.yaml)
        "backend",  # derived from the cluster's scheduler (clusters.yaml)
        "remote_path",  # derived from the cluster's scratch
        "total_tasks",  # derived from tasks.total() (compute-run-id)
        "sidecar",  # the whole config snapshot is re-written, never hand-set
        "submit_spec",  # the built submit-flow spec — a derived output
        "script",  # the cluster-side template path (backend + is_gpu)
        "repo_dir",  # derived from remote_path (deploy target)
        "job_name",  # defaults to profile
        "modules",  # activation — derived from (cluster, clusters.yaml)
        "conda_source",  # activation — derived from (cluster, clusters.yaml)
        "conda_env",  # activation — derived from (cluster, clusters.yaml)
    }
)

# The subset of CODE_DERIVED_FIELDS that ``append-decision`` refuses in a
# caller-supplied ``resolved`` dict. Scoped by AUDIT, not by the full set,
# because three derived names are LEGITIMATELY present in a committed
# ``resolved`` and refusing them would break green paths (engineering-
# principles: the guard must be able to fire without breaking a legit path):
#
# * ``run_id`` — a genuine INPUT field of the status/aggregate workflows
#   (``field_ownership.OWNERSHIP`` maps it to status-snapshot /
#   aggregate-check), so a greenlight's resolved naming it is sanctioned.
# * ``cmd_sha`` — the §4 identity fast-path token: ``block_drive`` reads
#   ``committed_resolved.get("cmd_sha")`` to decide advance-vs-rerun, so the
#   approved spec legitimately echoes it.
# * ``total_tasks`` — a count echo is harmless here because it has its own
#   ground-truth cross-check downstream (finding 21: resolve-submit-inputs
#   refuses any declared count that disagrees with ``tasks.total()``).
#
# Everything else in the set is a value the framework alone derives and no
# brief/spec flow ever asks the agent to restate — a ``resolved`` naming one
# is the hand-authoring bug class (run #6 F1's ``executor``), refused with a
# pointer at ``revise-resolved`` (patch the INPUT field instead).
# Derived by DEFAULT, but a caller MAY override — the activation contract:
# ``remote_activation_for_sidecar`` honors a sidecar-/spec-pinned activation over
# the cluster derivation (``test_explicit_env_activation_wins_over_clusters_yaml``),
# so ``append-decision`` must NOT refuse a caller supplying these in a
# ``resolved`` dict. 13-residual: activation sat in JOURNAL_UNAUTHORABLE, which
# CONTRADICTED that caller-wins contract — a legitimate override was refused as a
# hand-authored derived field. They stay in :data:`CODE_DERIVED_FIELDS` for the
# default derivation, but are exempt from the unauthorable refusal.
CALLER_OVERRIDABLE_DERIVED_FIELDS: frozenset[str] = frozenset(
    {"modules", "conda_source", "conda_env"}
)

JOURNAL_UNAUTHORABLE_FIELDS: frozenset[str] = (
    CODE_DERIVED_FIELDS
    - frozenset({"run_id", "cmd_sha", "total_tasks"})
    - CALLER_OVERRIDABLE_DERIVED_FIELDS
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
