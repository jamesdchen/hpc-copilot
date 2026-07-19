"""``program-verify`` — the program-level projection over recorded reproductions.

Program-level reproduction, phase 1 (RECEIPTS-FIRST): read the reproduction
evidence ALREADY on record for a program's run-set and disclose the roll-up;
fresh re-runs are a later phase's ceremony. A *program*'s identity is EMERGENT —
the run-set behind a citable results table (the ``extract-recipe`` seed), never a
declared-up-front key. The verdict DISCLOSES, never gates.

This verb is a PURE projection over recorded JUDGMENTS, never a new comparator:

* It resolves the constituent run-set either from an explicit ``run_ids`` list or
  by reusing ``extract-recipe``'s walk as a LIBRARY CALL (never re-deriving the
  minimal set); when that walk degrades to the G4a harvest-receipt proxy, the
  disclosure is passed through exactly as ``extract-recipe`` emits it.
* For each constituent it gathers the reproduction receipts reachable via the
  ``reproduces`` back-link (mirroring the READ side of
  ``ops/verify_reproduction.py``'s write: ``_aggregated/<repro_run_id>/
  reproduction_receipts.jsonl``, each receipt naming its ``original``), plus the
  determinism-fingerprint ledger samples for the run's identity. It classifies
  each constituent FROM the receipt's own ``overall`` vocabulary — a
  ``needs_verdict`` counts as reproduced only when the fingerprint admission join
  (a recorded HUMAN acceptance) says so. It NEVER re-compares a metric.
* It folds a program roll-up (the ``verify_reproduction._fold_overall`` idiom),
  materializes a WRITE-ONCE signed program manifest mirroring
  ``provenance_manifest``'s shape (reusing its :func:`manifest_signature` helper —
  which is not campaign-coupled), and renders a deterministic CODE report.

Like ``extract-recipe`` this file lives at the ``ops/`` role root (it reads
across subjects — the state sidecars, the aggregate receipts, the fingerprint
ledger, the campaign finders — and composes the shipped ``extract-recipe`` walk),
so the subject-imports lint short-circuits for it by construction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.program_verify import (
    ConstituentVerdict,
    ProgramVerifyResult,
    ProgramVerifySpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef

#: Bump when the emitted program-verify shape changes in a way a consumer branches on.
PROGRAM_SCHEMA_VERSION: int = 1

# ── the five receipt-derived classifications (mirrors the wire Literal) ───────
# MIRROR: hpc_agent/_wire/queries/program_verify.py::ConstituentClassification pinned-by tests/ops/test_program_verify.py::test_class_constants_match_the_wire_literal  # noqa: E501
CLASS_REPRODUCED = "reproduced_within_tolerance"
CLASS_MISMATCH = "mismatch_on_record"
CLASS_INCOMPARABLE = "evidence_incomparable"
CLASS_STALE_IDENTITY = "evidence_stale_identity"
CLASS_NONE = "no_reproduction_on_record"

#: PROGRAM roll-up severity (higher folds — the ``_fold_overall`` idiom): a
#: recorded contradiction dominates; then an untried constituent (a total absence
#: of evidence) — and, folded to the SAME tier, a ``evidence_stale_identity``
#: constituent (its receipt was earned at a superseded identity, so it is no
#: current evidence, distinctly named); then an executed-but-incomparable
#: comparison; a clean reproduction is least severe. A program is
#: ``reproduced_within_tolerance`` only when EVERY constituent is.
_PROGRAM_SEVERITY: dict[str, int] = {
    CLASS_REPRODUCED: 0,
    CLASS_INCOMPARABLE: 1,
    CLASS_STALE_IDENTITY: 2,
    CLASS_NONE: 2,
    CLASS_MISMATCH: 3,
}

#: The identity legs compared receipt-``original`` ↔ CURRENT sidecar to decide
#: whether a receipt's verdict was earned at a since-superseded identity. The
#: three code-identity legs plus ``data_sha`` (present-only — a run with no data
#: never records it, so its absence is never drift).
_IDENTITY_DRIFT_LEGS: tuple[str, ...] = ("cmd_sha", "tasks_py_sha", "executor", "data_sha")


def _read_json(path: Path) -> Any:
    """Parse a JSON file, or None on any absence/read/parse error (never raises)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


# ── receipt read side (mirror verify_reproduction's write locations) ──────────


def _repro_receipts_path(experiment_dir: Path, repro_run_id: str) -> Path:
    """The append-only receipts ledger a reproduction wrote — the READ mirror of
    ``ops/verify_reproduction.py::_receipt_path`` (never a second definition of
    the location, just the read side)."""
    return experiment_dir / "_aggregated" / repro_run_id / "reproduction_receipts.jsonl"


def _read_receipts(path: Path) -> list[dict[str, Any]]:
    """Tolerant JSONL read of a receipts ledger (the ``read_samples`` idiom).

    Blank / individually-corrupt lines are skipped so one torn line never strands
    the rest of a scientific record. Returns ``[]`` when the ledger is absent.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _reproduction_receipts_for(experiment_dir: Path, constituent: str) -> list[dict[str, Any]]:
    """Every ``reproduction`` receipt that names *constituent* as its original.

    Walks ``_aggregated/*/reproduction_receipts.jsonl`` (the reproduces back-link
    is embedded in each receipt's ``original.run_id`` — a receipt exists only
    because ``verify-reproduction`` validated the repro sidecar's ``reproduces``
    link at write time), filtering to genuine reproduction receipts for this
    constituent. Ordered by (repro_run_id, file order) so the fold is
    deterministic. Read-only; never a fresh comparison.
    """
    agg = experiment_dir / "_aggregated"
    if not agg.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for run_dir in sorted(agg.iterdir(), key=lambda p: p.name):
        if not run_dir.is_dir():
            continue
        ledger = _repro_receipts_path(experiment_dir, run_dir.name)
        if not ledger.is_file():
            continue
        for receipt in _read_receipts(ledger):
            if receipt.get("receipt_kind") != "reproduction":
                continue
            original = receipt.get("original")
            if isinstance(original, Mapping) and original.get("run_id") == constituent:
                out.append(receipt)
    return out


# ── the fingerprint admission join (recorded human judgment, reused) ──────────


def _pair_admitted(experiment_dir: Path, cmd_sha: str, original_id: str, repro_id: str) -> bool:
    """True when the fingerprint ledger holds an ADMITTED sample for this pair.

    Reuses the store-layer admission join (``fingerprint_store``): an
    ``auto_cleared`` sample is admitted by construction; a ``needs_verdict`` /
    ``mismatch`` sample is admitted ONLY when the reproduction run's decision
    journal carries a human ``reproduction-verdict`` acceptance token-exact on the
    bind-locked ``content_sha``. This is the recorded HUMAN judgment — program-verify
    reads it, never re-derives it. Best-effort: any read failure reads NOT admitted.
    """
    if not cmd_sha:
        return False
    from hpc_agent.state.fingerprint_store import compute_admitted_flags, read_samples

    try:
        samples, _ = read_samples(experiment_dir, cmd_sha)
    except errors.SpecInvalid:
        return False
    pair = [
        s
        for s in samples
        if isinstance(s.get("run_ids"), (list, tuple))
        and list(s.get("run_ids") or [])[:2] == [original_id, repro_id]
    ]
    if not pair:
        return False
    flags, _ = compute_admitted_flags(experiment_dir, pair)
    return any(flags)


def _fingerprint_sample_count(experiment_dir: Path, identity: Mapping[str, Any]) -> int:
    """Count the fingerprint ledger samples for a constituent's CURRENT identity.

    Reads the ledger keyed on ``cmd_sha`` and partitions to the current-identity
    set (``cmd_sha`` / ``tasks_py_sha`` / ``executor``) — the code-identity legs
    the directive names. Best-effort: 0 when the ledger is absent / unreadable.
    """
    cmd_sha = str(identity.get("cmd_sha") or "")
    if not cmd_sha:
        return 0
    from hpc_agent.state.fingerprint_store import partition_current_identity, read_samples

    try:
        samples, _ = read_samples(experiment_dir, cmd_sha)
    except errors.SpecInvalid:
        return 0
    current, _stale, _unknown = partition_current_identity(samples, identity)
    return len(current)


# ── classification (receipt vocabulary → the four classes) ────────────────────


def _classify_receipt(experiment_dir: Path, receipt: Mapping[str, Any]) -> str:
    """Classify ONE reproduction receipt from its own ``overall`` vocabulary.

    * ``auto_cleared`` / ``match`` → reproduced_within_tolerance.
    * ``mismatch`` → mismatch_on_record (the recorded contradiction, never hidden).
    * ``needs_verdict`` → reproduced_within_tolerance IF a human cleared it (the
      fingerprint admission join says admitted), else evidence_incomparable.
    * ``incomparable`` / anything else → evidence_incomparable.

    Never a fresh comparison — the receipt already recorded the verdict.
    """
    overall = receipt.get("overall")
    if overall in ("auto_cleared", "match"):
        return CLASS_REPRODUCED
    if overall == "mismatch":
        return CLASS_MISMATCH
    if overall == "needs_verdict":
        original = receipt.get("original")
        repro = receipt.get("repro")
        original_id = original.get("run_id") if isinstance(original, Mapping) else None
        repro_id = repro.get("run_id") if isinstance(repro, Mapping) else None
        cmd_sha = str(original.get("cmd_sha") or "") if isinstance(original, Mapping) else ""
        if (
            isinstance(original_id, str)
            and isinstance(repro_id, str)
            and _pair_admitted(experiment_dir, cmd_sha, original_id, repro_id)
        ):
            return CLASS_REPRODUCED
        return CLASS_INCOMPARABLE
    return CLASS_INCOMPARABLE


def _fold_constituent(classes: Sequence[str]) -> str:
    """Fold one constituent's per-receipt classes into a single classification.

    A recorded ``mismatch`` dominates (never hidden by a matching sibling); else a
    clean ``reproduced`` stands over a weaker ``incomparable`` attempt; else
    ``incomparable``; else — no receipts — ``no_reproduction_on_record``.
    """
    if not classes:
        return CLASS_NONE
    s = set(classes)
    if CLASS_MISMATCH in s:
        return CLASS_MISMATCH
    if CLASS_REPRODUCED in s:
        return CLASS_REPRODUCED
    if CLASS_INCOMPARABLE in s:
        return CLASS_INCOMPARABLE
    return CLASS_NONE


# MIRROR: hpc_agent/ops/verify_reproduction.py::_render_reason (the counting discipline) pinned-by tests/ops/test_program_verify.py::test_receipt_reason_counting_matches_verify_reproduction  # noqa: E501
def _receipt_reason(receipt: Mapping[str, Any], classification: str, repro_id: str | None) -> str:
    """Code-render a constituent reason off the DRIVING receipt's own keys.

    Reads the receipt's ``overall`` + per-key verdict counts (never an LLM number),
    the reproduction run it came from, and any localized diverging stage. Mirrors
    ``verify_reproduction._render_reason``'s counting discipline.
    """
    per_key = receipt.get("per_key")
    per_key = per_key if isinstance(per_key, list) else []
    n_match = sum(1 for e in per_key if isinstance(e, Mapping) and e.get("verdict") == "match")
    n_mismatch = sum(
        1 for e in per_key if isinstance(e, Mapping) and e.get("verdict") == "mismatch"
    )
    n_incomp = sum(
        1 for e in per_key if isinstance(e, Mapping) and e.get("verdict") == "incomparable"
    )
    total = len(per_key)
    overall = receipt.get("overall")
    where = f" [repro {repro_id}]" if repro_id else ""
    detail = (
        f"{n_match} matched, {n_mismatch} mismatched, {n_incomp} incomparable of "
        f"{total} key{'s' if total != 1 else ''}"
    )
    tail = ""
    diverged = receipt.get("diverged_stage")
    if isinstance(diverged, str) and diverged and classification != CLASS_REPRODUCED:
        tail = f"; diverges at stage {diverged!r}"
    return f"{classification}: receipt overall={overall} — {detail}{where}{tail}"


def _identity_drift(
    receipt_original: Mapping[str, Any], sidecar_identity: Mapping[str, Any]
) -> tuple[list[tuple[str, Any, Any]], list[str]]:
    """Compare a driving receipt's ``original`` identity legs to the CURRENT sidecar.

    Returns ``(mismatched, unrecorded)``:

    * ``mismatched`` — ``(leg, receipt_value, current_value)`` for every leg BOTH
      sides carry whose values differ (the receipt earned its verdict at a
      now-superseded identity — stale evidence).
    * ``unrecorded`` — the code-identity leg names the RECEIPT side does not carry
      (an old receipt predating the leg, or an executor never written into the
      receipt's ``original`` block): disclosed, never demoted, never invented into
      drift. ``data_sha`` is present-only (the directive's "when present") — its
      absence is silent, not disclosed.

    A leg absent on the SIDECAR side is likewise not a mismatch (compare only the
    legs BOTH sides carry) — it joins the unrecorded disclosure so a missing field
    never manufactures a false stale.
    """
    mismatched: list[tuple[str, Any, Any]] = []
    unrecorded: list[str] = []
    for leg in _IDENTITY_DRIFT_LEGS:
        r_val = receipt_original.get(leg)
        s_val = sidecar_identity.get(leg)
        r_present = r_val is not None and r_val != ""
        s_present = s_val is not None and s_val != ""
        if not (r_present and s_present):
            # data_sha is an optional present-only leg — silence its absence.
            if leg != "data_sha":
                unrecorded.append(leg)
            continue
        if r_val != s_val:
            mismatched.append((leg, r_val, s_val))
    return mismatched, unrecorded


def _unrecorded_phrase(unrecorded: Sequence[str]) -> str:
    """Code-render the "identity leg <name> unrecorded" disclosure clause, or ``""``."""
    if not unrecorded:
        return ""
    return "; " + ", ".join(f"identity leg {leg} unrecorded" for leg in unrecorded)


def _stale_reason(
    mismatched: Sequence[tuple[str, Any, Any]],
    unrecorded: Sequence[str],
    repro_id: str | None,
) -> str:
    """Code-render the ``evidence_stale_identity`` reason — every drifted leg with
    both shas, plus any unrecorded-leg disclosure (never an LLM number)."""
    legs = "; ".join(
        f"receipt earned at {leg}={r_val}, run now at {s_val}" for leg, r_val, s_val in mismatched
    )
    where = f" [repro {repro_id}]" if repro_id else ""
    return (
        f"{CLASS_STALE_IDENTITY}: the driving receipt's evidence was earned at a "
        f"superseded identity — {legs}{where}{_unrecorded_phrase(unrecorded)}"
    )


def _constituent_verdict(experiment_dir: Path, run_id: str) -> ConstituentVerdict:
    """Project one constituent's recorded reproduction evidence (read-only)."""
    from hpc_agent.state.runs import read_run_sidecar_or_empty

    sidecar = read_run_sidecar_or_empty(experiment_dir, run_id)
    identity = {
        "cmd_sha": sidecar.get("cmd_sha"),
        "tasks_py_sha": sidecar.get("tasks_py_sha"),
        "executor": sidecar.get("executor"),
    }
    # The data leg rides alongside the code-identity legs for the drift check only
    # (present-only), and never enters the fingerprint-keying identity above.
    drift_identity = {**identity, "data_sha": sidecar.get("data_sha")}
    receipts = _reproduction_receipts_for(experiment_dir, run_id)
    repro_ids = sorted(
        {
            str((r.get("repro") or {}).get("run_id"))
            for r in receipts
            if isinstance(r.get("repro"), Mapping) and (r.get("repro") or {}).get("run_id")
        }
    )
    per_receipt = [(_classify_receipt(experiment_dir, r), r) for r in receipts]
    classification = _fold_constituent([c for c, _ in per_receipt])

    # The driving receipt: the LAST (most recent append) receipt whose class
    # equals the folded classification — so the reason + disclosures come from the
    # receipt that actually decided the verdict.
    driving: dict[str, Any] | None = None
    driving_repro: str | None = None
    for cls, receipt in per_receipt:
        if cls == classification:
            driving = dict(receipt)
            repro = receipt.get("repro")
            driving_repro = repro.get("run_id") if isinstance(repro, Mapping) else None

    if driving is None:
        reason = "no reproduction receipt on record"
    else:
        original = driving.get("original")
        original = original if isinstance(original, Mapping) else {}
        mismatched, unrecorded = _identity_drift(original, drift_identity)
        if mismatched:
            # The receipt's verdict was earned at a since-superseded identity — it
            # is no CURRENT evidence, however it once classified. Distinctly named.
            classification = CLASS_STALE_IDENTITY
            reason = _stale_reason(mismatched, unrecorded, driving_repro)
        else:
            reason = _receipt_reason(driving, classification, driving_repro) + _unrecorded_phrase(
                unrecorded
            )

    diverged = driving.get("diverged_stage") if isinstance(driving, dict) else None
    return ConstituentVerdict(
        run_id=run_id,
        classification=classification,  # type: ignore[arg-type]
        reason=reason,
        receipt_count=len(receipts),
        repro_run_ids=repro_ids,
        cmd_sha=identity["cmd_sha"],
        tasks_py_sha=identity["tasks_py_sha"],
        executor=identity["executor"],
        fingerprint_samples=_fingerprint_sample_count(experiment_dir, identity),
        driving_receipt=driving,
        env_identity=(driving or {}).get("env_identity") if driving else None,
        hw_identity=(driving or {}).get("hw_identity") if driving else None,
        data_identity=(driving or {}).get("data_identity") if driving else None,
        diverged_stage=diverged if isinstance(diverged, str) else None,
    )


# ── seed resolution (reuse extract-recipe's walk as a library call) ───────────


def _resolve_program(
    experiment_dir: Path, spec: ProgramVerifySpec
) -> tuple[str, str, list[str], str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve the spec → ``(seed_kind, seed_ref, run_ids, recipe_signature, gaps, fingerprints)``.

    Explicit ``run_ids`` resolve directly (no walk). A ``campaign_id`` /
    ``aggregate_path`` seed reuses ``extract-recipe`` — never re-deriving the
    minimal set — so its exclusions, ``recipe_signature``, per-run fingerprints,
    and G4 gap disclosures (incl. the G4a proxy) are the SAME the recipe emits.
    """
    from hpc_agent.ops.extract_recipe import _fingerprint

    if spec.run_ids:
        run_ids = list(spec.run_ids)
        fingerprints = [_fingerprint(experiment_dir, rid) for rid in run_ids]
        return "explicit", ",".join(run_ids), run_ids, None, [], fingerprints

    from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput
    from hpc_agent.ops.extract_recipe import extract_recipe

    if spec.campaign_id and spec.campaign_id.strip():
        recipe_spec = ExtractRecipeInput(campaign_id=spec.campaign_id.strip())
    else:
        recipe_spec = ExtractRecipeInput(aggregate_path=(spec.aggregate_path or "").strip())
    recipe = extract_recipe(experiment_dir, spec=recipe_spec)
    seed_kind = str(recipe.get("seed_kind") or "aggregate")
    # extract-recipe emits seed_kind in {run, campaign, aggregate}; program-verify
    # only seeds from campaign / aggregate, so a "run" would be an internal drift.
    if seed_kind not in ("campaign", "aggregate"):
        seed_kind = "aggregate"
    return (
        seed_kind,
        str(recipe.get("seed_ref") or ""),
        list(recipe.get("minimal_run_ids") or []),
        recipe.get("recipe_signature"),
        list(recipe.get("gaps") or []),
        list(recipe.get("runs") or []),
    )


# ── the write-once signed program manifest ────────────────────────────────────


def _program_manifest_body(
    *,
    seed_kind: str,
    seed_ref: str,
    recipe_signature: str | None,
    resolved: list[str],
    fingerprints: list[dict[str, Any]],
) -> dict[str, Any]:
    """The IDENTITY-only signable manifest body (verdicts stay OUT so re-runs are
    idempotent — a new receipt must not churn the manifest signature)."""
    return {
        "manifest_kind": "program-verify",
        "program_schema_version": PROGRAM_SCHEMA_VERSION,
        "seed_kind": seed_kind,
        "seed_ref": seed_ref,
        "recipe_signature": recipe_signature,
        "resolved_run_ids": list(resolved),
        "runs": list(fingerprints),
    }


def _find_prior_manifest_signature(
    prov_dir: Path, *, seed_kind: str, seed_ref: str, exclude: Path
) -> str | None:
    """The signature of the most recent PRIOR program manifest for the same seed.

    Scans ``program-*.json`` for a body whose ``(seed_kind, seed_ref)`` matches,
    excluding *exclude* (the target we're about to write). Returns its signature so
    a content drift (a changed run-set / fingerprint → a new signature) can be
    disclosed. None when there is no prior manifest for this seed.
    """
    if not prov_dir.is_dir():
        return None
    best: tuple[float, str] | None = None
    for path in prov_dir.glob("program-*.json"):
        if path == exclude:
            continue
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        if data.get("seed_kind") != seed_kind or data.get("seed_ref") != seed_ref:
            continue
        sig = data.get("signature")
        if not isinstance(sig, str) or not sig:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, sig)
    return best[1] if best is not None else None


def _write_program_manifest(
    experiment_dir: Path, body: dict[str, Any], signature: str
) -> tuple[str, str | None]:
    """Write the WRITE-ONCE signed manifest; return ``(path, delta_disclosure)``.

    Idempotent: the same seed over the same on-disk identity re-derives the same
    signature → the same ``program-<sig[:12]>.json``; if it already exists with a
    matching signature it is NOT rewritten (write-once honored). A content drift
    (a different signature for the same seed) writes a NEW file and discloses the
    delta against the prior manifest. Reuses ``provenance_manifest``'s signable
    shape (body + top-level ``signature``).
    """
    from hpc_agent._kernel.contract.layout import RepoLayout
    from hpc_agent.infra.io import atomic_write_json

    prov_dir = RepoLayout(experiment_dir).hpc / "provenance"
    target = prov_dir / f"program-{signature[:12]}.json"

    if target.is_file():
        existing = _read_json(target)
        if isinstance(existing, dict) and existing.get("signature") == signature:
            return str(target), None  # write-once: already materialized, no rewrite

    delta: str | None = None
    prior_sig = _find_prior_manifest_signature(
        prov_dir, seed_kind=str(body["seed_kind"]), seed_ref=str(body["seed_ref"]), exclude=target
    )
    if prior_sig is not None and prior_sig != signature:
        delta = (
            f"content drifted from program-{prior_sig[:12]} to program-{signature[:12]} "
            f"(seed {body['seed_ref']!r}): the resolved run-set or a constituent "
            "fingerprint changed — a new write-once manifest was materialized"
        )

    prov_dir.mkdir(parents=True, exist_ok=True)
    written = dict(body)
    written["signature"] = signature
    atomic_write_json(target, written)
    return str(target), delta


# ── code render (deterministic; no LLM, no metric value) ──────────────────────


def _class_counts(constituents: Sequence[ConstituentVerdict]) -> dict[str, int]:
    counts = {
        CLASS_REPRODUCED: 0,
        CLASS_MISMATCH: 0,
        CLASS_INCOMPARABLE: 0,
        CLASS_STALE_IDENTITY: 0,
        CLASS_NONE: 0,
    }
    for c in constituents:
        counts[c.classification] += 1
    return counts


def _stale_identity_gaps(constituents: Sequence[ConstituentVerdict]) -> list[dict[str, Any]]:
    """One disclosed gap per ``evidence_stale_identity`` constituent (program-level
    visibility of the drift the per-constituent reason already names)."""
    return [
        {"code": "constituent-evidence-stale-identity", "detail": f"{c.run_id}: {c.reason}"}
        for c in constituents
        if c.classification == CLASS_STALE_IDENTITY
    ]


def _render_reason(
    overall: str,
    reproduced: int,
    total: int,
    counts: Mapping[str, int],
    gaps: Sequence[Mapping[str, Any]],
) -> str:
    """Code-rendered one-line program summary — counts only, never a metric."""
    return (
        f"program verdict: {overall} — {reproduced}/{total} reproduced_within_tolerance "
        f"({counts[CLASS_MISMATCH]} mismatch, {counts[CLASS_INCOMPARABLE]} incomparable, "
        f"{counts[CLASS_STALE_IDENTITY]} stale-identity, "
        f"{counts[CLASS_NONE]} no-reproduction); walk gaps: {len(gaps)}"
    )


# MIRROR: hpc_agent/ops/recipe_render.py::render_recipe (the identity + counting + disclosure render discipline) pinned-by tests/ops/test_program_verify.py::test_render_discipline_lockstep_with_recipe_render  # noqa: E501
def _render_markdown(result: Mapping[str, Any]) -> str:
    """Deterministic markdown render over the result's own fields (LLM-free).

    Mirrors ``recipe_render``'s discipline: identity (which runs, at which shas) +
    counting (per-class counts) + disclosure (each non-reproduced constituent named
    with its receipt-recorded reason). Byte-stable for a given state — no
    timestamps, sorted where order is not load-bearing.
    """
    seed_kind = str(result.get("seed_kind", ""))
    seed_ref = str(result.get("seed_ref", ""))
    resolved = list(result.get("resolved_run_ids") or [])
    constituents = list(result.get("constituents") or [])
    gaps = list(result.get("gaps") or [])
    reproduced = int(result.get("reproduced_count", 0))
    total = int(result.get("total", 0))
    overall = str(result.get("overall", ""))

    lines: list[str] = []
    lines.append(f"# Program-verify — {seed_kind} `{seed_ref}`")
    lines.append("")
    lines.append(f"verdict: **{overall}** — {reproduced}/{total} reproduced_within_tolerance")
    sig = result.get("program_signature")
    if sig:
        lines.append("")
        lines.append(f"program signature: `{sig}`")
    recipe_sig = result.get("recipe_signature")
    if recipe_sig:
        lines.append(f"recipe signature: `{recipe_sig}`")
    delta = result.get("manifest_delta")
    if delta:
        lines.append("")
        lines.append(f"> {delta}")
    lines.append("")

    lines.append(f"## Constituents ({len(resolved)})")
    lines.append("")
    if constituents:
        lines.append("| run_id | classification | receipts | fp_samples | reason |")
        lines.append("|---|---|---|---|---|")
        for c in constituents:
            lines.append(
                f"| {c.get('run_id', '')} | {c.get('classification', '')} | "
                f"{int(c.get('receipt_count', 0))} | {int(c.get('fingerprint_samples', 0))} | "
                f"{c.get('reason', '')} |"
            )
    else:
        lines.append("_(no constituents resolved)_")
    lines.append("")

    lines.append(f"## Disclosed walk gaps ({len(gaps)})")
    lines.append("")
    if gaps:
        for g in gaps:
            lines.append(f"- **{g.get('code', '')}** — {g.get('detail', '')}")
    else:
        lines.append("_(no walk gaps — the run-set link is first-class)_")
    lines.append("")

    return "\n".join(lines)


# ── the primitive ─────────────────────────────────────────────────────────────


@primitive(
    name="program-verify",
    verb="query",
    side_effects=[
        SideEffect(
            "filesystem",
            "<experiment>/.hpc/provenance/program-<program_signature[:12]>.json (write-once)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,  # write-once manifest; same seed + same identity → same file
    idempotency_key=None,
    agent_facing=True,
    cli=CliShape(
        help=(
            "Project the RECORDED reproduction evidence for a program's run-set "
            "(explicit run_ids XOR an extract-recipe campaign / aggregate seed) and "
            "disclose the roll-up: per-constituent classification read off the "
            "reproduction receipts (reproduced_within_tolerance / mismatch_on_record "
            "/ evidence_incomparable / evidence_stale_identity / "
            "no_reproduction_on_record), a k/N fold, the "
            "extract-recipe walk gaps, and a write-once signed program manifest. "
            "Read-only over recorded judgments — it never re-compares a metric. A "
            "not-fully-reproduced program is a FINDING (needs_decision), never an error."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ProgramVerifySpec,
        requires_ssh=False,
        schema_ref=SchemaRef(input="program_verify"),
    ),
)
def program_verify(experiment_dir: Path, *, spec: ProgramVerifySpec) -> ProgramVerifyResult:
    """Project a program's recorded reproduction evidence into a disclosed roll-up.

    Resolves the constituent run-set (explicit or via the reused ``extract-recipe``
    walk), classifies each constituent FROM its reproduction receipts + fingerprint
    admission (never a fresh comparison), folds the program verdict, materializes
    the write-once signed manifest, and renders a deterministic CODE report.

    Raises :class:`errors.SpecInvalid` only on a bad spec (the seed XOR rule, or an
    ``extract-recipe`` seed that does not resolve) — otherwise always succeeds
    (exit-0); a not-fully-reproduced program is a ``needs_decision`` finding.
    """
    from hpc_agent.ops.provenance_manifest import manifest_signature

    experiment_dir = Path(experiment_dir)

    seed_kind, seed_ref, resolved, recipe_signature, gaps, fingerprints = _resolve_program(
        experiment_dir, spec
    )

    constituents = [_constituent_verdict(experiment_dir, rid) for rid in resolved]
    counts = _class_counts(constituents)
    reproduced = counts[CLASS_REPRODUCED]
    total = len(constituents)

    # Carry the identity-drift disclosure up to the program gaps list (the
    # per-constituent reason already names each drifted leg; this surfaces it at
    # the roll-up alongside the extract-recipe walk gaps).
    gaps = list(gaps) + _stale_identity_gaps(constituents)

    # Program roll-up: the most severe constituent classification (the fold idiom).
    overall: str
    if constituents:
        overall = max(
            (c.classification for c in constituents), key=lambda cls: _PROGRAM_SEVERITY[cls]
        )
    else:
        # No constituents resolved (e.g. an old-shape table the extract-recipe walk
        # could not link): nothing reproduced, disclosed via gaps — not an error.
        overall = CLASS_NONE

    # Materialize the write-once signed manifest (identity only — verdicts stay out).
    body = _program_manifest_body(
        seed_kind=seed_kind,
        seed_ref=seed_ref,
        recipe_signature=recipe_signature,
        resolved=resolved,
        fingerprints=fingerprints,
    )
    program_signature = manifest_signature(body)
    manifest_path, manifest_delta = _write_program_manifest(experiment_dir, body, program_signature)

    reason = _render_reason(overall, reproduced, total, counts, gaps)

    result_fields: dict[str, Any] = {
        "program_schema_version": PROGRAM_SCHEMA_VERSION,
        "seed_kind": seed_kind,
        "seed_ref": seed_ref,
        "recipe_signature": recipe_signature,
        "program_signature": program_signature,
        "resolved_run_ids": resolved,
        "constituents": [c.model_dump(mode="json") for c in constituents],
        "reproduced_count": reproduced,
        "total": total,
        "overall": overall,
        "needs_decision": overall != CLASS_REPRODUCED,
        "reason": reason,
        "gaps": gaps,
        "manifest_path": manifest_path,
        "manifest_delta": manifest_delta,
    }
    result_fields["markdown"] = _render_markdown(result_fields)
    return ProgramVerifyResult.model_validate(result_fields)
