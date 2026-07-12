"""Env-var readers, parsed one way each so no two call sites drift apart.

Two conventions live here, one per reader:

* :func:`env_flag` — the single BOOLEAN truthiness convention for operator
  flags (``HPC_AGENT_ALWAYS_CANARY``, the decode-schema gates, …): unset or
  blank means *default*; any explicit value is parsed strictly — only
  ``1``/``true``/``yes``/``on`` enable, everything else (``0``, ``false``, …)
  disables, so a documented off-switch works on a default-on flag. Extracted
  so the gates cannot drift apart (``_decode_schema_enabled`` and
  ``_always_canary`` previously each inlined this parse).
* :func:`env_actor` — the opaque multi-human ACTOR slug (``HPC_ACTOR``,
  ``docs/design/multi-human.md`` MH8): unset/blank/not-a-valid-slug → ``None``
  (today's single-actor world); a filesystem-safe slug → itself. The value is
  harness-asserted, never verified (the attribution-honesty tier), and it must
  arrive from OUTSIDE the model's tool surface — an env var, never a CLI flag
  or spec field — exactly like the utterance text it attributes.
"""

from __future__ import annotations

import os

__all__ = ["active_env_overrides", "env_actor", "env_flag"]

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag(var: str, *, default: bool = False) -> bool:
    """Whether the boolean env flag *var* is on (unset/blank → *default*)."""
    value = os.environ.get(var, "").strip()
    if not value:
        return default
    return value.lower() in _TRUTHY


def active_env_overrides() -> dict[str, str]:
    """Every ``HPC_*`` env var currently exported, verbatim — pure disclosure.

    The env-vs-record drift seat (run-12 finding 24 addendum): an override like
    ``HPC_SSH_ENGINE=asyncssh`` can outlive the session that set it and silently
    reroute every ssh call while the durable record says it was retired. Every
    judgment surface echoes the live environment so the drift is visible in each
    brief; it never judges the values — an unexpected entry IS the finding.

    Superset disclosure: returns ALL ``HPC_*`` variables (sorted), never a
    hardcoded allow-list — a stray, never-anticipated override is exactly the
    one worth surfacing. Empty when no ``HPC_*`` variable is set. This is THE
    one definition; doctor, status-snapshot, net-triage and campaign briefs all
    consume it so no two surfaces can drift on what "the active env" means.
    """
    return {k: v for k, v in sorted(os.environ.items()) if k.startswith("HPC_")}


def env_actor(var: str = "HPC_ACTOR") -> str | None:
    """The multi-human actor slug from *var*, or ``None`` when unattributed.

    Returns ``None`` — today's single-actor path, byte-identical — when *var*
    is unset, blank, or NOT a filesystem-safe slug. A broken/invalid actor
    config degrades to the unattributed tier rather than wedging anything (the
    same fail-open posture as capture): an invalid slug is not an error, just
    an absent attribution. A valid slug is returned verbatim; core compares it
    by identity and NEVER verifies who set it (harness-asserted attribution).

    Slug shape is the shared filesystem-safe tag class
    (:func:`hpc_agent.state.scopes.validate_tag`) because the slug becomes a
    PATH SEGMENT in the actor-suffixed utterance locator (MH2) — load-bearing,
    not stylistic.
    """
    value = os.environ.get(var, "").strip()
    if not value:
        return None
    from hpc_agent import errors
    from hpc_agent.state.scopes import validate_tag

    try:
        validate_tag(value)
    except errors.SpecInvalid:
        return None
    return value
