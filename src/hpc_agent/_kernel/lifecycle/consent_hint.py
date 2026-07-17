"""Pure composer for the OFFERED-CONSENT scoped-utterance hint.

Lives in ``_kernel.lifecycle`` — NOT in ``ops`` — because its sole caller is the
driver park in :mod:`hpc_agent._kernel.lifecycle.block_drive`, and ``_kernel`` is
a substrate role that must not import UP into ``ops`` (the layering + private-
cross-package import lints). The composer is pure string work (no I/O, no clock),
so it belongs at the layer that consumes it; ``ops.relay_render`` re-exports
``compose_approve_hint`` (a legal DOWNWARD reach) so the relay-verbatim home and
its tests keep importing it from there.

Generalizes the audit-view "To sign: type …" precedent
(:func:`hpc_agent.ops.notebook.audit_view._render_next_actions`): every parked
consent boundary presents a code-composed, ready-to-type utterance naming the
exact scope tokens the ``y`` grants. Display + verification-target only — nothing
auto-fills it, and a bare ``y`` remains accepted.
"""

from __future__ import annotations

from typing import Any

__all__ = ["brief_cluster", "compose_approve_hint"]


def _sha8(sha: str | None) -> str | None:
    """The 8-hex display prefix of a full spec sha, or ``None``."""
    return sha[:8] if isinstance(sha, str) and sha else None


def brief_cluster(brief: dict[str, Any]) -> str | None:
    """The cluster a block's brief carries, checked across the known shapes."""
    val = brief.get("cluster")
    if isinstance(val, str) and val:
        return val
    resolved = brief.get("resolved")
    if isinstance(resolved, dict):
        cluster = resolved.get("cluster")
        if isinstance(cluster, str) and cluster:
            return cluster
    resolve = brief.get("resolve")
    if isinstance(resolve, dict):
        submit_spec = resolve.get("submit_spec")
        if isinstance(submit_spec, dict):
            cluster = submit_spec.get("cluster")
            if isinstance(cluster, str) and cluster:
                return cluster
    return None


def compose_approve_hint(
    *,
    workflow: str | None,
    successor: str | None,
    run_id: str | None,
    cluster: str | None = None,
    next_spec_sha: str | None = None,
    standing: bool = False,
    bounds: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compose the ready-to-type scoped-consent utterance for a parked boundary.

    Derives — MECHANICALLY, deterministically — the exact consent utterance the
    human types to approve the successor the driver parked before. The scope the
    ``y`` implicitly grants is made VISIBLE by naming, in the utterance itself:

    * ``successor`` — the block the approval greenlights (``submit-s2`` / … /
      ``aggregate-run`` / a campaign boundary);
    * ``run_id`` — the run (or campaign) the approval is scoped to;
    * ``@<sha8>`` — the 8-hex pin of the CODE-COMPOSED complete successor spec the
      driver materialized + disclosed at park (run-14 #4 / R3). Naming it makes the
      utterance a verification-target: "the spec I approved provably equals the spec
      that executes" (R3 sha-pin) is legible in what the human typed. Absent a
      materialized sha (a Row-14 composition refusal, or a pre-materialization
      boundary) the pin is simply omitted — the utterance still names successor+run.

    For a STANDING consent (``standing=True`` — a campaign greenlight authorizing
    unattended async execution, or an overnight consent) the returned ``line`` also
    names the *bounds* the standing ``y`` grants: its ``expires_at`` (duration), any
    ``walltime_cap`` / ``budget_cap`` (spend caps), and whether a wake is armed — so
    a standing consent is never a bare `y` with invisible, unbounded scope.

    Returns a dict ``{utterance, scope_tokens, line, note, bare_ok, standing}`` or
    ``None`` when there is nothing scoped to name (no successor or no run_id) — the
    caller then simply omits the hint (a bare `y` boundary with no successor to pin).

    PURE + DETERMINISTIC: the same (successor, run_id, sha, bounds) always yields the
    same utterance. No I/O, no clock — the caller supplies every token.
    """
    if not (isinstance(successor, str) and successor):
        return None
    if not (isinstance(run_id, str) and run_id):
        return None

    sha8 = _sha8(next_spec_sha)
    tokens = ["y", successor, run_id]
    if sha8:
        tokens.append(f"@{sha8}")
    utterance = " ".join(tokens)

    scope_tokens: dict[str, Any] = {"response": "y", "next_block": successor, "run_id": run_id}
    if isinstance(cluster, str) and cluster:
        scope_tokens["cluster"] = cluster
    if isinstance(next_spec_sha, str) and next_spec_sha:
        scope_tokens["next_spec_sha"] = next_spec_sha

    pin_clause = (
        f" the code-composed {successor} spec (sha {sha8})"
        if sha8
        else f" the code-composed {successor} spec"
    )
    line = (
        f'To approve: type  "{utterance}"  — this names the successor ({successor}), '
        f'the run ({run_id}), and{pin_clause} you are approving. A bare "y" still works.'
    )
    note = (
        "code-composed scoped-consent utterance (OFFERED-CONSENT ruling): display + "
        "verification-target only. The human types it; nothing auto-fills it. Relay it "
        "VERBATIM. A bare 'y' remains accepted (backward compat)."
    )

    result: dict[str, Any] = {
        "utterance": utterance,
        "scope_tokens": scope_tokens,
        "line": line,
        "note": note,
        "bare_ok": True,
        "standing": bool(standing),
    }

    if standing:
        bound_clauses, bound_tokens = _standing_bound_clauses(bounds)
        if bound_tokens:
            scope_tokens["bounds"] = bound_tokens
        wf = workflow or ""
        subject = (
            "an unattended async campaign" if wf == "campaign" else "unattended overnight advances"
        )
        result["line"] = (
            f'To approve: type  "{utterance}"  — this is a STANDING consent for {subject}. '
            f"It names the successor ({successor}), the run ({run_id})"
            + (f", {pin_clause.strip()}" if sha8 else "")
            + (f", and its bounds ({'; '.join(bound_clauses)})" if bound_clauses else "")
            + '. A bare "y" still works, but grants the SAME unbounded scope invisibly — '
            "prefer the scoped form."
        )

    return result


def _standing_bound_clauses(bounds: dict[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
    """Human clauses + token map for a standing consent's bounds (duration/caps/wake).

    Reads the same fields the overnight-consent caps gate enforces
    (``ops/overnight.py::assert_consent_hard_caps`` / ``assert_wake_armed``):
    ``expires_at`` (the morning boundary), ``walltime_cap`` / ``budget_cap`` (the
    resource ceilings), and a ``wake`` marker. Purely descriptive — it names what the
    ``y`` grants, it never validates (the gate owns validation).
    """
    if not isinstance(bounds, dict):
        return [], {}
    clauses: list[str] = []
    tokens: dict[str, Any] = {}
    expires = bounds.get("expires_at")
    if isinstance(expires, str) and expires:
        clauses.append(f"until {expires}")
        tokens["expires_at"] = expires
    walltime = bounds.get("walltime_cap")
    if isinstance(walltime, (int, float)) and not isinstance(walltime, bool) and walltime > 0:
        clauses.append(f"≤ {walltime:g} wall-seconds")
        tokens["walltime_cap"] = walltime
    budget = bounds.get("budget_cap")
    if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
        clauses.append(f"≤ {budget:g} budget")
        tokens["budget_cap"] = budget
    wake = bounds.get("wake")
    if wake:
        clauses.append("wake armed")
        tokens["wake"] = wake
    return clauses, tokens
