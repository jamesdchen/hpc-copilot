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
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.recall import RecallSpec
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Iterable, Sequence


__all__ = ["recall_campaigns", "resolve_roots"]

# Mirrors the ``cmd_sha`` pattern on recall's output schema (_CampaignSummary).
_CMD_SHA_RE = re.compile(r"^[0-9a-f]{8,64}$")


def _is_valid_cmd_sha(value: Any) -> bool:
    """True if *value* is a lowercase-hex cmd_sha the output schema accepts."""
    return isinstance(value, str) and bool(_CMD_SHA_RE.match(value))


# Hard cap on filesystem walk to bound scans of giant trees.
_MAX_INTERVIEW_FILES = 10_000

# Per-user config; unrelated to the per-repo .hpc/ tree.
_USER_CONFIG = Path("~/.hpc-agent/config.json").expanduser()


def _recall_arg_pre(ns: Namespace) -> dict[str, Any]:
    """Build {spec, roots} from the recall CLI flags.

    Re-maps the individual --root / --limit / --task-kind / --operator /
    --since / --include-* flags into a ``RecallSpec`` payload (wire-validated
    via the recall JSON schema, then ``model_validate``-d) plus the
    ``roots`` list the primitive expects. The raw flag values are dropped
    by the dispatcher's signature-based kwarg filter.
    """
    from hpc_agent.cli._helpers import _validate_against_schema

    payload: dict[str, Any] = {
        "limit": int(ns.limit),
        "include_runtime": bool(ns.include_runtime),
        "include_generator_stats": bool(ns.include_generator_stats),
    }
    if getattr(ns, "root", None):
        payload["root"] = ns.root
    if getattr(ns, "task_kind", None):
        payload["task_kind"] = ns.task_kind
    if getattr(ns, "operator", None):
        payload["operator"] = ns.operator
    if getattr(ns, "since", None):
        payload["since"] = ns.since
    _validate_against_schema(payload, "recall")
    return {
        "spec": _model_validate_or_raise(RecallSpec, payload),
        "roots": resolve_roots(getattr(ns, "root", None)),
    }


def _model_validate_or_raise(model_cls, payload):
    """Wrap pydantic ``model_validate`` so failures map to typed ``SpecInvalid``."""
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError shape
        from hpc_agent import errors

        raise errors.SpecInvalid(str(exc)) from exc


@primitive(
    name="recall",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Query past interview.json files under --root. Returns "
            "recency-sorted campaign summaries (goal, task_kind, "
            "task_count, operator, materialized_at, cmd_sha) for use as "
            "context in the next interview."
        ),
        args=(
            CliArg(
                "--limit",
                type=int,
                default=20,
                help="Maximum number of summaries to return (default 20).",
            ),
            CliArg(
                "--include-runtime",
                action="store_true",
                help=(
                    "Tier 2 rollup: walk each matched campaign's "
                    ".hpc/runtimes/*.json and aggregate elapsed_sec / failure "
                    "rate across all dispatched tasks."
                ),
            ),
            CliArg(
                "--include-generator-stats",
                action="store_true",
                help=(
                    "Tier 3 rollup: bucket by task_generator.kind and report "
                    "observed parameter envelopes. Most useful with --task-kind "
                    "also set."
                ),
            ),
            CliArg(
                "--root",
                type=str,
                default=None,
                help=(
                    "Filesystem directory to walk recursively for interview.json. "
                    "When omitted, falls back to ~/.hpc-agent/config.json:"
                    "experiment_roots; if neither is set, errors."
                ),
            ),
            CliArg(
                "--task-kind",
                type=str,
                default=None,
                help="Exact-match filter against intent.task_kind.",
            ),
            CliArg(
                "--operator",
                type=str,
                default=None,
                help="Exact-match filter against intent.produced_by.operator.",
            ),
            CliArg(
                "--since",
                type=str,
                default=None,
                help=(
                    "ISO-8601 timestamp; only campaigns materialized at or "
                    "after this point are returned."
                ),
            ),
        ),
        arg_pre=_recall_arg_pre,
    ),
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
        raise errors.SpecInvalid(
            f"no roots to walk — pass --root or set experiment_roots in {_USER_CONFIG}"
        )
    for root in roots:
        if not root.is_dir():
            raise errors.SpecInvalid(f"recall root is not a directory: {root}")

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
        config = json.loads(_USER_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
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
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
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
    # interview.json is documented as untrusted (hand-written / legacy /
    # cross-version files are tolerated and merely skipped when un-materialized).
    # The recall output schema constrains these fields (cmd_sha hex pattern,
    # produced_by_kind enum, task_count >= 0 int), so a malformed-but-present
    # value would fail output validation and surface as an `internal` error
    # instead of being recalled. Coerce out-of-contract values to None — the
    # schema makes all three nullable.
    raw_cmd_sha = materialized.get("cmd_sha")
    cmd_sha = raw_cmd_sha if _is_valid_cmd_sha(raw_cmd_sha) else None
    raw_kind = produced_by.get("kind")
    produced_by_kind = raw_kind if raw_kind in ("agent", "human") else None
    raw_task_count = doc.get("task_count")
    task_count = (
        raw_task_count
        if isinstance(raw_task_count, int)
        and not isinstance(raw_task_count, bool)
        and raw_task_count >= 0
        else None
    )
    summary: dict[str, Any] = {
        "campaign_dir": str(path.parent.resolve()),
        "goal": doc.get("goal"),
        "task_kind": doc.get("task_kind"),
        "task_count": task_count,
        "operator": produced_by.get("operator"),
        "produced_by_kind": produced_by_kind,
        "materialized_at": materialized.get("at"),
        "cmd_sha": cmd_sha,
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
        "data_axes": _axis_classifications(path.parent),
    }
    return summary


def _axis_classifications(campaign_dir: Path) -> dict[str, Any] | None:
    """Project the classified DataAxis per @register_run from a sibling axes.yaml.

    Best-effort: reads ``<campaign_dir>/.hpc/axes.yaml``'s ``executors``
    block so the next classification interview can pre-fill from a prior
    similar experiment instead of re-asking cold. Parses leniently (a
    malformed axes.yaml is skipped, matching recall's silent-skip
    contract — it does NOT route through the schema-validating
    ``read_axes``). Returns ``None`` when there is no axes.yaml, no
    ``executors`` block, or the file is unreadable.
    """
    axes_yaml = campaign_dir / ".hpc" / "axes.yaml"
    if not axes_yaml.is_file():
        return None
    try:
        import yaml

        doc = yaml.safe_load(axes_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):  # type: ignore[name-defined]
        return None
    if not isinstance(doc, dict):
        return None
    executors = doc.get("executors")
    if not isinstance(executors, dict):
        return None
    out: dict[str, Any] = {}
    for run_name, entry in executors.items():
        if not isinstance(entry, dict):
            continue
        data_axis = entry.get("data_axis")
        if not isinstance(data_axis, dict) or not data_axis.get("kind"):
            continue
        proj: dict[str, Any] = {"kind": data_axis["kind"]}
        halo = data_axis.get("halo")
        if isinstance(halo, dict) and halo.get("expr"):
            proj["halo_expr"] = halo["expr"]
        if data_axis.get("monoid"):
            proj["monoid"] = data_axis["monoid"]
        out[str(run_name)] = proj
    return out or None


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
            "data_axis_kinds": {},
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
        "clusters": _histogram(_cluster_of(s) for s in summaries),
        "data_axis_kinds": _histogram(
            proj.get("kind")
            for s in summaries
            for proj in (s.get("data_axes") or {}).values()
            if isinstance(proj, dict)
        ),
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
                doc = json.loads(runtime_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
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


def _cluster_of(summary: dict[str, Any]) -> Any:
    """Cluster name from a summary's ``cluster_target``, tolerating bad shapes.

    ``cluster_target`` is copied verbatim from arbitrary interview.json
    files, which recall is documented to tolerate — a hand-written or
    legacy file may carry a string/list there instead of a dict.
    """
    ct = summary.get("cluster_target")
    return ct.get("cluster") if isinstance(ct, dict) else None


def _histogram(values: Iterable[Any]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for v in values:
        if v is None:
            continue
        counter[str(v)] += 1
    return dict(counter)


def _pctile(values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile via stdlib ``statistics.quantiles``.

    The ``n=100`` / ``method="inclusive"`` cut points reproduce the old
    hand-rolled ``(len-1)*p`` interpolation exactly. Guards the two inputs
    ``statistics.quantiles`` rejects: empty (returns 0.0) and a single
    sample (returns that value).
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(statistics.quantiles(values, n=100, method="inclusive")[round(p * 100) - 1])
