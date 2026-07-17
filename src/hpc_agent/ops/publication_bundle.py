"""Export-bundle — the publication bundle a scientist ships with a paper.

The PUBLICATION-time composition layer over :mod:`hpc_agent.ops.export_dossier`'s
SEALING layer (``docs/design/publication-bundle.md``). ``export-dossier`` gathers
a run's concrete on-disk source stores into one integrity-sealed ``.zip`` — and,
since BR-4, the derived clean-reproduction recipe — via the ONE gather
``export_dossier`` defines
(:func:`~hpc_agent.ops.export_dossier.compute_dossier_signature`). This SIBLING
verb COMPOSES that same sealed evidence (never a second store walk, the
``export-attestations`` precedent) and adds, under ONE top-level seal:

* the **signed provenance manifest** (``provenance_manifest`` v3 — the wheel-sha +
  resolved-environment lock, signature-attested INSIDE the sealed artifact rather
  than merely referenced), which the dossier does not seal today;
* the **cite-check report** over the MANUSCRIPT — the per-number audit of every
  cited digit against the sealed ``aggregated_metrics`` values (the ONE member
  sourced from a new input; it closes the last-mile transcription link). Absent a
  manuscript, it is disclose-skipped (R-B2, disclose-not-gate);
* the **in-toto/DSSE attestations** member (the ``export-attestations`` projection
  of the SAME dossier signature — the stock-tooling offline-verify story);
* the top-level **``VERIFY`` manifest** — a CODE-emitted per-link
  MECHANICAL/DISCLOSED/ABSENT classification (thesis §3), the union-of-disclosures
  ledger, the member pointers, and the offline-verify recipe, all sealed under one
  ``bundle_sha256`` (the ONE signable digest,
  :func:`~hpc_agent.ops.provenance_manifest.manifest_signature`, one level up).

Boundary posture (``docs/internals/engineering-principles.md`` Q1, "substrate,
not semantics", extended). It is a ``mutate`` verb with a SINGLE local write and
NO SSH (the ``export-dossier`` decorator, mirrored). It COMPOSES the shipped verbs
— it reinvents nothing. It never overclaims: the ``VERIFY`` verdict is a code
template filled by the classification, never LLM-composed (R-B4), and it inherits
EVERY disclosure honestly (an opted-out data run classifies the data link
DISCLOSED, never MECHANICAL). The **no-parse boundary holds**: this module copies
member bytes verbatim and NEVER ``json``-parses a sealed member's content — the
cite-check report, the signed manifest, and the attestations are FRAMEWORK-derived
records, serialized once (``json.dumps``, allowed) and sealed as opaque-to-the-
sealer bytes. There is NO ``json.load`` / ``json.loads`` anywhere here.

R-B3 — **the cite-check report is a BUNDLE member under this module's own closed
:data:`BUNDLE_MEMBERS` vocabulary, NOT a ``DOSSIER_SOURCES`` noun**: adding a
DOSSIER noun would fire the dossier boundary pin AND force the
``export-attestations`` ``PREDICATE_TYPES`` pair-edit, for a member that is a
*publication* concern, not a *run* store. The bundle carries its own vocabulary,
disjoint from ``DOSSIER_SOURCES``; the ``export-dossier`` contract is untouched.

This file lives at the ``ops/`` *role root* (sibling to ``export_dossier.py`` /
``extract_recipe.py`` / ``cite_check.py``) because it reads across subjects; the
subject-imports lint short-circuits for role-root files.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._build_info import full_version
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.publication_bundle import ExportBundleResult, ExportBundleSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.io import atomic_replace_path
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.export_dossier import (
    DOSSIER_DIRNAME,
    DossierSignature,
    compute_dossier_signature,
)
from hpc_agent.ops.provenance_manifest import build_provenance_manifest, manifest_signature

__all__ = [
    "BUNDLE_MEMBERS",
    "BUNDLE_SCHEMA_VERSION",
    "LINK_ABSENT",
    "LINK_DISCLOSED",
    "LINK_MECHANICAL",
    "export_bundle",
]

# The bundle's own artifact schema version (R-B5). A NEW artifact with its own
# version; no existing schema breaks (dossier v2, manifest v3, cite-check,
# extract-recipe all unchanged). Bump when a consumer (the offline verifier, a
# reviewer) would need to branch on the emitted shape.
BUNDLE_SCHEMA_VERSION: int = 1

# ── the closed BUNDLE-MEMBER vocabulary (R-B3) ──────────────────────────────
# A bundle entry is typed by which MEMBER of the composition it is — a closed set
# this module owns, DISJOINT from ``export_dossier.DOSSIER_SOURCES`` (the dossier
# store nouns). The disjointness is the load-bearing R-B3 property: the cite-check
# report (and the signed manifest, the attestations, the verify render) are
# PUBLICATION concerns, not run stores, so they never touch the dossier vocabulary
# — no dossier-boundary blast radius, no ``export-attestations`` pair-edit. The
# derived recipe travels sealed INSIDE ``dossier-evidence`` (it is already a
# dossier store, BR-4) and is pointed at from the manifest's ``members`` block, so
# the two vocabularies stay disjoint. ``tests/contracts/
# test_publication_bundle_boundary.py`` pins this set by equality AND pins the
# disjointness from ``DOSSIER_SOURCES``.
BUNDLE_MEMBERS: frozenset[str] = frozenset(
    {
        "dossier-evidence",  # every sealed dossier store (incl. the recipe), byte-verbatim
        "provenance-manifest",  # the signed provenance manifest (v3), sealed member
        "cite-check-report",  # the manuscript number → sealed-table audit (manuscript only)
        "attestations",  # the in-toto/DSSE portability stream (one envelope per dossier entry)
        "verify",  # the top-level VERIFY manifest human render (VERIFY.md)
    }
)

# ── the per-link reproducibility classification vocabulary (thesis §3) ──────
# A reproducibility link is MECHANICAL (a stranger confirms with sha recompute /
# signature verify alone), DISCLOSED (an honest gap the chain inherits — opt-in
# data, weak env, an uncitable number), or ABSENT (the link was not exercised —
# no manuscript). The verdict NEVER stamps "reproducible"; a DISCLOSED/ABSENT link
# is disclosed, never laundered into a proof.
LINK_MECHANICAL = "MECHANICAL"
LINK_DISCLOSED = "DISCLOSED"
LINK_ABSENT = "ABSENT"

# The fixed archive paths the added members are sealed under (deterministic so the
# members sort + address stably across re-gathers). The dossier stores are carried
# under the ``dossier/`` prefix at their original archive paths (byte-verbatim, so
# their sha survives — the seal is over content, not path).
_DOSSIER_PREFIX = "dossier/"
_PROVENANCE_MEMBER_PATH = "provenance-manifest.json"
_CITE_CHECK_MEMBER_PATH = "cite-check-report.json"
_ATTESTATIONS_MEMBER_PATH = "attestations.jsonl"
_VERIFY_RENDER_PATH = "VERIFY.md"
# The top-level self-attesting seal — like the dossier's ``manifest.json``, it is
# NOT itself a sealed entry (a manifest cannot hash itself).
_VERIFY_MANIFEST_PATH = "VERIFY.json"


def _sha256_hex(data: bytes) -> str:
    """Return the 64-char hex SHA-256 of *data* — one member's integrity fingerprint."""
    return hashlib.sha256(data).hexdigest()


def _seal_member(
    member: str,
    archive_path: str,
    data: bytes,
    *,
    write_map: dict[str, bytes],
    entries: list[dict[str, Any]],
) -> None:
    """Register *data* for the zip under *archive_path* and append its entry.

    The one place bytes become a sealed bundle member: ``data`` → sha256 → a
    ``{member, path, sha256, bytes}`` provenance record (the ``member`` key is a
    :data:`BUNDLE_MEMBERS` noun — the bundle analogue of the dossier's ``source``).
    Content is never decoded or parsed; a member round-trips byte-identical.
    """
    write_map[archive_path] = data
    entries.append(
        {
            "member": member,
            "path": archive_path,
            "sha256": _sha256_hex(data),
            "bytes": len(data),
        }
    )


def _resolve_bundle_context(
    experiment_dir: Path, spec: ExportBundleSpec
) -> tuple[str, str, str, str | None]:
    """Resolve the seed to ``(seed_kind, seed_ref, primary_run_id, campaign_id)``.

    Reuses ``extract_recipe._resolve_seed`` verbatim (the exactly-one-seed
    contract — it raises :class:`errors.SpecInvalid` on zero / two / three seeds).
    The dossier gather is run-scoped, so a PRIMARY run seeds it: the run itself for
    a run seed, the head contributing run for a campaign / aggregate seed. The
    campaign for the signed provenance manifest is the campaign seed, or the
    primary run's sidecar ``campaign_id`` when a run / aggregate seeded the bundle
    (``None`` → the provenance member is disclose-skipped).

    Raises :class:`errors.SpecInvalid` when no primary run resolves (a campaign /
    aggregate with no contributing run — nothing to seal a dossier for), mirroring
    the dossier's missing-run guard.
    """
    from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput
    from hpc_agent.ops.extract_recipe import _resolve_seed
    from hpc_agent.state.runs import read_run_sidecar_or_empty

    recipe_input = ExtractRecipeInput(
        run_id=spec.run_id,
        campaign_id=spec.campaign_id,
        aggregate_path=spec.aggregate_path,
    )
    seed_kind, seed_ref, candidates, _contributing, _artifact_opaque, _gaps = _resolve_seed(
        experiment_dir, recipe_input
    )

    if seed_kind == "run":
        primary: str | None = seed_ref
    else:
        primary = candidates[0] if candidates else None
    if not primary:
        raise errors.SpecInvalid(
            f"export-bundle: seed {seed_kind} {seed_ref!r} resolved no contributing "
            "run — there is nothing to seal a dossier for."
        )

    if seed_kind == "campaign":
        campaign_id: str | None = seed_ref
    else:
        sidecar = read_run_sidecar_or_empty(experiment_dir, primary)
        cid = sidecar.get("campaign_id")
        campaign_id = cid if isinstance(cid, str) and cid else None

    return seed_kind, seed_ref, primary, campaign_id


def _safe_recipe(experiment_dir: Path, spec: ExportBundleSpec) -> dict[str, Any] | None:
    """Re-derive the clean-reproduction recipe for the seed, or ``None`` on failure.

    A PURE re-derivation of the same recipe the dossier already seals as its
    ``recipe`` member (``extract_recipe`` is a deterministic projection, no
    wall-clock) — used here only to CLASSIFY the reproducibility links (the recipe
    gaps → the minimal-set link; the per-run ``data_sha`` / ``env_lock_sha`` →
    the data / environment links). Disclose-not-gate (the ``_gather_recipe``
    posture): any extraction failure returns ``None`` and the classification
    degrades to DISCLOSED — it never blocks the bundle.
    """
    from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput
    from hpc_agent.ops.extract_recipe import extract_recipe

    recipe_input = ExtractRecipeInput(
        run_id=spec.run_id,
        campaign_id=spec.campaign_id,
        aggregate_path=spec.aggregate_path,
    )
    try:
        return extract_recipe(experiment_dir, spec=recipe_input)
    except Exception:  # noqa: BLE001 — disclose-not-gate: a recipe-walk failure degrades disclosed
        return None


def _manuscript_present(spec: ExportBundleSpec) -> bool:
    """Whether a manuscript was supplied — at most one of text / path (R-B2).

    Raises :class:`errors.SpecInvalid` when BOTH sources are given (a spec error,
    like ``cite-check``); NEITHER is legal (disclose-not-gate — the report member
    is skipped and the transcription link is classified ABSENT).
    """
    has_text = bool(spec.manuscript_text)
    has_path = bool((spec.manuscript_path or "").strip())
    if has_text and has_path:
        raise errors.SpecInvalid(
            "export-bundle: at most one manuscript source — manuscript_text XOR "
            "manuscript_path (or neither, which disclose-skips the cite-check report)."
        )
    return has_text or has_path


def _cite_report(
    experiment_dir: Path, spec: ExportBundleSpec, seed_kind: str, seed_ref: str
) -> dict[str, Any]:
    """Run ``cite-check`` over the manuscript against the SAME seed's sealed table.

    Delegates to the shipped ``cite_check`` verb (the report member is composed,
    never re-implemented). The seed is threaded through unchanged so the citing
    authority is the same sealed ``aggregated_metrics`` the recipe walks.
    """
    from hpc_agent._wire.queries.cite_check import CiteCheckInput
    from hpc_agent.ops.cite_check import cite_check

    seed_field = {"run": "run_id", "campaign": "campaign_id", "aggregate": "aggregate_path"}[
        seed_kind
    ]
    seed_kwargs: dict[str, str] = {seed_field: seed_ref}
    cite_input = CiteCheckInput(
        manuscript_text=spec.manuscript_text,
        manuscript_path=spec.manuscript_path,
        **seed_kwargs,
    )
    return cite_check(experiment_dir, spec=cite_input)


def _signed_provenance(experiment_dir: Path, campaign_id: str) -> dict[str, Any]:
    """Build the signed provenance manifest IN MEMORY (never a disk write here).

    Reuses ``build_provenance_manifest`` + the ONE signable digest
    (``manifest_signature``) exactly as ``write_provenance_manifest`` does, minus
    the ``.hpc/provenance`` write — the bundle SEALS the signed manifest as a
    member, it does not mint a standalone provenance file. Self-attesting: a reader
    strips ``signature`` and re-hashes the body (``verify_provenance_manifest``).
    """
    manifest = build_provenance_manifest(experiment_dir, campaign_id)
    manifest_with_sig = dict(manifest)
    manifest_with_sig["signature"] = manifest_signature(manifest)
    return manifest_with_sig


def _attestations_bytes(sig: DossierSignature) -> bytes:
    """Project the ONE dossier signature into the in-toto/DSSE JSONL member.

    Reuses ``export_attestations``'s projection (``_statement`` + ``_dsse_envelope``
    — the stores are NEVER re-walked; the sealed bytes come from ``sig.write_map``,
    exactly as ``export_attestations`` consumes them). One unsigned DSSE envelope
    per sealed dossier entry, JSONL — the stock-tooling offline-verify layer.
    """
    from hpc_agent.ops.export_attestations import _dsse_envelope, _statement

    lines = [
        json.dumps(_dsse_envelope(_statement(entry, sig.write_map[entry["path"]])), sort_keys=True)
        for entry in sig.entries
    ]
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _env_lock_summary(prov: dict[str, Any] | None) -> str:
    """A compact env-lock-status summary over a signed provenance manifest.

    Surfaces the signed ``env_lock_status`` verbatim across the manifest's runs
    (the design's "surfacing env_lock_status verbatim"). No manifest → says so.
    IDENTITY / COUNTING only — never a metric.
    """
    if not isinstance(prov, dict):
        return "no signed provenance manifest sealed (no campaign resolved for the seed)"
    statuses = sorted(
        {
            str(r.get("env_lock_status"))
            for r in (prov.get("runs") or [])
            if isinstance(r, dict) and r.get("env_lock_status") is not None
        }
    )
    if not statuses:
        return (
            "signed provenance manifest sealed; no env-lock capture recorded (env_lock_status null)"
        )
    return f"signed provenance manifest sealed; env_lock_status across runs: {', '.join(statuses)}"


def _classify_links(
    recipe: dict[str, Any] | None,
    cite: dict[str, Any] | None,
    prov: dict[str, Any] | None,
    *,
    manuscript_present: bool,
) -> list[dict[str, Any]]:
    """The per-link MECHANICAL/DISCLOSED/ABSENT classification (thesis §3).

    Five links in a fixed order — code / data / environment / minimal-set /
    transcription. The classification is CODE, filled from the recipe fingerprints
    + gaps and the cite-check buckets. It inherits every disclosure honestly: an
    opted-out data run (a ``null`` ``data_sha``) classifies the data link DISCLOSED,
    NEVER MECHANICAL; environment is DISCLOSED in v1 (the full-environment identity
    remains weak, ``env_hash`` never gated); a manuscript with an uncitable number
    classifies transcription DISCLOSED (never a failure); no manuscript → ABSENT.
    """
    runs = list((recipe or {}).get("runs") or [])
    recipe_gaps = list((recipe or {}).get("gaps") or [])

    # code — always MECHANICAL: cmd_sha + tasks_py_sha are sealed per contributing run.
    code = {
        "link": "code",
        "status": LINK_MECHANICAL,
        "detail": "cmd_sha + tasks_py_sha sealed per contributing run",
    }

    # data — MECHANICAL only when every contributing run declared input data
    # (data_sha present); DISCLOSED (opt-in) otherwise. An empty run-set is
    # DISCLOSED (nothing to attest data for), the honest direction.
    data_declared = bool(runs) and all(r.get("data_sha") for r in runs)
    data = {
        "link": "data",
        "status": LINK_MECHANICAL if data_declared else LINK_DISCLOSED,
        "detail": (
            "all contributing runs declared input data (data_sha present)"
            if data_declared
            else "one or more contributing runs did not declare input data (data_sha null) — "
            "data-drift attribution is opt-in and disclosed"
        ),
    }

    # environment — DISCLOSED in v1: env_lock_sha is captured + signed where present,
    # but the full-environment identity remains weak and env_hash is never gated.
    environment = {
        "link": "environment",
        "status": LINK_DISCLOSED,
        "detail": (
            "resolved-environment lock captured + signed where present; full-environment "
            f"identity remains weak (env_hash never gated). {_env_lock_summary(prov)}"
        ),
    }

    # minimal-set — MECHANICAL when the recipe resolved and disclosed no gaps;
    # DISCLOSED when the recipe carries gaps (table-run-set-link-absent,
    # operator-bypass, pack-csv-opaque) or could not be derived at all.
    if recipe is None:
        minimal = {
            "link": "minimal-set",
            "status": LINK_DISCLOSED,
            "detail": "the clean-reproduction recipe could not be derived (disclosed)",
        }
    elif recipe_gaps:
        codes = ", ".join(sorted({str(g.get("code")) for g in recipe_gaps if g.get("code")}))
        minimal = {
            "link": "minimal-set",
            "status": LINK_DISCLOSED,
            "detail": (
                f"minimal run-set signature-verified with {len(recipe_gaps)} disclosed "
                f"gap(s): {codes}"
            ),
        }
    else:
        minimal = {
            "link": "minimal-set",
            "status": LINK_MECHANICAL,
            "detail": "minimal run-set signature-verified with no disclosed gaps",
        }

    # transcription — ABSENT with no manuscript; MECHANICAL when every cited number
    # matched a sealed value; DISCLOSED when an uncitable number was disclosed.
    if not manuscript_present or cite is None:
        transcription = {
            "link": "transcription",
            "status": LINK_ABSENT,
            "detail": "no manuscript supplied — the number-to-paper transcription was not audited",
        }
    else:
        findings = list(cite.get("findings") or [])
        n_uncitable = sum(1 for f in findings if f.get("kind") == "uncitable")
        n_matched = sum(1 for f in findings if f.get("kind") == "matched")
        if cite.get("clean"):
            transcription = {
                "link": "transcription",
                "status": LINK_MECHANICAL,
                "detail": (
                    f"{n_matched} cited number(s) all equal a sealed value (faithful-render "
                    "tolerance); 0 uncitable"
                ),
            }
        else:
            transcription = {
                "link": "transcription",
                "status": LINK_DISCLOSED,
                "detail": (
                    f"{n_uncitable} cited number(s) have no sealed backing (uncitable) — "
                    "disclosed as context, never a failure"
                ),
            }

    return [code, data, environment, minimal, transcription]


def _disclosures_ledger(
    sig: DossierSignature,
    recipe: dict[str, Any] | None,
    cite: dict[str, Any] | None,
    *,
    manuscript_present: bool,
    campaign_id: str | None,
) -> list[dict[str, Any]]:
    """The union of EVERY disclosed gap across the whole chain.

    Dossier absent-stores + recipe gaps + the cite-check uncitable/skip + the
    provenance-manifest skip. Each item names its origin + a disclosed detail; the
    bundle can be no more honest than its weakest link, and this ledger says so.
    Disclosed, never fatal.
    """
    ledger: list[dict[str, Any]] = []

    for gap in sig.gaps:
        item: dict[str, Any] = {"origin": "dossier"}
        item.update(gap)  # {source, run_id, note}
        ledger.append(item)

    if recipe is None:
        ledger.append(
            {
                "origin": "recipe",
                "code": "recipe-underivable",
                "detail": "the clean-reproduction recipe could not be derived for this seed",
            }
        )
    else:
        for gap in recipe.get("gaps") or []:
            item = {"origin": "recipe"}
            item.update(gap)  # {code, detail}
            ledger.append(item)

    if not manuscript_present:
        ledger.append(
            {
                "origin": "cite-check",
                "code": "cite-check-skipped",
                "detail": "no manuscript supplied — the number-to-paper transcription link "
                "was not audited (disclose-not-gate)",
            }
        )
    elif cite is not None and not cite.get("clean"):
        n_uncitable = sum(1 for f in (cite.get("findings") or []) if f.get("kind") == "uncitable")
        ledger.append(
            {
                "origin": "cite-check",
                "code": "uncitable-numbers",
                "detail": f"{n_uncitable} manuscript number(s) have no sealed backing — "
                "disclosed, not a failure",
            }
        )

    if campaign_id is None:
        ledger.append(
            {
                "origin": "provenance-manifest",
                "code": "provenance-manifest-skipped",
                "detail": "no campaign_id resolved for the seed — the signed provenance "
                "manifest member was not sealed",
            }
        )

    return ledger


def _bundle_verdict(links: list[dict[str, Any]]) -> str:
    """The CODE-emitted honest verdict (R-B4) — a fixed template + the classification.

    The ``CLAIM_CONSISTENT_SENTENCE`` precedent: trusted code, never LLM-composed.
    It enumerates the per-link statuses and scopes the thesis §5 claim to THIS
    bundle. It NEVER stamps a bare "reproducible" — the bundle is a proof-of-what-
    is-mechanical + an honest ledger-of-what-is-disclosed, never a certificate.
    """
    parts = "; ".join(f"{link['link']} {link['status']}" for link in links)
    return (
        "Every citable number in this bundle is reducer-computed and byte-sealed — never "
        "computed or silently altered by a language model. The minimal run-set is "
        f"signature-verified and gap-disclosing. Per-link classification: {parts}. "
        "The chain is mechanical for code, mechanical for data and environment where the "
        "scientist opted in and disclosed where not, and the number-to-paper transcription "
        "is audited to the matched/uncitable split. The one unbound link is a human typing "
        "the sealed number into the paper. This is an honest ledger of what is mechanical "
        "and what is disclosed, not a reproducibility certificate."
    )


def _offline_verify_block() -> dict[str, Any]:
    """The offline-verify recipe a stranger recomputes with stdlib only (Layer 1).

    Documents the EXACT canonicalization ``bundle_sha256`` is sealed under — the
    ONE signable digest (``manifest_signature``): ``json.dumps(entries,
    sort_keys=True, separators=(",",":"))`` (Python default ``ensure_ascii=True``;
    the entries are ASCII paths / shas / ints, so it is moot in practice), UTF-8,
    SHA-256 lowercase hex. Written IN the manifest so a non-Python reimplementation
    is possible.
    """
    return {
        "seal_algorithm": "sha256",
        "digest_encoding": "lowercase hex",
        "canonicalization": (
            'json.dumps(entries, sort_keys=True, separators=(",",":")) over the '
            "path-sorted VERIFY.json 'entries' list, UTF-8 encoded, then SHA-256 "
            "(the shared manifest_signature definition)"
        ),
        "steps": [
            "unzip the bundle",
            "for each entry in VERIFY.json 'entries': sha256(bytes at 'path') == entry['sha256']",
            "recompute bundle_sha256 = sha256(canonical('entries')) and compare to "
            "VERIFY.json 'bundle_sha256'",
            "verify the signed provenance-manifest.json member: strip 'signature', re-hash the "
            "body (sorted-keys, tight separators), compare to 'signature'",
            "optionally: parse attestations.jsonl with stock in-toto/DSSE tooling and compare "
            "each subject digest to the matching dossier entry",
        ],
    }


@primitive(
    name="export-bundle",
    verb="mutate",
    side_effects=[
        SideEffect(
            "file_write",
            "<output_path> (default <experiment>/_dossier/<seed>.bundle.zip)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    # The bundle is derived state, recomputed from disk on every call: replaying
    # with the same seed re-seals the same members + bundle_sha256 (generated_at is
    # excluded from the pre-image), overwriting the archive at the resolved path.
    idempotency_key="seed",
    cli=CliShape(
        help=(
            "Assemble the publication bundle — one offline-verifiable .zip a "
            "scientist ships with a paper: the sealed dossier evidence + the "
            "minimal recipe (the ONE dossier gather), the signed provenance "
            "manifest, a cite-check audit of the manuscript's numbers against the "
            "sealed table, the in-toto/DSSE attestations, and a top-level VERIFY "
            "manifest classifying each reproducibility link MECHANICAL/DISCLOSED/"
            "ABSENT with the union-of-disclosures ledger, all under one seal. "
            "Composes the shipped verbs; discloses, never gates; never overclaims "
            "'reproducible'. Seed = exactly one of --run-id / --campaign-id / "
            "--aggregate-path; manuscript = optionally one of --manuscript-text / "
            "--manuscript-path (absent → disclose-skipped). Pure local reads + one "
            "local write, no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ExportBundleSpec,
        requires_ssh=False,
        schema_ref=SchemaRef(input="export_bundle"),
    ),
    agent_facing=True,
)
def export_bundle(*, experiment_dir: Path, spec: ExportBundleSpec) -> ExportBundleResult:
    """Assemble the publication bundle for a seed (optionally a manuscript) and seal it.

    Composes the ONE dossier gather (evidence + the derived recipe), the signed
    provenance manifest, the cite-check report over the manuscript (when supplied),
    and the in-toto/DSSE attestations into one ``.zip`` under a top-level ``VERIFY``
    manifest — the per-link MECHANICAL/DISCLOSED/ABSENT classification + the union
    disclosure ledger + the code-emitted verdict, sealed under one
    ``bundle_sha256``. Inherits every disclosure honestly; never overclaims.

    Raises :class:`errors.SpecInvalid` on a bad seed (not exactly one), both
    manuscript sources at once, or a seed that resolves no run to seal a dossier
    for. Absent individual stores / an absent manuscript / a missing signed
    manifest are DISCLOSED, never fatal. An existing bundle at the target is
    overwritten (idempotent replay). Default output:
    ``<experiment>/_dossier/<seed>.bundle.zip``.
    """
    experiment_dir = Path(experiment_dir)

    # 1. Resolve the seed → the primary run (the dossier gather is run-scoped) and
    #    the campaign (the signed provenance manifest is campaign-scoped).
    seed_kind, seed_ref, primary_run_id, campaign_id = _resolve_bundle_context(experiment_dir, spec)

    # 2. The ONE dossier gather — evidence + the derived recipe member, sealed as
    #    raw bytes (the missing-run guard lives in this seam). Never a second walk.
    sig = compute_dossier_signature(
        experiment_dir, primary_run_id, include_lineage=spec.include_lineage
    )

    # 3. The classification inputs — a pure re-derivation of the recipe (for the
    #    minimal-set / data / environment links) + the signed provenance manifest
    #    + the cite-check report over the manuscript (when one is supplied).
    recipe = _safe_recipe(experiment_dir, spec)
    manuscript_present = _manuscript_present(spec)
    cite = _cite_report(experiment_dir, spec, seed_kind, seed_ref) if manuscript_present else None
    prov = _signed_provenance(experiment_dir, campaign_id) if campaign_id else None

    # 4. The honest classification, disclosure ledger, and CODE-emitted verdict.
    links = _classify_links(recipe, cite, prov, manuscript_present=manuscript_present)
    disclosures = _disclosures_ledger(
        sig,
        recipe,
        cite,
        manuscript_present=manuscript_present,
        campaign_id=campaign_id,
    )
    verdict = _bundle_verdict(links)

    # 5. Seal every member under one write_map + entries. Dossier stores ride under
    #    the ``dossier/`` prefix (bytes verbatim; sha survives). The added members
    #    are FRAMEWORK-derived records: serialized once (json.dumps — allowed) and
    #    sealed as opaque bytes (never re-parsed — the no-parse boundary).
    write_map: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []

    recipe_entry = next((e for e in sig.entries if e.get("source") == "recipe"), None)
    for entry in sig.entries:
        _seal_member(
            "dossier-evidence",
            f"{_DOSSIER_PREFIX}{entry['path']}",
            sig.write_map[entry["path"]],
            write_map=write_map,
            entries=entries,
        )

    if prov is not None:
        _seal_member(
            "provenance-manifest",
            _PROVENANCE_MEMBER_PATH,
            json.dumps(prov, sort_keys=True, indent=2).encode("utf-8"),
            write_map=write_map,
            entries=entries,
        )

    if cite is not None:
        _seal_member(
            "cite-check-report",
            _CITE_CHECK_MEMBER_PATH,
            json.dumps(cite, sort_keys=True, indent=2).encode("utf-8"),
            write_map=write_map,
            entries=entries,
        )

    _seal_member(
        "attestations",
        _ATTESTATIONS_MEMBER_PATH,
        _attestations_bytes(sig),
        write_map=write_map,
        entries=entries,
    )

    # The member pointers — where each part of the bundle lives (the recipe travels
    # sealed INSIDE dossier-evidence, pointed at here so the disjoint vocabulary
    # still surfaces it).
    members_block: dict[str, Any] = {
        "dossier_evidence_prefix": _DOSSIER_PREFIX,
        "recipe": (f"{_DOSSIER_PREFIX}{recipe_entry['path']}" if recipe_entry else None),
        "provenance_manifest": _PROVENANCE_MEMBER_PATH if prov is not None else None,
        "cite_check_report": _CITE_CHECK_MEMBER_PATH if cite is not None else None,
        "attestations": _ATTESTATIONS_MEMBER_PATH,
        "verify_render": _VERIFY_RENDER_PATH,
        "verify_manifest": _VERIFY_MANIFEST_PATH,
    }

    # The human render is a SEALED member (typed ``verify``), computed from the
    # PRE-SEAL view only — never bundle_sha256 (which hashes this render among the
    # members). Import kept local so the render module is off the hot import path.
    from hpc_agent.ops.bundle_render import render_verify

    pre_seal_view: dict[str, Any] = {
        "seed": {"kind": seed_kind, "ref": seed_ref},
        "primary_run_id": primary_run_id,
        "links": links,
        "verdict": verdict,
        "disclosures": disclosures,
        "members": members_block,
    }
    _seal_member(
        "verify",
        _VERIFY_RENDER_PATH,
        render_verify(pre_seal_view).encode("utf-8"),
        write_map=write_map,
        entries=entries,
    )

    # 6. Path-sort the entries (and the write order) so a member hashes identically
    #    regardless of gather order; the ONE top-level seal over the entries list
    #    ONLY (generated_at / tool_version excluded from the pre-image, the dossier
    #    discipline one level up), reusing manifest_signature verbatim.
    entries.sort(key=lambda e: e["path"])
    bundle_sha256 = manifest_signature(entries)  # type: ignore[arg-type]

    generated_at = utcnow_iso()
    verify_manifest: dict[str, Any] = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "generated_at": generated_at,  # envelope — NOT in the seal pre-image
        "tool_version": full_version(),  # envelope — NOT in the seal pre-image
        "seed": {"kind": seed_kind, "ref": seed_ref},
        "primary_run_id": primary_run_id,
        "runs": sig.run_projections,
        "members": members_block,
        "entries": entries,
        "links": links,
        "disclosures": disclosures,
        "verdict": verdict,
        "verdict_meta": {"code_emitted": True, "claims_reproducible": False},
        "offline_verify": _offline_verify_block(),
        "bundle_sha256": bundle_sha256,
    }

    # 7. Resolve the output path and overwrite any existing bundle (idempotent
    #    replay). Build on a temp sibling + atomic swap (the dossier discipline):
    #    ZipFile(path, "w") TRUNCATES the previously-sealed archive the instant it
    #    opens, so a crash mid-write would destroy it.
    if spec.output_path:
        bundle_path = Path(spec.output_path)
    else:
        safe_seed = seed_ref.replace("/", "_").replace("\\", "_")
        bundle_path = experiment_dir / DOSSIER_DIRNAME / f"{safe_seed}.bundle.zip"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    with (
        atomic_replace_path(bundle_path) as tmp_archive,
        zipfile.ZipFile(tmp_archive, "w", zipfile.ZIP_DEFLATED) as zf,
    ):
        for path in sorted(write_map):
            zf.writestr(path, write_map[path])
        # VERIFY.json is the top-level self-attesting seal — NOT itself an entry.
        # json.dumps SERIALIZES a framework record (allowed); the ban is on
        # json.load/loads reading a sealed member back into structure.
        zf.writestr(_VERIFY_MANIFEST_PATH, json.dumps(verify_manifest, sort_keys=True, indent=2))

    return ExportBundleResult(
        bundle_path=str(bundle_path),
        seed_kind=seed_kind,  # type: ignore[arg-type]
        seed_ref=seed_ref,
        primary_run_id=primary_run_id,
        run_ids=list(sig.run_ids),
        bundle_sha256=bundle_sha256,
        member_count=len(entries),
        manuscript_present=manuscript_present,
        verdict=verdict,
        disclosures=disclosures,
        verify_manifest=verify_manifest,
    )
