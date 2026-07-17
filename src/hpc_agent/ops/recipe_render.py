"""Deterministic markdown render for ``extract-recipe`` (the ``relay_render.py``
posture, clean-reproduction extraction proposal #1).

Pure string formatting over the recipe's own structured fields — IDENTITY (which
runs, at which shas), ORDERING (the re-derivation steps), COUNTING (exclusion +
receipt counts). It imports nothing LLM-adjacent and nothing from ``_wire`` (the
``ops`` op owns the Pydantic boundary), takes no free-prose parameter, and never
names a metric value: a fingerprint field renders as a sha pointer, an exclusion
as a run-id + a reason word, a gap as a code + a disclosed detail. The boundary
test (``tests/contracts/test_extract_recipe_boundary.py``) pins this posture.
"""

from __future__ import annotations

from typing import Any

__all__ = ["render_recipe"]

# The fingerprint fields rendered per contributing run, in a fixed order — the
# identity legs the directive names (params/code/data/env/env-lock/wheel/cluster).
# A metric value is NEVER among them.
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


def _fmt(value: Any) -> str:
    """Render one fingerprint value — ``-`` for an absent (null) field."""
    return "-" if value is None else str(value)


def render_recipe(recipe: dict[str, Any]) -> str:
    """Render the recipe dict as one deterministic markdown document.

    *recipe* is the ``ExtractRecipeResult`` as a plain dict (the op dumps the
    model to JSON mode before calling here, so the render path stays wire-free).
    The output is stable for a given recipe so a caller can diff two renders.
    """
    seed_kind = str(recipe.get("seed_kind", ""))
    seed_ref = str(recipe.get("seed_ref", ""))
    minimal = list(recipe.get("minimal_run_ids") or [])
    runs = list(recipe.get("runs") or [])
    excluded = list(recipe.get("excluded") or [])
    steps = list(recipe.get("rederivation_steps") or [])
    receipts = list(recipe.get("receipts") or [])
    gaps = list(recipe.get("gaps") or [])

    lines: list[str] = []
    lines.append(f"# Clean-reproduction recipe — {seed_kind} `{seed_ref}`")
    lines.append("")
    lines.append(f"signature: `{recipe.get('recipe_signature', '')}`")
    if recipe.get("artifact_opaque"):
        lines.append("")
        lines.append(
            "> artifact accepted as an OPAQUE citation (content never parsed); "
            "provenance is its containing run's."
        )
    lines.append("")

    # 1. Minimal run-set + fingerprints.
    lines.append(f"## Minimal run-set ({len(minimal)})")
    lines.append("")
    if runs:
        header = "| run_id | " + " | ".join(_FINGERPRINT_FIELDS) + " |"
        sep = "|" + "---|" * (len(_FINGERPRINT_FIELDS) + 1)
        lines.append(header)
        lines.append(sep)
        for run in runs:
            cells = [str(run.get("run_id", ""))]
            cells += [_fmt(run.get(f)) for f in _FINGERPRINT_FIELDS]
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("_(no contributing runs resolved)_")
    lines.append("")

    # 2. Exclusions (disclosed + counted).
    lines.append(f"## Excluded ({len(excluded)})")
    lines.append("")
    if excluded:
        for entry in excluded:
            lines.append(f"- `{entry.get('run_id', '')}` — {entry.get('reason', '')}")
    else:
        lines.append("_(nothing excluded)_")
    lines.append("")

    # 3. Runnable re-derivation steps.
    lines.append(f"## Re-derivation steps ({len(steps)})")
    lines.append("")
    for i, step in enumerate(steps, start=1):
        verb = step.get("verb", "")
        hint = step.get("spec_hint")
        hint_str = f" {hint}" if hint else ""
        lines.append(f"{i}. `{verb}`{hint_str}")
    if not steps:
        lines.append("_(no steps)_")
    lines.append("")

    # 4. Receipts chain (presence / counts only).
    lines.append("## Receipts")
    lines.append("")
    for r in receipts:
        lines.append(
            f"- `{r.get('run_id', '')}` — harvest_receipt="
            f"{str(bool(r.get('harvest_receipt'))).lower()} "
            f"reproduction_receipt={str(bool(r.get('reproduction_receipt'))).lower()} "
            f"greenlights={int(r.get('greenlights', 0))}"
        )
    if not receipts:
        lines.append("_(no receipts)_")
    lines.append("")

    # 5. Gaps — disclosed, never papered.
    lines.append(f"## Disclosed gaps ({len(gaps)})")
    lines.append("")
    if gaps:
        for g in gaps:
            lines.append(f"- **{g.get('code', '')}** — {g.get('detail', '')}")
    else:
        lines.append("_(no gaps — the receipts chain is complete)_")
    lines.append("")

    return "\n".join(lines)
