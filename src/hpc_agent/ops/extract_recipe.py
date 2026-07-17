"""``extract-recipe`` — the artifact → minimal-run-set → runnable-recipe walk.

A read-only ``query`` primitive (clean-reproduction extraction, proposal #1).
Given a citable artifact reference — a ``run_id``, a ``campaign_id``, or a path
to a reduced-metrics artifact — it walks BACK to the MINIMAL contributing
run-set and emits one deterministic recipe: the run-set with canary siblings,
superseded lineage members, and dead-end runs mechanically EXCLUDED (each
exclusion disclosed + counted); each contributing run's full provenance
fingerprint including ``hpc_agent_version`` (the wheel the directive names); a
recipe-specific signature over ONLY the minimal set; the runnable re-derivation
steps; the receipts chain; and every G4 gap it cannot bridge DISCLOSED, never
papered over.

It COMPOSES the shipped walks — it does not reinvent them: the reduce-time
``contributing_run_ids`` provenance (Task 1, ``ops/aggregate_flow``), the
supersession ``lineage_chain`` (``state/scopes``), the canary-family suffix
definition (``sibling_run_ids`` / ``canary_parent_of``,
``ops/monitor/reconcile``), the harvest-receipt ledger
(``harvest_receipt_exists``, ``ops/monitor/harvest_guard``), the campaign run /
sidecar finders (``state/index`` + the reduce history), and the signable
``manifest_signature`` (``ops/provenance_manifest``). Since R3 (v2) the wheel sha
``hpc_agent_version`` — and since U-ENV1 (v3) the resolved-environment lock
``env_lock_sha`` — are SIGNED fields of the provenance manifest; when a written,
signature-verified manifest carries a contributing run, this verb PREFERS each
signed value over the sidecar projection and discloses which source it used
(``hpc_agent_version_source`` / ``env_lock_sha_source`` per run — ``signed-manifest``
vs ``sidecar``), per the memo's receipts-chain honesty. Absent a signed source, the
sidecar projection stands.

It is a PURE projection (the ``run_story`` / ``trace`` posture): no SSH, no
scheduler, no write, no store. Derived state recomputed from the on-disk records
on every call, so it can never drift from a second source of truth. It never
interprets what any record MEANS — every fact is IDENTITY (which run, at which
sha), ORDERING (the re-derivation steps), or COUNTING (exclusion / receipt
counts) over opaque records. It never names a metric, never picks a "best" run,
never concludes; a pack ``*.csv`` is an OPAQUE citation whose content is NEVER
parsed (R2 — the dossier no-parse boundary). NOT MCP-curated: like
``trace`` / ``provenance-manifest`` / ``run-story`` it is an operator/reviewer
projection, and the curated catalog is a deliberate human-amplification allowlist
(MCP-is-projection ruling), so it is reachable via the CLI registry but not
advertised as a curated tool.

This file lives at the ``ops/`` *role root* (sibling to ``trace.py`` /
``run_story.py``) because it reads across subjects — the ``state`` sidecars +
lineage, the monitor's harvest ledger + canary vocabulary, the aggregate
provenance, and the campaign finders. The subject-imports lint short-circuits for
role-root files, so the cross-subject reads here are allowed by construction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.extract_recipe import ExtractRecipeInput, ExtractRecipeResult
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    pass

__all__ = ["extract_recipe", "recipe_citation_ref", "resolve_recipe_citation"]

# The evidence-memory ``recipe`` citation ref shape (``state/evidence.py::
# KIND_RECIPE``): ``"<seed_kind>:<seed_ref>"``, mirroring the ``attestation`` ref.
# Each seed kind maps to the ONE ``ExtractRecipeInput`` field it seeds.
_RECIPE_SEED_FIELD: dict[str, str] = {
    "run": "run_id",
    "campaign": "campaign_id",
    "aggregate": "aggregate_path",
}

# Bump when the emitted recipe shape changes in a way a consumer would branch on.
RECIPE_SCHEMA_VERSION: int = 1

# The fingerprint fields projected per contributing run — the identity legs the
# directive names (params/code/data/env/env-lock/wheel/cluster/profile). Two of
# them — ``hpc_agent_version`` (the wheel sha, R3) and ``env_lock_sha`` (the
# resolved-environment lock, U-ENV1/v3) — default to the sidecar projection but
# are PREFERRED from a verified provenance manifest when one signs them (the
# signed value beats a drifted sidecar); the per-run ``<field>_source`` discloses
# which for each. NO metric value is among them.
_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "cmd_sha",
    "tasks_py_sha",
    "data_sha",
    "data_manifest_sha",
    "env_hash",
    "env_lock_sha",
    "hpc_agent_version",
    "cluster",
    "profile",
)

# The fingerprint legs that are SIGNED provenance-manifest fields — each prefers
# its verified signed value over the sidecar projection, disclosing the source.
# ``hpc_agent_version`` joined the signed manifest in v2 (R3); ``env_lock_sha`` in
# v3 (U-ENV1). A manifest whose schema predates a field never signed it, so a
# too-old entry falls back to the sidecar (handled in :func:`_signed_field`).
_SIGNED_FINGERPRINT_FIELDS: tuple[str, ...] = ("hpc_agent_version", "env_lock_sha")


def _read_json(path: Path) -> Any:
    """Parse a JSON file, or None on any absence/read/parse error (never raises)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _aggregate_path_for_run(experiment_dir: Path, run_id: str) -> Path:
    """The canonical persisted aggregate for a run (the F-Q / Task-1 location)."""
    return experiment_dir / "_aggregated" / run_id / "metrics_aggregate.json"


def _safe_sidecar(experiment_dir: Path, run_id: str) -> dict[str, Any]:
    """A run's sidecar dict, or ``{}`` when none exists (absence is data)."""
    from hpc_agent.state.runs import read_run_sidecar

    try:
        return read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _run_exists(experiment_dir: Path, run_id: str) -> bool:
    """True when a run has EITHER a journal record OR a sidecar on disk."""
    from hpc_agent.state.journal import load_run

    if load_run(experiment_dir, run_id) is not None:
        return True
    return bool(_safe_sidecar(experiment_dir, run_id))


def _signed_field(
    experiment_dir: Path, run_id: str, sidecar: dict[str, Any], field: str
) -> tuple[bool, str | None]:
    """A run's *field* value AS SIGNED by a verified provenance manifest.

    The one signed-provenance lookup, generalized over the manifest's signed
    identity legs (``hpc_agent_version``, R3/v2; ``env_lock_sha``, U-ENV1/v3).
    Returns ``(signed?, value)``. ``signed`` is True ONLY when a written,
    signature-verified provenance manifest for the run's campaign carries this
    run WITH *field* present (a manifest whose schema predates *field* never
    signed it — the key is absent — so that entry is NOT a signed source and the
    caller falls back to the sidecar). ``value`` is the signed string, or
    ``None`` for a signed absent marker (a signed ``null``). Any absence — no
    campaign tag, no written manifest, an unverifiable manifest, the run missing
    — returns ``(False, None)``. Never raises.
    """
    campaign_id = sidecar.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id:
        return False, None
    from hpc_agent._kernel.contract.layout import RepoLayout
    from hpc_agent.ops.provenance_manifest import verify_provenance_manifest

    safe = campaign_id.replace("/", "_")
    path = RepoLayout(experiment_dir).hpc / "provenance" / f"{safe}.json"
    manifest = _read_json(path)
    if not isinstance(manifest, dict) or not verify_provenance_manifest(manifest):
        return False, None
    for entry in manifest.get("runs") or []:
        if isinstance(entry, dict) and entry.get("run_id") == run_id:
            if field not in entry:
                return False, None  # a manifest schema too old to sign this field
            value = entry.get(field)
            return True, (value if isinstance(value, str) and value else None)
    return False, None


def _fingerprint(experiment_dir: Path, run_id: str) -> dict[str, Any]:
    """Project one run's provenance fingerprint (incl. the wheel sha + env lock).

    Each signed leg (:data:`_SIGNED_FINGERPRINT_FIELDS` — ``hpc_agent_version``,
    ``env_lock_sha``) prefers a verified provenance manifest's SIGNED value over
    the sidecar projection, disclosing which via a per-field ``<field>_source``
    (``signed-manifest`` vs ``sidecar``).
    """
    sidecar = _safe_sidecar(experiment_dir, run_id)
    out: dict[str, Any] = {"run_id": run_id}
    for field in _FINGERPRINT_FIELDS:
        out[field] = sidecar.get(field)
    for field in _SIGNED_FINGERPRINT_FIELDS:
        signed, signed_value = _signed_field(experiment_dir, run_id, sidecar, field)
        if signed:
            out[field] = signed_value
            out[f"{field}_source"] = "signed-manifest"
        else:
            out[f"{field}_source"] = "sidecar"
    return out


def _resolve_seed(
    experiment_dir: Path, spec: ExtractRecipeInput
) -> tuple[str, str, list[str], bool, list[dict[str, str]]]:
    """Resolve the seed to ``(seed_kind, seed_ref, candidates, artifact_opaque, gaps)``.

    ``candidates`` is the UNfiltered contributing-run universe (exclusions run
    next). ``gaps`` collects the G4 breaks discovered at seed-resolution time
    (table→run-set absent, pack-csv opaque, operator-bypass).
    """
    gaps: list[dict[str, str]] = []
    cid = (spec.campaign_id or "").strip()
    rid = (spec.run_id or "").strip()
    apath = (spec.aggregate_path or "").strip()
    if sum(bool(x) for x in (cid, rid, apath)) != 1:
        raise errors.SpecInvalid(
            "extract-recipe requires exactly one seed: --run-id XOR --campaign-id "
            "XOR --aggregate-path"
        )

    if cid:
        # Campaign fallback: the whole campaign is the candidate universe; the
        # exclusions carve it down to the minimal set.
        from hpc_agent.execution.mapreduce.reduce.history import find_sidecars_by_campaign
        from hpc_agent.state.index import find_runs_by_campaign

        candidates: list[str] = [r.run_id for r in find_runs_by_campaign(experiment_dir, cid)]
        for sc in find_sidecars_by_campaign(experiment_dir, cid):
            sid = sc.get("run_id")
            if isinstance(sid, str) and sid and sid not in candidates:
                candidates.append(sid)
        return "campaign", cid, candidates, False, gaps

    if apath:
        p = Path(apath)
        if not p.is_file():
            raise errors.SpecInvalid(
                f"extract-recipe: aggregate_path {apath!r} does not exist — there is "
                "no artifact to walk back from."
            )
        if p.suffix.lower() != ".json":
            # R2: a pack *.csv (or any non-json) is an OPAQUE citation — its
            # content is NEVER parsed. Its provenance is its containing run's
            # (the parent dir under _aggregated/<run_id>/), disclosed as a gap.
            owner = p.parent.name
            gaps.append(
                {
                    "code": "pack-csv-opaque",
                    "detail": (
                        f"cited artifact {p.name!r} is a non-json pack table — accepted "
                        f"as an OPAQUE citation (content never parsed); provenance is its "
                        f"containing run {owner!r} (R2)."
                    ),
                }
            )
            candidates = [owner] if owner else []
            return "aggregate", apath, candidates, True, gaps
        # A metrics_aggregate.json — read the Task-1 contributing set.
        data = _read_json(p)
        prov = (data or {}).get("provenance") if isinstance(data, dict) else None
        candidates, prov_gaps = _candidates_from_provenance(prov, seed_ref=apath)
        gaps.extend(prov_gaps)
        return "aggregate", apath, candidates, False, gaps

    # run_id seed: read the run's persisted aggregate for its contributing set;
    # fall back to the run + its supersession lineage when none was persisted.
    data = _read_json(_aggregate_path_for_run(experiment_dir, rid))
    prov = (data or {}).get("provenance") if isinstance(data, dict) else None
    if prov is not None:
        candidates, prov_gaps = _candidates_from_provenance(prov, seed_ref=rid)
        gaps.extend(prov_gaps)
        if rid not in candidates:
            candidates.append(rid)
        return "run", rid, candidates, False, gaps
    # No persisted table — the lineage IS the candidate universe.
    from hpc_agent.state.scopes import lineage_chain

    gaps.append(
        {
            "code": "table-run-set-link-absent",
            "detail": (
                f"run {rid!r} has no persisted metrics_aggregate.json — no first-class "
                "table→run-set link; candidates derived from the supersession lineage "
                "(G4a)."
            ),
        }
    )
    candidates = list(lineage_chain(experiment_dir, rid))
    return "run", rid, candidates, False, gaps


def _candidates_from_provenance(
    prov: Any, *, seed_ref: str
) -> tuple[list[str], list[dict[str, str]]]:
    """Extract ``contributing_run_ids`` from an aggregate's provenance block.

    Discloses the G4a gap when the block predates Task 1 (no
    ``contributing_run_ids``), and the G4d gap when the reduce was human-directed
    (``source == "human-directed"`` — the operator-bypass table settle).
    """
    gaps: list[dict[str, str]] = []
    if not isinstance(prov, dict):
        gaps.append(
            {
                "code": "table-run-set-link-absent",
                "detail": (
                    f"cited artifact {seed_ref!r} carries no provenance block — the "
                    "table keeps no record of which runs' pieces it consumed (G4a)."
                ),
            }
        )
        return [], gaps
    contributing = prov.get("contributing_run_ids")
    if not isinstance(contributing, list) or not contributing:
        gaps.append(
            {
                "code": "table-run-set-link-absent",
                "detail": (
                    f"cited artifact {seed_ref!r} predates reduce-time provenance "
                    "(no contributing_run_ids) — the table→run-set link is not "
                    "first-class (G4a)."
                ),
            }
        )
        contributing = []
    source = str(prov.get("source") or "")
    if source in ("human-directed", "operator-settled"):
        gaps.append(
            {
                "code": "operator-bypass",
                "detail": (
                    f"cited artifact {seed_ref!r} was reduced OUTSIDE the sanctioned "
                    f"flow (source={source!r}) — its numbers are operator-settled, "
                    "provenance human-asserted (G4d)."
                ),
            }
        )
    return [str(c) for c in contributing if isinstance(c, str) and c], gaps


def _apply_exclusions(
    experiment_dir: Path, candidates: list[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Carve the candidate universe down to the minimal set; disclose each cut.

    Three mechanical exclusions, each a countable disclosed fact, applied in
    order (a run gets exactly ONE reason — the first that matches):

    1. **canary** — a ``-canary`` / ``-canary2`` family sibling (the one suffix
       definition, ``canary_parent_of``);
    2. **superseded** — a lineage member another candidate supersedes (it appears
       as a non-head in another candidate's ``lineage_chain``; keep the newest);
    3. **dead-end** — a run with NO harvest receipt (never harvested into a
       citable table). The "no piece under remote_path" leg is a remote scan
       (SSH); this local walk uses the durable harvest-receipt ledger as the
       dead-end signal and discloses that basis.
    """
    from hpc_agent.ops.monitor.harvest_guard import harvest_receipt_exists
    from hpc_agent.ops.monitor.reconcile import canary_parent_of
    from hpc_agent.state.scopes import lineage_chain

    # De-dup, preserve first-seen order.
    seen: set[str] = set()
    ordered: list[str] = []
    for r in candidates:
        if r and r not in seen:
            seen.add(r)
            ordered.append(r)

    # Superseded set: a candidate that appears as a non-head (an older member) in
    # ANY candidate's supersession chain.
    superseded: set[str] = set()
    for r in ordered:
        for older in lineage_chain(experiment_dir, r)[1:]:
            if older in seen and older != r:
                superseded.add(older)

    kept: list[str] = []
    excluded: list[dict[str, str]] = []
    for r in ordered:
        if canary_parent_of(r) is not None:
            excluded.append({"run_id": r, "reason": "canary"})
            continue
        if r in superseded:
            excluded.append({"run_id": r, "reason": "superseded"})
            continue
        if not harvest_receipt_exists(experiment_dir, r):
            excluded.append({"run_id": r, "reason": "dead-end (no harvest receipt on the ledger)"})
            continue
        kept.append(r)
    return kept, excluded


def _receipts_chain(experiment_dir: Path, run_ids: list[str]) -> list[dict[str, Any]]:
    """Walk the receipts chain per contributing run — presence / counts only."""
    from hpc_agent.ops.monitor.harvest_guard import harvest_receipt_exists
    from hpc_agent.state.decision_journal import read_decisions

    out: list[dict[str, Any]] = []
    for rid in run_ids:
        repro = (experiment_dir / "_aggregated" / rid / "reproduction_receipts.jsonl").is_file()
        try:
            recs = read_decisions(experiment_dir, "run", rid)
            greenlights = sum(1 for rec in recs if rec.get("response") == "y")
        except errors.SpecInvalid:
            greenlights = 0
        out.append(
            {
                "run_id": rid,
                "harvest_receipt": harvest_receipt_exists(experiment_dir, rid),
                "reproduction_receipt": repro,
                "greenlights": greenlights,
            }
        )
    return out


def _rederivation_steps(run_ids: list[str], seed_kind: str, seed_ref: str) -> list[dict[str, Any]]:
    """The runnable re-derivation steps — a reproduce/canary pair per run, then aggregate.

    Emitted as structured hints (a runnable artifact, not prose). ``extract-recipe``
    NEVER executes them — it only names the shipped verbs that would re-mint each
    identity and reduce them to the same table.
    """
    steps: list[dict[str, Any]] = []
    for rid in run_ids:
        steps.append({"verb": "reproduce-run", "spec_hint": {"original_run_id": rid}})
        steps.append({"verb": "submit-s2", "spec_hint": {"run_id": f"{rid}-repro"}})
    steps.append(
        {
            "verb": "aggregate",
            "spec_hint": {"over": list(run_ids), "seed": {"kind": seed_kind, "ref": seed_ref}},
        }
    )
    return steps


@primitive(
    name="extract-recipe",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Walk a citable artifact BACK to its minimal contributing run-set and "
            "emit the clean-reproduction recipe: the run-set with canary / "
            "superseded / dead-end runs mechanically EXCLUDED (each exclusion "
            "disclosed + counted), each run's full provenance fingerprint incl. the "
            "wheel sha (hpc_agent_version) and the resolved-environment lock "
            "(env_lock_sha), a signature over ONLY the minimal set, "
            "the runnable re-derivation steps, the receipts chain, and every gap it "
            "cannot bridge DISCLOSED. Read-only, no SSH. Seed with exactly one of "
            "--run-id / --campaign-id / --aggregate-path. A pack *.csv is an OPAQUE "
            "citation — its content is never parsed."
        ),
        spec_arg=True,
        spec_model=ExtractRecipeInput,
        experiment_dir_arg=True,
        requires_ssh=False,
        schema_ref=SchemaRef(input="extract_recipe"),
    ),
    agent_facing=True,
)
def extract_recipe(experiment_dir: Path, *, spec: ExtractRecipeInput) -> dict[str, Any]:
    """Return the derived clean-reproduction recipe for a citable artifact.

    Resolves the seed to a contributing-run universe, mechanically excludes the
    canary / superseded / dead-end members (each disclosed + counted), projects
    each kept run's fingerprint (incl. ``hpc_agent_version`` + ``env_lock_sha``,
    each preferring a signed manifest value), signs ONLY the minimal set, and
    walks the receipts chain — disclosing every G4 gap it cannot
    bridge. Pure derived state, recomputed from disk on every call.

    Raises :class:`errors.SpecInvalid` on a bad seed (not exactly one) or an
    absent ``aggregate_path``.
    """
    from hpc_agent.ops.provenance_manifest import manifest_signature
    from hpc_agent.ops.recipe_render import render_recipe

    experiment_dir = Path(experiment_dir)
    seed_kind, seed_ref, candidates, artifact_opaque, gaps = _resolve_seed(experiment_dir, spec)

    minimal, excluded = _apply_exclusions(experiment_dir, candidates)
    runs = [_fingerprint(experiment_dir, rid) for rid in minimal]

    # A recipe-specific attestation over ONLY the minimal set (not a whole-campaign
    # signature). Reuses the ONE signable-digest definition (sorted-keys sha-256).
    recipe_body = {
        "recipe_schema_version": RECIPE_SCHEMA_VERSION,
        "minimal_run_ids": minimal,
        "runs": runs,
    }
    recipe_signature = manifest_signature(recipe_body)

    receipts = _receipts_chain(experiment_dir, minimal)
    steps = _rederivation_steps(minimal, seed_kind, seed_ref)

    seed_kind_typed: Literal["run", "campaign", "aggregate"] = seed_kind  # type: ignore[assignment]
    result = ExtractRecipeResult(
        recipe_schema_version=RECIPE_SCHEMA_VERSION,
        seed_kind=seed_kind_typed,
        seed_ref=seed_ref,
        artifact_opaque=artifact_opaque,
        minimal_run_ids=minimal,
        runs=runs,
        excluded=list(excluded),
        recipe_signature=recipe_signature,
        rederivation_steps=steps,
        receipts=receipts,
        gaps=list(gaps),
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    # The markdown render rides on the dumped dict so the render path stays
    # wire-free (the ops op owns the Pydantic boundary).
    dumped["markdown"] = render_recipe(dumped)
    return dumped


# --- the evidence-memory ``recipe`` citation resolver (BR-5) ------------------


def recipe_citation_ref(seed_kind: str, seed_ref: str) -> str:
    """Compose a ``recipe`` citation ``ref`` for a seed → ``"<seed_kind>:<seed_ref>"``.

    The ONE spelling of the ref shape the resolver parses (``run`` / ``campaign``
    / ``aggregate``), so a conclusion author and :func:`resolve_recipe_citation`
    can never drift. *seed_ref* is an opaque identity / path — never read for
    meaning here.
    """
    return f"{seed_kind}:{seed_ref}"


def _recipe_summary(recipe: dict[str, Any]) -> str:
    """A one-line disclosure summary over a resolved recipe (counts + wheel source).

    IDENTITY / COUNTING only (the recipe posture): the minimal run-set SIZE, the
    exclusions COUNT, the disclosed-gaps COUNT, and which wheel-sha SOURCE(S) the
    fingerprints used (``signed-manifest`` vs ``sidecar``). Names no metric.
    """
    minimal = recipe.get("minimal_run_ids") or []
    excluded = recipe.get("excluded") or []
    gaps = recipe.get("gaps") or []
    runs = recipe.get("runs") or []
    sources = sorted(
        {
            r["hpc_agent_version_source"]
            for r in runs
            if isinstance(r, dict) and r.get("hpc_agent_version_source")
        }
    )
    wheel_src = ",".join(sources) if sources else "-"
    return (
        f"minimal {len(minimal)}, excluded {len(excluded)}, gaps {len(gaps)}, wheel-src {wheel_src}"
    )


def resolve_recipe_citation(experiment_dir: Path, ref: str) -> tuple[str, str] | None:
    """Resolve a ``recipe`` citation ref → ``(recipe_signature, summary)`` or ``None``.

    The INJECTED resolver ops callers pass into
    :func:`state.evidence.resolve_citation` (``state`` never imports ``ops``; the
    recipe is COMPUTED). Parses *ref* (``"<seed_kind>:<seed_ref>"``), re-derives
    the recipe via :func:`extract_recipe`, and returns its ``recipe_signature``
    (the parity target) with a compact disclosure *summary* (minimal run-set size,
    exclusions count, disclosed-gaps count, wheel-sha source). Returns ``None`` —
    "not derivable on this namespace" — for a malformed ref or ANY derivation
    failure (a wiped run, an absent aggregate path, a bad seed): the resolver is
    fail-safe, so a moved artifact DISCLOSES at read and REFUSES loudly at the
    append gate, never crashes. Pure read; never raises.
    """
    seed_kind, sep, seed_ref = ref.partition(":")
    field = _RECIPE_SEED_FIELD.get(seed_kind)
    if not sep or field is None or not seed_ref:
        return None
    try:
        spec = ExtractRecipeInput(**{field: seed_ref})
        recipe = extract_recipe(experiment_dir, spec=spec)
    except Exception:  # noqa: BLE001 — read side: any derivation failure is "not derivable"
        return None
    signature = recipe.get("recipe_signature")
    if not isinstance(signature, str) or not signature:
        return None
    return signature, _recipe_summary(recipe)
