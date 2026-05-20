"""``summarize-submit-plan`` primitive — canonical pre-submit summary string.

Replaces the agent-rendered "here's what I'm about to launch" prose at
/submit-hpc Step 5. Takes a ``submit_flow.input.json`` spec dict and
returns a deterministic multi-line summary the slash command prints
verbatim before asking the user to confirm.

Same reliability win as ``monitor-summary``: byte-stable framing for
the same input state, no per-tick wording drift across consecutive
runs.

Pure function over the spec dict; no filesystem reads, no SSH.
"""

from __future__ import annotations

from typing import Any

from hpc_agent import errors
from hpc_agent._internal.primitive import primitive


def _format_resources(spec: dict[str, Any]) -> str:
    """Compact one-liner of the per-task resource ask, if any."""
    res = spec.get("resources")
    if not isinstance(res, dict):
        return ""
    parts: list[str] = []
    if "cpus" in res:
        parts.append(f"cpus={res['cpus']}")
    if "mem" in res:
        parts.append(f"mem={res['mem']}")
    if "walltime" in res:
        parts.append(f"walltime={res['walltime']}")
    if "gpus" in res:
        parts.append(f"gpus={res['gpus']}")
    if "gpu_type" in res:
        parts.append(f"gpu_type={res['gpu_type']}")
    return ", ".join(parts)


@primitive(
    name="summarize-submit-plan",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli="hpc-agent summarize-submit-plan --spec <path>",
    agent_facing=True,
)
def summarize_submit_plan(spec: dict[str, Any]) -> dict[str, Any]:
    """Render a canonical pre-submit confirmation summary for *spec*.

    *spec* is a fully-resolved ``submit_flow.input.json`` shape (the
    output of :func:`build_submit_spec`, or a dict assembled by hand
    by an external orchestrator).

    Returns ``{headline, body, confirm_prompt}``:

    * ``headline`` — single-sentence "ready to submit" line.
    * ``body`` — multi-line breakdown: profile, cluster, total_tasks,
      backend, script, runtime, campaign_id, resources, canary,
      partial_ok. Order is stable across calls.
    * ``confirm_prompt`` — the literal "Confirm? [y/N]" string the
      slash command should ask, or a ``--no-confirm`` advisory when
      the spec carries an obviously-large total_tasks (>1000) so the
      agent surfaces the magnitude before submitting.

    Raises :class:`errors.SpecInvalid` only on missing required keys
    (profile, cluster, total_tasks, backend); the primitive does NOT
    re-validate against the full schema — that's
    ``build-submit-spec``'s job.
    """
    if not isinstance(spec, dict):
        raise errors.SpecInvalid(f"spec must be a dict, got {type(spec).__name__}")
    for required in ("profile", "cluster", "total_tasks", "backend"):
        if required not in spec:
            raise errors.SpecInvalid(f"spec missing required key {required!r}")

    profile = str(spec["profile"])
    cluster = str(spec["cluster"])
    total_tasks = int(spec["total_tasks"])
    backend = str(spec["backend"])
    script = str(spec.get("script") or "")
    runtime = spec.get("runtime")
    campaign_id = spec.get("campaign_id") or ""
    canary = bool(spec.get("canary", True))
    partial_ok = bool(spec.get("partial_ok", False))
    ssh_target = str(spec.get("ssh_target") or "")
    remote_path = str(spec.get("remote_path") or "")

    resources_str = _format_resources(spec)

    headline = (
        f"Ready to submit profile={profile!r} to cluster={cluster!r}: "
        f"{total_tasks} tasks via {backend}."
    )

    body_lines: list[str] = [
        f"profile:      {profile}",
        f"cluster:      {cluster}",
        f"total_tasks:  {total_tasks}",
        f"backend:      {backend}",
        f"script:       {script}",
        f"ssh_target:   {ssh_target}",
        f"remote_path:  {remote_path}",
    ]
    if runtime:
        body_lines.append(f"runtime:      {runtime}")
    if campaign_id:
        body_lines.append(f"campaign_id:  {campaign_id}")
    if resources_str:
        body_lines.append(f"resources:    {resources_str}")
    body_lines.append(f"canary:       {'on' if canary else 'off'}")
    if partial_ok:
        body_lines.append("partial_ok:   on")

    # Magnitude advisory: surfaces the task count up front when the
    # user is about to launch a >1000-task array, matching the slash
    # command's existing "confirm large submit" prose.
    if total_tasks > 1000:
        confirm_prompt = f"This will produce {total_tasks} tasks (>1000). Confirm? [y/N]"
    else:
        confirm_prompt = "Confirm? [y/N]"

    return {
        "headline": headline,
        "body": "\n".join(body_lines),
        "confirm_prompt": confirm_prompt,
    }
