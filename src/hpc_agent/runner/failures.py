"""Failure clustering by stderr fingerprint + retry-policy advice."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from hpc_agent.infra.parsing import (
    FAILURE_CATEGORY_PATTERNS as _FAILURE_CATEGORY_PATTERNS,
)
from hpc_agent.infra.parsing import (
    categorize_failure as _categorize,
)

if TYPE_CHECKING:
    from hpc_agent._internal.session import RunRecord

# Re-exported from :mod:`hpc_agent.infra.parsing` (extracted in PR 1.5 so
# multiple Wave-2 subjects can share the catalog without contending for
# this module). The high-level :func:`cluster_failures_by_fingerprint`
# orchestrator below layers exit-code overrides and the richer
# :func:`hpc_agent.runner.failure_signatures.classify` catalog on top.
__all__ = [
    "_FAILURE_CATEGORY_PATTERNS",
    "_categorize",
    "fingerprint_stderr_tail",
    "annotate_clusters_with_retry_advice",
    "cluster_failures_by_fingerprint",
    "DEFAULT_AUTO_RETRY_POLICY",
]

# Lines we strip before fingerprinting so per-task volatility (paths,
# pids, timestamps, line numbers in tracebacks) doesn't fragment a
# single failure mode into many "unique" fingerprints.
_FINGERPRINT_NOISE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*"),  # ISO timestamps
    re.compile(r"\b/(?:home|u|scratch|tmp)/[^\s:]+"),  # absolute paths
    re.compile(r"\bpid[=: ]\d+\b", re.I),
    re.compile(r"\bjob[_ ]?id[=: ]\d+\b", re.I),
    re.compile(r"\btask[_ ]?id[=: ]\d+\b", re.I),
    re.compile(r"\bline \d+"),
    re.compile(r"\b0x[0-9a-fA-F]+\b"),  # hex pointers
    re.compile(r"\b\d{8,}\b"),  # long ints (job ids, pids)
)


def fingerprint_stderr_tail(content: str | None, *, max_chars: int = 400) -> str:
    """Reduce a stderr blob to a stable, comparable fingerprint string.

    Strategy: take the last non-empty line of the tail (typically the
    actual exception), strip volatile noise (timestamps, abs paths, pids,
    hex pointers), and truncate.  Two failures with the same root cause
    on different tasks yield the same fingerprint.
    """
    if not content or not content.strip():
        return ""
    # Last non-empty line: the actual exception is almost always there.
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return ""
    line = lines[-1].strip()
    for pat in _FINGERPRINT_NOISE:
        line = pat.sub("", line)
    # Collapse runs of whitespace introduced by the substitutions.
    line = re.sub(r"\s{2,}", " ", line).strip()
    return line[:max_chars]


# Default per-failure-category retry policy. Conservative caps so an
# auto-retry never compounds a real bug into many wasted submissions; users
# can override per-run by passing ``auto_retry={...}`` to write_run_sidecar
# at /submit time. cmd_failures resolves: sidecar override > these defaults.
DEFAULT_AUTO_RETRY_POLICY: dict[str, dict[str, Any]] = {
    "gpu_oom": {"max_attempts": 1, "mem_multiplier": 1.5},
    "system_oom": {"max_attempts": 1, "mem_multiplier": 1.5},
    "walltime": {"max_attempts": 1, "walltime_multiplier": 2.0},
    "node_failure": {"max_attempts": 2},
    # SSH transport blips (auth flakes, network resets) are usually
    # transient — the cluster itself is fine, the control-plane channel
    # isn't. Match node_failure's max_attempts: a couple of retries
    # absorb the blip without compounding a real outage.
    "ssh_unreachable": {"max_attempts": 2},
}


def annotate_clusters_with_retry_advice(
    clusters: list[dict[str, Any]],
    *,
    auto_retry_policy: dict[str, dict[str, Any]],
    record: RunRecord,
) -> list[dict[str, Any]]:
    """Tag each failure cluster with retry eligibility per the supplied policy.

    *auto_retry_policy* maps failure-category strings to per-category
    policy dicts. Default categories and shape:

    .. code-block:: python

        {
            "gpu_oom":      {"max_attempts": 1, "mem_multiplier": 1.5},
            "system_oom":   {"max_attempts": 1, "mem_multiplier": 1.5},
            "walltime":     {"max_attempts": 1, "walltime_multiplier": 2.0},
            "node_failure": {"max_attempts": 2},
        }

    See :data:`DEFAULT_AUTO_RETRY_POLICY` for the framework's hardcoded
    fallback when no per-run override is set on the sidecar.

    For each cluster, looks up ``record.retries[tid].attempts`` and tags
    task ids as ``eligible_task_ids`` (attempts < max_attempts) or
    ``blocked_task_ids`` (already at the cap). The policy dict itself is
    echoed back so the caller can compute multiplied overrides.

    Mutates and returns *clusters* for the caller's convenience.
    """
    if not auto_retry_policy:
        return clusters
    for cluster in clusters:
        category = cluster.get("category")
        policy = auto_retry_policy.get(category) if isinstance(category, str) else None
        if not isinstance(policy, dict):
            continue  # No policy for this category; leave untouched.
        max_attempts = int(policy.get("max_attempts", 0) or 0)
        eligible: list[int] = []
        blocked: list[int] = []
        for tid in cluster.get("task_ids", []) or []:
            prior = record.retries.get(str(tid), {}) if record.retries else {}
            attempts = int(prior.get("attempts", 0) or 0)
            if attempts < max_attempts:
                eligible.append(tid)
            else:
                blocked.append(tid)
        cluster["retry_advice"] = {
            "policy": dict(policy),
            "eligible_task_ids": eligible,
            "blocked_task_ids": blocked,
        }
    return clusters


def cluster_failures_by_fingerprint(
    logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group ``fetch_task_logs`` output by failure fingerprint.

    *logs* is the list returned by :func:`fetch_task_logs`.  Output is a
    list of clusters, one per distinct fingerprint, sorted descending by
    member count.  Each cluster carries:

    * ``category``: high-level bucket (gpu_oom, walltime, etc., else 'unknown')
    * ``fingerprint``: noise-stripped last line of the stderr tail
    * ``count``: how many tasks share this failure
    * ``task_ids``: the list of task ids
    * ``sample``: a short representative snippet (last 200 chars)

    Tasks marked ``missing: True`` are split into two buckets so the
    operator can see at a glance which entries are genuinely missing
    logs (executor didn't write one) vs. which ones the SSH transport
    couldn't reach (an unreachable cluster shouldn't read as a
    code-side failure).

    * ``log_missing``: ``missing=True`` with no ``ssh_error``.
    * ``ssh_unreachable``: ``missing=True`` with an ``ssh_error`` string
      from :func:`fetch_task_logs` (every retry hit a transport error).
    """
    by_fp: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in logs:
        tid = entry.get("task_id")
        if entry.get("missing"):
            ssh_error = entry.get("ssh_error")
            category = "ssh_unreachable" if ssh_error else "log_missing"
            key = (category, "")
            bucket = by_fp.setdefault(
                key,
                {
                    "category": category,
                    "fingerprint": "",
                    "count": 0,
                    "task_ids": [],
                    # Surface the ssh_error so a single click into the
                    # bucket shows the actual transport failure instead
                    # of an empty sample. Truncate to keep the rollup
                    # compact.
                    "sample": (ssh_error or "")[:200],
                },
            )
            bucket["count"] += 1
            if tid is not None:
                bucket["task_ids"].append(tid)
            continue
        content = entry.get("content") or ""
        fp = fingerprint_stderr_tail(content)
        category = _categorize(content)
        # Exit-code-130 fallback: the dispatcher's SIGTERM-trap stderr
        # line may have been clipped from the log tail, but exit 130
        # is still a definitive preempted signal. Match the campus
        # user's bumped jobs to the ``preempted`` cluster regardless.
        # Also overrides ``walltime`` because the SLURM/SGE preempt
        # notification contains "signal SIGTERM 15" which the walltime
        # regex would otherwise claim.
        # Exit 130 is the dispatcher's own SIGTERM-trap signal. Exit 143
        # (128 + SIGTERM=15) is what the scheduler reports when the
        # dispatcher was killed directly before it could re-emit 130 —
        # so both indicate preemption.
        preempted_override = entry.get("exit_code") in (130, 143) and category in (
            "unknown",
            "walltime",
        )
        if preempted_override:
            category = "preempted"
        # D1c: VASPilot-pattern catalog returns a suggested_fix per error
        # class so integrating agents can auto-resubmit with adjusted
        # resources rather than asking the user. Importable as
        # ``hpc_agent.runner.failure_signatures.classify``.
        from hpc_agent.runner.failure_signatures import classify

        sig = classify(content, entry.get("exit_code"))
        # The category fallback above also needs to override sig — otherwise
        # the cluster carries ``category=preempted`` but
        # ``suggested_fix=increase-walltime`` (or ``unknown``) from the catalog,
        # which would auto-bump h_rt on every preempted job and burn the budget
        # (v3 BUG-6V3-3).
        if preempted_override:
            sig = {
                "error_class": "preempted",
                "suggested_fix": {"action": "resubmit-preempted"},
                "matched_pattern": "exit_code_fallback",
            }
        key = (category, fp)
        bucket = by_fp.setdefault(
            key,
            {
                "category": category,
                "fingerprint": fp,
                "count": 0,
                "task_ids": [],
                "sample": content[-200:].rstrip(),
                "suggested_fix": sig["suggested_fix"],
                "error_class": sig["error_class"],
            },
        )
        bucket["count"] += 1
        if tid is not None:
            bucket["task_ids"].append(tid)
    clusters = sorted(by_fp.values(), key=lambda b: -b["count"])
    return clusters
