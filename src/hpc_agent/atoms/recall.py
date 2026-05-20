"""``recall`` primitive — query past interview.json files for memory across campaigns.

The interview primitive persists structured intent into
``<campaign_dir>/interview.json``. ``recall`` walks one or more roots
for those files and returns recency-ordered, filterable summaries plus
a ``rollup`` block that pre-computes the aggregations the
next-interview agent would otherwise have to derive from scratch.

The rollup has three tiers, ranked by compute cost / opt-in level:

* **Tier 1 (always-on)** — Pure interview.json projections: count,
  histograms over task_kind / operator / produced_by_kind /
  task_generator.kind / cluster, task_count quantiles, materialized_at
  envelope. Free; computed from the same data already projected.
* **Tier 2 (``include_runtime=True``)** — Walks each campaign's
  ``.hpc/runtimes/*.json`` files and aggregates ``elapsed_sec``,
  ``exit_code`` across every sample. Output: walltime quantiles,
  failure rate, count of campaigns with no runtime data.
* **Tier 3 (``include_generator_stats=True``)** — Buckets matched
  campaigns by ``task_generator.kind`` and reports observed parameter
  envelopes (e.g. for ``numeric_logspace``: low/high/n ranges across
  campaigns). Most useful when filtered to a single ``task_kind`` so
  bucket buckets aren't noisy.

Roots resolution: callers pass an explicit list, or omit it to fall
back to ``~/.hpc-agent/config.json:experiment_roots``. Both empty
raises — no implicit cwd default.

Read-only and idempotent. No persistent index DB beyond the config
file. Malformed ``interview.json`` files are skipped silently.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent._internal.primitive import primitive
from hpc_agent._schema_models.queries.recall import RecallSpec

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


__all__ = ["recall_campaigns", "resolve_roots"]


# Hard cap on filesystem walk to bound scans of giant trees.
_MAX_INTERVIEW_FILES = 10_000

# Per-user config; unrelated to the per-repo .hpc/ tree.
_USER_CONFIG = Path("~/.hpc-agent/config.json").expanduser()


@primitive(
    name="recall",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-agent recall",
    agent_facing=True,
)
def recall_campaigns(
    roots: list[Path],
    *,
    spec: RecallSpec | None = None,
) -> dict[str, Any]:
    """Walk every path in *roots* for ``interview.json`` files; return
    filtered summaries plus a tiered rollup.

    *spec* is a wire-validated :class:`RecallSpec` (None defaults to
    a fresh ``RecallSpec()`` — every filter kwarg has a default so
    no-argument recall is a valid wire payload). The body
    destructures into typed locals so the rest reads naturally.

    *roots* is a framework-context kwarg: the wire surface carries
    only ``spec.root`` (a single path) but the Python entry point
    accepts a list so callers iterating ``experiment_roots`` from
    config don't need a wrapper loop.

    Returns ``{campaigns, total_matching, showing, rollup}``. The
    ``rollup`` block always contains Tier 1; Tier 2 keys are present
    only when ``include_runtime=True``; Tier 3 keys are present only
    when ``include_generator_stats=True``.

    Raises ``ValueError`` if any root is not an existing directory.
    """
    if spec is None:
        spec = RecallSpec()
    task_kind = spec.task_kind
    operator = spec.operator
    since = spec.since
    limit = int(spec.limit)
    include_runtime = bool(spec.include_runtime)
    include_generator_stats = bool(spec.include_generator_stats)

    if not roots:
        raise ValueError(
            f"no roots to walk — pass --root or set experiment_roots in {_USER_CONFIG}"
        )
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"recall root is not a directory: {root}")

    summaries = list(_collect(roots, task_kind=task_kind, operator=operator, since=since))
    summaries.sort(key=lambda r: r.get("materialized_at") or "", reverse=True)

    rollup: dict[str, Any] = _tier1_rollup(summaries)
    if include_runtime:
        rollup["runtime_rollup"] = _tier2_runtime_rollup(summaries)
    if include_generator_stats:
        rollup["generator_rollup"] = _tier3_generator_rollup(summaries)

    return {
        "campaigns": summaries[:limit],
        "total_matching": len(summaries),
        "showing": min(len(summaries), limit),
        "rollup": rollup,
    }


# ─── roots resolution ────────────────────────────────────────────────────


def resolve_roots(explicit: str | None) -> list[Path]:
    """Resolve recall roots: explicit CLI flag, or config-file default.

    Precedence:

    * If *explicit* is given, return ``[Path(explicit)]`` (size-one list).
    * Else read ``~/.hpc-agent/config.json:experiment_roots``; return
      every path it lists (with ``~`` expanded).
    * Else return an empty list — the caller raises a clearer error than
      "no campaigns matched."

    The config form lets operators bake in their machine's experiment
    layout once instead of repeating ``--root ~/experiments`` on every
    call. The CLI flag still wins when set, so ad-hoc queries against
    other roots remain frictionless.
    """
    if explicit is not None:
        return [Path(explicit).expanduser()]
    if not _USER_CONFIG.is_file():
        return []
    try:
        config = json.loads(_USER_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    raw = config.get("experiment_roots") or []
    if not isinstance(raw, list):
        return []
    return [Path(p).expanduser() for p in raw if isinstance(p, str) and p]


# ─── collection + per-campaign projection ────────────────────────────────


def _collect(
    roots: list[Path],
    *,
    task_kind: str | None,
    operator: str | None,
    since: str | None,
) -> Iterable[dict[str, Any]]:
    seen = 0
    for root in roots:
        for path in root.rglob("interview.json"):
            seen += 1
            if seen > _MAX_INTERVIEW_FILES:
                return
            try:
                doc = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            summary = _summarize(doc, path)
            if summary is None:
                continue
            if task_kind is not None and summary.get("task_kind") != task_kind:
                continue
            if operator is not None and summary.get("operator") != operator:
                continue
            if since is not None:
                mat_at = summary.get("materialized_at")
                if not mat_at or mat_at < since:
                    continue
            yield summary


def _summarize(doc: dict[str, Any], path: Path) -> dict[str, Any] | None:
    """Project an interview.json doc to the recall summary shape.

    Returns None for docs missing ``_materialized`` (legacy or
    hand-written files that happen to be named interview.json).

    The projection deliberately mirrors what the next-interview agent
    would compare against, not just file-listing metadata: budget,
    abort_if, task_generator, cluster_target are all included so the
    caller can ground prompts ('your last sweep budgeted 200 GPU-h…')
    without re-reading interview.json.
    """
    materialized = doc.get("_materialized") or {}
    if not materialized:
        return None
    produced_by = doc.get("produced_by") or {}
    generator = doc.get("task_generator")
    summary: dict[str, Any] = {
        "campaign_dir": str(path.parent.resolve()),
        "goal": doc.get("goal"),
        "task_kind": doc.get("task_kind"),
        "task_count": doc.get("task_count"),
        "operator": produced_by.get("operator"),
        "produced_by_kind": produced_by.get("kind"),
        "materialized_at": materialized.get("at"),
        "cmd_sha": materialized.get("cmd_sha"),
        # Fix A: surface the structured prior-decision fields the next
        # interviewer wants to compare against. Drop only the verbose
        # transcript / notes fields — those the agent re-reads on demand.
        "budget": doc.get("budget"),
        "abort_if": doc.get("abort_if"),
        "cluster_target": doc.get("cluster_target"),
        "task_generator": (
            {"kind": generator.get("kind"), "params": generator.get("params")}
            if isinstance(generator, dict)
            else None
        ),
    }
    return summary


# ─── Tier 1: invariant rollup over interview.json fields ─────────────────


def _tier1_rollup(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-field rollup: histograms + task_count quantiles + time envelope.

    Fields chosen for invariance — every campaign produces them
    regardless of task family. Histograms drop None values so a missing
    task_kind doesn't show up as a "None" bucket.
    """
    if not summaries:
        return {
            "count": 0,
            "task_kind_distribution": {},
            "operators": {},
            "produced_by_kinds": {},
            "task_generator_kinds": {},
            "clusters": {},
            "task_count": None,
            "materialized_at": None,
        }

    task_counts = [int(s["task_count"]) for s in summaries if isinstance(s.get("task_count"), int)]
    times = [s["materialized_at"] for s in summaries if s.get("materialized_at")]
    return {
        "count": len(summaries),
        "task_kind_distribution": _histogram(s.get("task_kind") for s in summaries),
        "operators": _histogram(s.get("operator") for s in summaries),
        "produced_by_kinds": _histogram(s.get("produced_by_kind") for s in summaries),
        "task_generator_kinds": _histogram(
            (s.get("task_generator") or {}).get("kind") for s in summaries
        ),
        "clusters": _histogram((s.get("cluster_target") or {}).get("cluster") for s in summaries),
        "task_count": (
            {
                "p50": _pctile(task_counts, 0.50),
                "p95": _pctile(task_counts, 0.95),
                "min": min(task_counts),
                "max": max(task_counts),
            }
            if task_counts
            else None
        ),
        "materialized_at": ({"earliest": min(times), "latest": max(times)} if times else None),
    }


# ─── Tier 2: runtime rollup from .hpc/runtimes/*.json ────────────────────


def _tier2_runtime_rollup(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk each campaign's runtime-prior files; aggregate per-task stats.

    The runtime priors are written by ``orchestrator.runtime_prior.append_sample``
    once per dispatched task and contain ``elapsed_sec`` and
    ``exit_code``. Failed = exit_code != 0; preempted is currently not
    distinguishable here without sidecar inspection (a future enhancement).

    Campaigns with no runtime files are counted separately — typically
    submit-but-nothing-finished campaigns.
    """
    elapsed: list[int] = []
    failed = 0
    total = 0
    no_runtime = 0
    for s in summaries:
        camp = Path(s["campaign_dir"])
        runtimes_dir = camp / ".hpc" / "runtimes"
        if not runtimes_dir.is_dir():
            no_runtime += 1
            continue
        any_samples = False
        for runtime_file in runtimes_dir.glob("*.json"):
            try:
                doc = json.loads(runtime_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            samples = doc.get("samples") or []
            if not isinstance(samples, list):
                continue
            for sample in samples:
                any_samples = True
                total += 1
                try:
                    sec = int(sample.get("elapsed_sec", 0))
                except (TypeError, ValueError):
                    sec = 0
                if sec > 0:
                    elapsed.append(sec)
                if int(sample.get("exit_code", 0) or 0) != 0:
                    failed += 1
        if not any_samples:
            no_runtime += 1

    return {
        "walltime_per_task_sec": (
            {
                "p50": _pctile(elapsed, 0.50),
                "p95": _pctile(elapsed, 0.95),
                "min": min(elapsed),
                "max": max(elapsed),
                "n_samples": len(elapsed),
            }
            if elapsed
            else None
        ),
        "failure_rate": (failed / total) if total else None,
        "total_task_samples": total,
        "campaigns_with_no_runtime": no_runtime,
    }


# ─── Tier 3: generator-aware parameter envelopes ─────────────────────────


def _tier3_generator_rollup(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Bucket campaigns by task_generator.kind; report observed envelopes.

    Each kind reports counts + the parameter shape that's actually
    aggregable for that kind:

    * ``numeric_logspace`` / ``numeric_linspace``: param + low/high/n
      ranges across campaigns. Different ``param`` names within one
      bucket are kept distinct.
    * ``cartesian_product``: per-axis value union (every value ever
      seen on each axis name across the bucket).
    * ``items_x_seeds`` / ``enumerated``: just the count — item-level
      structure isn't usefully aggregable across campaigns.

    Returns ``{by_kind: {kind: {count, ...}}}``. No 'by_kind: {}' for
    None, so summaries without a task_generator don't pollute the
    output.
    """
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for s in summaries:
        gen = s.get("task_generator")
        if not isinstance(gen, dict) or not gen.get("kind"):
            continue
        by_kind.setdefault(gen["kind"], []).append(gen.get("params") or {})

    out: dict[str, Any] = {"by_kind": {}}
    for kind, paramsets in by_kind.items():
        bucket: dict[str, Any] = {"count": len(paramsets)}
        if kind in ("numeric_logspace", "numeric_linspace"):
            by_param: dict[str, dict[str, list[float]]] = {}
            for p in paramsets:
                pname = p.get("param")
                if not pname:
                    continue
                d = by_param.setdefault(pname, {"low": [], "high": [], "n": []})
                if isinstance(p.get("low"), (int, float)):
                    d["low"].append(float(p["low"]))
                if isinstance(p.get("high"), (int, float)):
                    d["high"].append(float(p["high"]))
                if isinstance(p.get("n"), int):
                    d["n"].append(float(p["n"]))
            bucket["param_envelopes"] = {
                pname: {
                    "low": [min(d["low"]), max(d["low"])] if d["low"] else None,
                    "high": [min(d["high"]), max(d["high"])] if d["high"] else None,
                    "n": [int(min(d["n"])), int(max(d["n"]))] if d["n"] else None,
                }
                for pname, d in by_param.items()
            }
        elif kind == "cartesian_product":
            # Split the dedup set keyed by (type-name, value) so True/1 and
            # False/0 don't collapse together (Python treats bool as int).
            axis_values: dict[str, set[tuple[str, Any]]] = {}
            for p in paramsets:
                axes = p.get("axes")
                if not isinstance(axes, dict):
                    continue
                for axis_name, vals in axes.items():
                    if isinstance(vals, list):
                        axis_values.setdefault(axis_name, set()).update(
                            (type(v).__name__, v)
                            for v in vals
                            if isinstance(v, (str, int, float, bool))
                        )
            bucket["axis_value_unions"] = {
                name: [
                    v
                    for _, v in sorted(pairs, key=lambda kv: (isinstance(kv[1], str), kv[0], kv[1]))
                ]
                for name, pairs in axis_values.items()
            }
        out["by_kind"][kind] = bucket
    return out


# ─── helpers ─────────────────────────────────────────────────────────────


def _histogram(values: Iterable[Any]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for v in values:
        if v is None:
            continue
        counter[str(v)] += 1
    return dict(counter)


def _pctile(values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile. Returns 0 for empty input."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    idx = (len(s) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (idx - lo))
