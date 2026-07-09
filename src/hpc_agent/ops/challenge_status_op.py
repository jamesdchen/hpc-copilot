"""``challenge-status`` — the one read-only query over standing dissent.

Design origin: ``docs/design/challenge-attestation.md`` C-verb (Wave B / T3). A
challenge is a human-authored, evidence-bound, sha-targeted attestation of
DISSENT against a committed record (C1: "a nudge is a challenge against a
proposal; a challenge is a nudge against the archive"). This verb READS that
standing state — it never files, resolves, or withdraws (those land only via
``append-decision`` under the gated ``"challenge"``-family blocks, C-gate lock 1;
this verb is ``verb="query"``, side-effect-free).

Two views, one collector:

* **the thread view** — keyed by a ``challenge_id``: the filing/verdict/withdraw
  conversation under that id, reduced to its ``open|upheld|dismissed|withdrawn|
  superseded`` status.
* **the target view** — keyed by a target ADDRESS (a ``content_sha``, or a
  ``{subject_kind, subject_id}`` pair): "what stands against this record?" — every
  standing challenge whose target names that address.

The read POSTURE is the evidence-memory E-read rule, applied to dissent
(``docs/design/evidence-memory.md``): the target is **re-resolved and DISCLOSED**
(``found-current | found-superseded | unresolvable``) — **never refused** (only
the append gate refuses; evidence and targets legitimately move). Each cited
evidence sha is likewise re-resolved and disclosed per line
(``verified`` / not). ``contested`` counts ride beside — a ``current`` target
reads ``current`` AND contested (C-status: an orthogonal dimension, never a
fifth status, never blocking).

The brief is CODE-rendered from the projection's own fields — dated, sha-cited,
with **no urgency / recommendation / interpretation vocabulary** (the
attention-queue D1 no-urgency rule; the token pin in the tests). Its
canonical-JSON sha is the ``view_sha`` a subsequent ``challenge-verdict`` may
carry: the render is a PURE FUNCTION of the result data (no wall-clock, no fleet
accounting), so the verdict gate RECOMPUTES a carried ``view_sha`` and it
matches byte-for-byte (the v1.6 recomputable-render precedent).

Seams (this worktree is isolated; T1/T2 land in parallel — the imports are
GUARDED so ``register_primitives`` never crashes on their absence, the loud-
import-failure contract of ``_kernel/registry/primitive.py``):

* **``_wire/queries/challenge_status.py`` (T2)** — ``ChallengeStatusSpec`` /
  ``ChallengeStatusResult`` and the inline item models. Imported at module level
  (needed at decoration). When absent, faithful placeholders mirroring the pinned
  C-verb contract keep the module importable and the tests meaningful; the real
  T2 module SHADOWS them at merge (one definition restored — the ``except`` branch
  becomes dead).
* **``state/challenges.py`` (T1)** — ``standing_challenges`` (the ONE collector
  every disclosure seat routes through — C-reduce / the C-disclose enforcement
  row) and ``contested_projection`` (the C-status counts). ``state`` never imports
  ``ops``, so the ``dossier`` resolver is composed HERE and injected (the
  evidence-brief idiom). Referenced by module-level name so a test monkeypatches
  ``challenge_status_op.standing_challenges`` / ``.contested_projection``.

This file lives at the ``ops/`` role root (sibling to ``evidence_brief_op.py`` /
``export_dossier.py``); the subject-imports lint short-circuits role-root files,
so the cross-subject reads + the ``export_dossier`` composition are allowed by
construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops import export_dossier
from hpc_agent.ops.attention_queue import discover_fleet_experiments
from hpc_agent.state.determinism import canonical_sha

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = ["challenge_status"]

# --- the T2 wire seam (guarded) ----------------------------------------------
# The wire models must exist at DECORATION time (spec_model / the return
# annotation). Imported top-level; when T2 is absent this worktree falls back to
# placeholders that mirror the pinned C-verb contract byte-for-byte, so the verb
# decorates, the module imports (the loud-import contract is honoured), and the
# tests exercise the real shapes. The real ``_wire`` module shadows these at
# merge (the ``except`` branch is then dead code).
try:  # pragma: no cover — the seam flips to "present" once T2 lands
    from hpc_agent._wire.queries.challenge_status import (
        ChallengeCitationLine,
        ChallengeContestedBlock,
        ChallengeEntryLine,
        ChallengeSkippedNamespace,
        ChallengeStatusResult,
        ChallengeStatusSpec,
    )
except ImportError:  # pragma: no cover — isolated-worktree placeholders (T2 pending)
    from typing import Literal

    from pydantic import BaseModel, ConfigDict, Field, model_validator

    from hpc_agent._wire._shared import RunIdStrict

    _TargetResolution = Literal["found-current", "found-superseded", "unresolvable"]
    _ChallengeStatus = Literal["open", "upheld", "dismissed", "withdrawn", "superseded"]

    class ChallengeCitationLine(BaseModel):  # type: ignore[no-redef]
        model_config = ConfigDict(extra="forbid", title="challenge citation line")

        kind: str = Field(description="The citation's CITATION_KINDS mechanism kind.")
        ref: str = Field(description="The opaque citation ref; echoed, never parsed.")
        sha: str = Field(description="The full sha the challenge recorded for this citation.")
        verified: bool = Field(
            description="True = re-resolved and the sha still matches here; False = unresolvable."
        )

    class ChallengeEntryLine(BaseModel):  # type: ignore[no-redef]
        model_config = ConfigDict(extra="forbid", title="challenge entry line")

        challenge_id: str = Field(
            description="The challenge's caller-authored slug (path segment)."
        )
        status: _ChallengeStatus = Field(
            description="open | upheld | dismissed | withdrawn | superseded — the reduced status."
        )
        filed_ts: str = Field(description="The filing record's ts — every line dated.")
        grounds: str = Field(
            description="The human's free-text dissent — opaque, verbatim, never parsed."
        )
        target_kind: str = Field(
            description="The target's resolver-dispatch kind (CITATION_KINDS)."
        )
        target_subject_kind: str = Field(description="The challenged record's subject_kind.")
        target_subject_id: str = Field(description="The challenged record's subject_id — opaque.")
        target_content_sha: str = Field(description="The exact sha being challenged.")
        target_resolution: _TargetResolution = Field(
            description="found-current | found-superseded | unresolvable — disclosed, not refused."
        )
        verdict: str | None = Field(
            default=None,
            description="upheld | dismissed when a verdict record resolved it; else null.",
        )
        reasoning: str | None = Field(
            default=None,
            description="The verdict's mandatory free-text reasoning; opaque, verbatim.",
        )
        citations: list[ChallengeCitationLine] = Field(
            default_factory=list,
            description="Each cited evidence sha, re-resolved at read (verified / unresolvable).",
        )

    class ChallengeContestedBlock(BaseModel):  # type: ignore[no-redef]
        model_config = ConfigDict(extra="forbid", title="challenge contested block")

        open: int = Field(default=0, description="Count of open (unresolved) challenges.")
        upheld: int = Field(default=0, description="Count of upheld challenges.")
        dismissed: int = Field(default=0, description="Count of dismissed challenges.")
        withdrawn: int = Field(default=0, description="Count of withdrawn challenges.")
        superseded: int = Field(default=0, description="Count of computed-superseded challenges.")
        challenge_ids: list[str] = Field(
            default_factory=list,
            description="The challenge ids in this block — identities, never a score.",
        )

    class ChallengeSkippedNamespace(BaseModel):  # type: ignore[no-redef]
        model_config = ConfigDict(extra="forbid", title="challenge skipped namespace")

        ref: str = Field(description="The namespace id that was skipped.")
        reason: str = Field(description="Why it was skipped (unreadable, torn, absent).")

    class ChallengeStatusSpec(BaseModel):  # type: ignore[no-redef]
        """Input spec — EXACTLY ONE addressing: challenge_id | content_sha | subject pair."""

        model_config = ConfigDict(extra="forbid", title="challenge-status input spec")

        challenge_id: RunIdStrict | None = Field(
            default=None, description="The thread view — one challenge conversation by its slug."
        )
        content_sha: str | None = Field(
            default=None,
            description="The target view — every challenge whose target names this sha.",
        )
        subject_kind: str | None = Field(
            default=None,
            description="The target view by subject — the challenged record's subject_kind.",
        )
        subject_id: str | None = Field(
            default=None,
            description="The target view by subject — the challenged record's subject_id.",
        )
        fleet: bool = Field(
            default=False,
            description="When True, the per-namespace walk over every journaled experiment.",
        )

        @model_validator(mode="after")
        def _exactly_one_address(self) -> ChallengeStatusSpec:
            by_id = self.challenge_id is not None
            by_sha = self.content_sha is not None
            by_subject = self.subject_kind is not None or self.subject_id is not None
            if by_subject and not (self.subject_kind is not None and self.subject_id is not None):
                raise ValueError(
                    "challenge-status subject addressing needs BOTH subject_kind and subject_id "
                    "(a subject is a full R3 address, never a bare half)."
                )
            modes = [by_id, by_sha, self.subject_kind is not None and self.subject_id is not None]
            if sum(modes) != 1:
                raise ValueError(
                    "challenge-status needs EXACTLY ONE addressing: a challenge_id (thread view), "
                    "a content_sha, or a subject pair (target view). You cannot contest an "
                    "under-specified address — the R3 full-address rule, read side."
                )
            return self

    class ChallengeStatusResult(BaseModel):  # type: ignore[no-redef]
        """Output data — the reduced statuses, target/citation disclosure, brief + view_sha."""

        model_config = ConfigDict(extra="forbid", title="challenge-status output data")

        view: Literal["thread", "target"] = Field(
            description="thread (by challenge_id) | target (by address) — which view was asked."
        )
        addressed_challenge_id: str | None = Field(
            default=None, description="The challenge_id addressed (thread view); echoed."
        )
        addressed_content_sha: str | None = Field(
            default=None, description="The content_sha addressed (target view); echoed."
        )
        addressed_subject_kind: str | None = Field(
            default=None, description="The subject_kind addressed (target-by-subject view); echoed."
        )
        addressed_subject_id: str | None = Field(
            default=None, description="The subject_id addressed (target-by-subject view); echoed."
        )
        target_resolution: _TargetResolution | None = Field(
            default=None,
            description="The addressed target's re-resolution (null when no challenge names it).",
        )
        entries: list[ChallengeEntryLine] = Field(
            default_factory=list, description="The reduced per-challenge statuses in scope."
        )
        contested: ChallengeContestedBlock = Field(
            default_factory=ChallengeContestedBlock,
            description="The C-status counts + ids over the entries (orthogonal to any status).",
        )
        skipped: list[ChallengeSkippedNamespace] = Field(
            default_factory=list,
            description="Namespaces skipped during fleet collection (fail-open).",
        )
        render: str = Field(
            description="The deterministic markdown brief — relayed to the human verbatim."
        )
        view_sha: str = Field(
            description="Canonical-JSON sha of the projection; byte-stable, recomputed by the gate."
        )


# --- the T1 collector seam (guarded; module-level for monkeypatch) -----------
# ``state/challenges.py::standing_challenges`` is the ONE collector every
# disclosure seat routes through (C-reduce; the C-disclose route-through
# enforcement row) and ``contested_projection`` computes the C-status counts.
# Guarded so the module imports before T1 lands; a test monkeypatches these
# module attributes (the evidence-brief ``_render_brief`` seam idiom).
try:  # pragma: no cover — the seam flips to "present" once T1 lands
    from hpc_agent.state.challenges import (
        contested_projection as _contested_projection_impl,
    )
    from hpc_agent.state.challenges import (
        standing_challenges as _standing_challenges_impl,
    )
except ImportError:  # pragma: no cover
    _standing_challenges_impl = None  # type: ignore[assignment]
    _contested_projection_impl = None  # type: ignore[assignment]

#: Module-level bindings the primitive calls and tests monkeypatch. Kept as
#: distinct names (not the ``_impl`` aliases) so a monkeypatch is unambiguous.
standing_challenges = _standing_challenges_impl
contested_projection = _contested_projection_impl


# --- the injected dossier resolver (state never imports ops) -----------------


def _dossier_resolver_for(experiment_dir: Path) -> Callable[[str], str | None]:
    """A ``ref -> bundle_sha256 | None`` resolver bound to *experiment_dir*.

    Composed HERE and injected into ``standing_challenges`` — ``state`` never
    imports ``ops`` (the evidence-brief drift-log item 2 seam). At READ an
    unresolvable dossier returns ``None`` → the collector DISCLOSES it, never
    raises (only the append gate refuses).
    """

    def _resolve(ref: str) -> str | None:
        try:
            sig = export_dossier.compute_dossier_signature(experiment_dir, ref)
        except Exception:  # noqa: BLE001 — read-side: any failure is "unresolvable here"
            return None
        return sig.bundle_sha256

    return _resolve


# --- collection → projection (mechanism-nouned, deterministic) ---------------


def _entry_line(entry: Any) -> ChallengeEntryLine:
    """Project one ``standing_challenges`` entry → the wire entry line.

    Reads the pinned T1 entry contract by attribute: ``challenge_id``, ``status``,
    ``filed_ts``, ``grounds``, ``target`` (``.kind`` / ``.subject_kind`` /
    ``.subject_id`` / ``.content_sha``), ``target_resolution``, ``verdict`` /
    ``reasoning`` (both optional), and ``citations`` (each ``.kind`` / ``.ref`` /
    ``.sha`` / ``.verified``). Identity + counting only — nothing here reads
    ``grounds`` or ``reasoning`` for meaning.
    """
    tgt = entry.target
    return ChallengeEntryLine(
        challenge_id=entry.challenge_id,
        status=entry.status,
        filed_ts=entry.filed_ts,
        grounds=entry.grounds,
        target_kind=tgt.kind,
        target_subject_kind=tgt.subject_kind,
        target_subject_id=tgt.subject_id,
        target_content_sha=tgt.content_sha,
        target_resolution=entry.target_resolution,
        verdict=getattr(entry, "verdict", None),
        reasoning=getattr(entry, "reasoning", None),
        citations=[
            ChallengeCitationLine(kind=c.kind, ref=c.ref, sha=c.sha, verified=bool(c.verified))
            for c in entry.citations
        ],
    )


def _contested_block(entries: Sequence[Any]) -> ChallengeContestedBlock:
    """The C-status counts + ids, routed through the T1 ``contested_projection``.

    Delegates to the ONE projection definition (``state/challenges.py``) so the
    seat never forks the reduction (the C-disclose route-through pin). A target
    with all-zero counts still emits the block here — the wire default is the
    empty block; the RENDER omits an all-zero block (the ``reproduces``
    emitted-only-when-present precedent).
    """
    proj = contested_projection(list(entries))
    return ChallengeContestedBlock(
        open=int(proj.get("open", 0)),
        upheld=int(proj.get("upheld", 0)),
        dismissed=int(proj.get("dismissed", 0)),
        withdrawn=int(proj.get("withdrawn", 0)),
        superseded=int(proj.get("superseded", 0)),
        challenge_ids=list(proj.get("challenge_ids", [])),
    )


def _projection(result_core: dict[str, Any]) -> dict[str, Any]:
    """The deterministic, wall-clock-FREE projection the ``view_sha`` shas over.

    Excludes ``computed_at`` (none here) and fleet ``skipped`` accounting — a
    verdict is filed against ONE target and the gate recomputes the
    single-namespace projection, so those would break byte-stability. Pure
    function of the entries + the addressed target + the contested counts.
    """
    return result_core


def _sha_prefix(sha: str) -> str:
    """The 8-hex display prefix (the R6 sha-prefix idiom); short but naming."""
    return sha[:8]


def _render(
    view: str,
    address: dict[str, Any],
    target_resolution: str | None,
    entries: Sequence[ChallengeEntryLine],
    contested: ChallengeContestedBlock,
) -> str:
    """Render the markdown brief — dated, sha-cited, mechanism-nouned.

    NO urgency / recommendation / interpretation vocabulary (the token pin): the
    brief states identities, dates, sha prefixes, and reduced statuses. ``grounds``
    and ``reasoning`` are echoed VERBATIM (opaque), never summarised. Pure function
    of its arguments — no wall-clock — so two calls render byte-identically and
    the verdict gate can recompute the ``view_sha``.
    """
    lines: list[str] = ["# challenge-status"]
    if view == "thread":
        lines.append(f"thread · challenge {address.get('challenge_id')}")
    else:
        if address.get("content_sha"):
            lines.append(f"target · content_sha {_sha_prefix(str(address['content_sha']))}")
        else:
            lines.append(f"target · {address.get('subject_kind')} · {address.get('subject_id')}")
    if target_resolution is not None:
        lines.append(f"target re-resolution · {target_resolution}")

    if (
        contested.open
        or contested.upheld
        or contested.dismissed
        or contested.withdrawn
        or contested.superseded
    ):
        lines.append(
            "contested · "
            f"{contested.open} open · {contested.upheld} upheld · "
            f"{contested.dismissed} dismissed · {contested.withdrawn} withdrawn · "
            f"{contested.superseded} superseded"
        )

    if not entries:
        lines.append("no standing challenges name this address.")
        return "\n".join(lines)

    for e in entries:
        lines.append("")
        lines.append(f"## {e.challenge_id} · {e.status} · filed {e.filed_ts}")
        lines.append(
            f"target · {e.target_kind} · {e.target_subject_kind} · {e.target_subject_id} · "
            f"sha {_sha_prefix(e.target_content_sha)} · {e.target_resolution}"
        )
        cited = ", ".join(
            f"{c.kind} {_sha_prefix(c.sha)} ({'verified' if c.verified else 'unresolvable here'})"
            for c in e.citations
        )
        lines.append(f"cites · {cited}" if cited else "cites · (none)")
        if e.verdict is not None:
            lines.append(f"verdict · {e.verdict}")
            if e.reasoning:
                lines.append(f"reasoning · {e.reasoning}")
        lines.append(f"grounds · {e.grounds}")
    return "\n".join(lines)


# --- the primitive ------------------------------------------------------------


@primitive(
    name="challenge-status",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Read the standing dissent over a committed record. Two views: a "
            "challenge_id (the filing/verdict/withdraw thread) or a target address "
            "(a content_sha, or a subject_kind+subject_id pair) — 'what stands "
            "against this record?'. Reduces each challenge to open / upheld / "
            "dismissed / withdrawn / superseded, re-resolves the target "
            "(found-current / found-superseded / unresolvable) and each cited "
            "evidence sha (verified / unresolvable) — DISCLOSED, never refused. "
            "Contested is an orthogonal flag beside the target's status, never "
            "blocking. Fleet-capable. Read-only; renders a deterministic markdown "
            "brief relayed verbatim, whose canonical-JSON view_sha a verdict may bind."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ChallengeStatusSpec,
        schema_ref=SchemaRef(input="challenge_status"),
    ),
    agent_facing=True,
)
def challenge_status(*, experiment_dir: Path, spec: ChallengeStatusSpec) -> ChallengeStatusResult:
    """Project the standing challenges over a thread (by id) or a target address.

    Single-experiment by default; ``spec.fleet`` widens to every journaled
    namespace (the non-creating ``discover_fleet_experiments`` walk — a torn
    namespace is skipped and counted). Every surface routes through the ONE
    collector ``state/challenges.py::standing_challenges`` (with the dossier
    resolver injected); the target and each citation are re-resolved and DISCLOSED
    (never refused — the E-read posture). ``contested`` rides beside the target's
    own status (C-status). The brief + ``view_sha`` are a pure function of the
    projection (no wall-clock, no fleet accounting), so the ``view_sha`` is
    byte-stable and the verdict gate recomputes it.

    Non-creating: reads only; writes nothing, scaffolds no journal.
    """
    exp = Path(experiment_dir)

    by_id = spec.challenge_id is not None
    view = "thread" if by_id else "target"

    if spec.fleet:
        experiments, ns_skipped = discover_fleet_experiments()
    else:
        experiments, ns_skipped = [exp], []

    collected: list[Any] = []
    for e in experiments:
        resolver = _dossier_resolver_for(e)
        if by_id:
            # Thread view: the collector has no id filter (C-reduce pins address
            # filtering only), so collect the namespace's standing challenges and
            # select the thread — still the ONE collector, never a private re-glob.
            found = standing_challenges(e, dossier_resolver=resolver)
            collected.extend(x for x in found if x.challenge_id == spec.challenge_id)
        else:
            collected.extend(
                standing_challenges(
                    e,
                    content_sha=spec.content_sha,
                    subject_kind=spec.subject_kind,
                    subject_id=spec.subject_id,
                    dossier_resolver=resolver,
                )
            )

    entry_lines = [_entry_line(x) for x in collected]
    contested = _contested_block(collected)

    # The addressed target's re-resolution: the entries agree on it (they all
    # name the addressed target); null when nothing names the address.
    target_resolution = entry_lines[0].target_resolution if entry_lines else None

    address = {
        "challenge_id": spec.challenge_id,
        "content_sha": spec.content_sha,
        "subject_kind": spec.subject_kind,
        "subject_id": spec.subject_id,
    }
    render = _render(view, address, target_resolution, entry_lines, contested)

    # view_sha over the dateless, fleet-free projection (the verdict gate
    # recomputes this — it must not depend on wall-clock or fleet state).
    projection = _projection(
        {
            "view": view,
            "address": address,
            "target_resolution": target_resolution,
            "entries": [line.model_dump() for line in entry_lines],
            "contested": contested.model_dump(),
        }
    )
    view_sha = canonical_sha(projection)

    return ChallengeStatusResult(
        view=view,
        addressed_challenge_id=spec.challenge_id,
        addressed_content_sha=spec.content_sha,
        addressed_subject_kind=spec.subject_kind,
        addressed_subject_id=spec.subject_id,
        target_resolution=target_resolution,
        entries=entry_lines,
        contested=contested,
        skipped=[ChallengeSkippedNamespace(ref=s["ref"], reason=s["reason"]) for s in ns_skipped],
        render=render,
        view_sha=view_sha,
    )
