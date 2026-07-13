"""``conformance-status`` — the read-only comparator seat of live conformance
(``docs/design/live-conformance.md`` C-verbs, Wave-B T5).

Given a ``registration_id`` and a caller-supplied window selection, this
``query`` primitive:

1. resolves the registration by reducing its journal (``reduce_registration``);
2. reads its SEALED ``conformance`` declaration off the winning record;
3. reads the sealed baseline artifact from the experiment tree and DISCLOSES any
   sha drift (never refuses on it — the membership gate is the append-time job);
4. selects the live window over the registration's ledger (T3's
   ``select_window`` — timestamp/count arithmetic only, no invented span);
5. calls the ONE comparator (``state/conformance.py::judge_window``); and
6. projects the report to the wire result + the deterministic code-rendered
   brief (``ops/conformance_render.py``).

Verdicts are DERIVED on every read — no verdict store, no watermark, nothing
marked seen (the attention-queue recompute posture; the write-probe test pins
that the query creates and mutates nothing).

Boundary posture (``docs/internals/engineering-principles.md`` Q1): every value
this op touches is opaque caller data — key slugs, payload values, label
strings, the emitter id are counted, range-compared, and echoed by IDENTITY,
never read for meaning. The only vocabularies core owns are the tier / tier_reason
set (C-compare) and the reused ``n>=3`` well-evidenced bar. This op OBSERVES,
JUDGES, and ROUTES; it never actuates, never mutates a registration, and reaches
no external system — the agency boundary, mechanized.

Two read postures the plan pins verbatim:

* **The declaration read (opt-in gate).** The comparator judges only a
  registration that OPTED IN: the winning record's ``resolved["conformance"]``.
  An ABSENT registration, or a winner carrying no ``conformance`` block, is a
  loud ``errors.SpecInvalid`` naming the gap — there is no hypothesis to test
  (the fabrication class). The declaration is validated STRUCTURE-only
  (``validate_declaration``); the ``conformance`` block on ``resolved`` is the
  registration T6 seam (not landed in this worktree), so it is read by its
  documented shape directly off the winner — see the ``# T6 seam`` note below.
* **The baseline read (disclose, never refuse).** The declaration names the
  sealed artifact ``{path, sha256}``; at status time we read that relpath from
  the experiment tree and verify its RAW sha equals the declared one. Drift (a
  mismatch) or an absent artifact is DISCLOSED in the brief and the report — the
  honest read-side posture — never a refusal. The append-time membership GATE
  (that the pair is a dossier member) is T7's job, not the reader's.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import hpc_agent.state.conformance as conformance
import hpc_agent.state.conformance_store as conformance_store
from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.conformance_status import (
    ConformanceBaseline,
    ConformanceStatusResult,
    ConformanceStatusSpec,
    ConformanceWindow,
    KeyVerdictLine,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.conformance_render import render_status_brief
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.registration import ABSENT, reduce_registration

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hpc_agent.state.conformance import ConformanceDeclaration, KeyVerdict

__all__ = ["conformance_status"]


# ── the T6 declaration seam ──────────────────────────────────────────────────


def _read_declaration(winner: Mapping[str, Any], registration_id: str) -> ConformanceDeclaration:
    """Read + validate the winner's SEALED ``conformance`` declaration, or refuse.

    # T6 seam: the ``conformance`` block on a registration's ``resolved`` is the
    # live-conformance registration amendment (plan T6 — ``state/registration.py``
    # gains structure-only validation of the block at append). That is NOT landed
    # in this worktree, so the block is read here by its DOCUMENTED shape
    # (C-declare) directly off the winner's ``resolved`` and validated
    # structure-only via ``validate_declaration``. When T6 lands, the append-time
    # validator and this read-time validator share the ONE ``validate_declaration``
    # definition — this seam collapses to a plain field read.

    An opted-in registration is REQUIRED: a winner carrying no ``conformance``
    block is a loud :class:`errors.SpecInvalid` naming the missing declaration
    (there is no hypothesis to test — the fabrication class; the D7 fail-safe
    posture, surfaced here as the reader's refusal rather than silent machinery).
    """
    raw = winner.get("conformance")
    if raw is None:
        raise errors.SpecInvalid(
            f"conformance-status: registration {registration_id!r} carries no "
            "'conformance' declaration — conformance is opt-in per registration "
            "(C-declare). There is no sealed hypothesis to judge live evidence "
            "against; the registration must be re-registered with a conformance "
            "block to be watched."
        )
    return conformance.validate_declaration(raw)


# ── the baseline read (disclose, never refuse) ───────────────────────────────


def _read_baseline(
    experiment_dir: Path, declaration: ConformanceDeclaration
) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Read the sealed baseline artifact → ``(rows, disclosure_note)``.

    The declaration names ``{path, sha256}`` inside the sealed dossier; at status
    time we read that relpath from the experiment tree and verify its RAW bytes'
    sha256 equals the declared one. The honest read-side posture (plan C-declare
    / the enforcement rows): DISCLOSE any gap, NEVER refuse — the membership gate
    that the pair belongs to the dossier is the append-time job (T7).

    Returns the parsed baseline rows (empty on any read/parse gap, so the
    comparator still runs and routes the thin baseline to the human) and a
    disclosure note (``None`` when the artifact is present AND its raw sha matches
    the declaration). The note is range-/identity-phrased — no urgency vocabulary.
    """
    rel = declaration.baseline.path
    declared_sha = declaration.baseline.sha256
    artifact = Path(experiment_dir) / rel
    try:
        data = artifact.read_bytes()
    except OSError:
        return (), (
            f"the declared baseline artifact {rel!r} is not readable in the "
            "experiment tree; no registered envelope could be read (comparison "
            "runs against an empty baseline and routes every key to a human)."
        )

    actual_sha = hashlib.sha256(data).hexdigest()
    note: str | None = None
    if actual_sha != declared_sha:
        note = (
            f"the on-disk baseline artifact {rel!r} sha does not match the sealed "
            f"declaration (declared {declared_sha[:12]}..., on-disk {actual_sha[:12]}...); "
            "the sealed evidence moved - treat the comparison below as provisional."
        )

    rows = _parse_baseline_bytes(data)
    if rows is None:
        parse_note = (
            f"the baseline artifact {rel!r} is not a JSON list of {{key: scalar}} rows; "
            "no registered envelope could be read."
        )
        return (), note or parse_note
    return rows, note


def _parse_baseline_bytes(data: bytes) -> tuple[dict[str, Any], ...] | None:
    """Parse baseline bytes to rows via the kernel's ``parse_baseline_rows``, or ``None``.

    Accepts either a bare JSON list of row objects, or a ``{"rows": [...]}``
    envelope. Any decode/shape gap yields ``None`` (disclosed upstream) — a
    malformed artifact never crashes a read-only reporter.
    """
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(obj, dict) and "rows" in obj:
        obj = obj["rows"]
    try:
        return conformance.parse_baseline_rows(obj)
    except errors.SpecInvalid:
        return None


# ── window evidence projection ───────────────────────────────────────────────


def _window_span(window: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """The observed ``(since, until)`` span over the selected window, or ``(None, None)``.

    Disclosed for EVERY selection mode (count OR timestamp), so a count-based
    window still states its observed span (C-compare step 2). Reads ``observed_at``
    only — timestamp comparison, never a fabricated bound.
    """
    stamps = sorted(
        r["observed_at"]
        for r in window
        if isinstance(r.get("observed_at"), str) and r["observed_at"]
    )
    if not stamps:
        return None, None
    return stamps[0], stamps[-1]


def _window_labels(window: Sequence[Mapping[str, Any]]) -> list[str]:
    """Distinct opaque label-set signatures observed across the window, sorted.

    Each receipt's ``labels`` mapping renders to a stable ``k=v,…`` signature;
    novelty is DISCLOSED (a heterogeneous window), never interpreted. Order-stable
    so the render is byte-deterministic.
    """
    sigs: set[str] = set()
    for r in window:
        labels = r.get("labels")
        if not isinstance(labels, Mapping) or not labels:
            continue
        sigs.add(",".join(f"{k}={labels[k]}" for k in sorted(labels)))
    return sorted(sigs)


def _key_line(kv: KeyVerdict) -> KeyVerdictLine:
    """Project a state :class:`KeyVerdict` to the wire :class:`KeyVerdictLine`.

    Both sides' order statistics range-phrased; the four bounds are absent when a
    side carried no comparable statistics (novel/incomparable); the two ns are
    always present — the mechanical evidence the classifier routed on.
    """
    return KeyVerdictLine(
        key=kv.key,
        tier_reason=kv.tier_reason,  # type: ignore[arg-type]
        window_lo=kv.window.lo if kv.window is not None else None,
        window_hi=kv.window.hi if kv.window is not None else None,
        baseline_lo=kv.baseline.lo if kv.baseline is not None else None,
        baseline_hi=kv.baseline.hi if kv.baseline is not None else None,
        window_n=kv.window_n,
        baseline_n=kv.baseline_n,
    )


# ── the primitive ─────────────────────────────────────────────────────────────


@primitive(
    name="conformance-status",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Report a registration's LIVE conformance: reduce its journal, read "
            "its sealed conformance declaration + baseline, select a caller-named "
            "window over the ledger ({since,until?} or last_n), and judge it "
            "against the REGISTERED evidence via the one comparator. Returns "
            "per-key verdicts (within/outside the envelope, or a thin/novel/"
            "incomparable route to needs_verdict), the overall tier, both sides' "
            "range-phrased evidence, and a deterministic brief. Verdicts are "
            "DERIVED on every read — no verdict store, nothing marked seen. A "
            "reporter: a nonconforming window is a FINDING that changes no "
            "registration status. Read-only, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ConformanceStatusSpec,
        schema_ref=SchemaRef(input="conformance_status"),
    ),
    agent_facing=True,
)
def conformance_status(
    *, experiment_dir: Path, spec: ConformanceStatusSpec
) -> ConformanceStatusResult:
    """Report a registration's live conformance — DERIVED on read (T5).

    Resolves the registration, reads its sealed declaration + baseline (drift
    disclosed, never refused), selects the caller-named window over the ledger,
    calls the ONE comparator, and returns per-key verdicts + the overall tier +
    both sides' evidence + a deterministic brief. Creates and mutates nothing
    (the write-probe pin). Raises :class:`errors.SpecInvalid` only when there is
    no opted-in hypothesis to judge (absent registration / missing declaration)
    or the window selection is malformed.
    """
    experiment_dir = Path(experiment_dir)
    registration_id = str(spec.registration_id)

    # 1. Resolve the registration by reducing its journal (the registration scope
    #    kind is landed via the registration kernel; the live-sha is irrelevant to
    #    a conformance read — we only need the winner's resolved).
    records = read_decisions(experiment_dir, "registration", registration_id)
    status = reduce_registration(records, registration_id=registration_id, live_dossier_sha=None)
    if status.status == ABSENT:
        raise errors.SpecInvalid(
            f"conformance-status: no registration named {registration_id!r} — there "
            "is no sealed hypothesis to judge live evidence against (the fabrication "
            "class; an observation naming an absent registration is likewise refused "
            "at record time)."
        )
    winner: Mapping[str, Any] = status.winner or {}

    # 2. The opt-in gate: the winner's sealed conformance declaration (T6 seam).
    declaration = _read_declaration(winner, registration_id)

    # 3. The sealed baseline (disclose drift; never refuse).
    baseline_rows, baseline_note = _read_baseline(experiment_dir, declaration)
    sealed_at = status.registered_at

    # 4. The live window (T3's selection — arithmetic only, no invented span).
    ledger, _skipped = conformance_store.read_observations(experiment_dir, registration_id)
    window = conformance_store.select_window(
        ledger, since=spec.since, until=spec.until, last_n=spec.last_n
    )

    # 5. The ONE comparator (derived on read).
    report = conformance.judge_window(baseline_rows, window, declaration, now=utcnow_iso())

    # 6. Project to the wire result + the deterministic brief.
    window_since, window_until = _window_span(window)
    window_labels = _window_labels(window)
    baseline_n = _baseline_n(baseline_rows, declaration)

    brief = render_status_brief(
        registration_id=registration_id,
        report=report,
        baseline_n=baseline_n,
        sealed_at=sealed_at,
        baseline_note=baseline_note,
        window_since=window_since,
        window_until=window_until,
        window_labels=window_labels,
        declaration=declaration,
    )

    declaration_echo: dict[str, str | int | list[str] | None] = {
        "keys": list(declaration.keys),
        "min_window_n": declaration.min_window_n,
        "review_horizon": declaration.review_horizon,
    }

    return ConformanceStatusResult(
        registration_id=registration_id,
        overall=report.tier,  # type: ignore[arg-type]
        keys=[_key_line(kv) for kv in report.keys],
        window=ConformanceWindow(
            n=report.window_n,
            since=window_since,
            until=window_until,
            labels=window_labels,
        ),
        baseline=ConformanceBaseline(n=baseline_n, sealed_at=sealed_at),
        declaration_echo=declaration_echo,
        render=brief,
    )


def _baseline_n(
    baseline_rows: Sequence[Mapping[str, Any]], declaration: ConformanceDeclaration
) -> int:
    """The sealed baseline's row count — the registered side's headline evidence.

    The number of sealed rows read (point-in-time; it never grows — live
    observations never enter it). Per-key baseline ns live on each
    :class:`KeyVerdictLine`; this is the artifact-level count for the brief's
    "baseline n=126 sealed …" label.
    """
    return len(baseline_rows)
