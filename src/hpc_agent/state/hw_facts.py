"""Hardware / scheduler placement facts — U-HW1 (reproducibility program, gap #5).

The #5 crisis gap: **two runs can differ because of the MACHINE** — the node, the
CPU generation, the scheduler's placement — and today that is invisible. The
determinism fingerprint only ever surfaced it as the n=2 ``same_submission``
caveat (the "same-node correlated samples" failure mode,
:mod:`hpc_agent.state.determinism`), never RECORDED, so unexplained fingerprint
variance could never be ATTRIBUTED to hardware.

This module is the placement-facts leg, shaped exactly after the env-lock leg
(:mod:`hpc_agent.state.env_lock`). The task already emits its placement facts —
the dispatcher's per-task ``_runtime.json`` carries the exec ``node``, the
``cpu_model`` and the scheduler ``partition`` — so nothing new is captured over
SSH; the facts ride the pull the fingerprint mint already runs. Here we reduce
those opaque facts to an additive ``hw_sha`` stamped on the run sidecar. A later
reproduction that resolved DIFFERENT hardware is DISCLOSED, never gated (echoing
the DATA and ENV legs): reproducing on a newer SKU is a legitimate reproduction;
the machinery NAMES the hardware as a candidate attribution for any metric
divergence rather than mislabeling it nondeterminism.

Agnosticism (the boundary test): core hashes OPAQUE fact strings and never
interprets a hostname, a CPU model, or a partition name — they are opaque values
keyed by the fixed :data:`FACT_KEYS` vocabulary.

Pure — no SSH, no ``_wire`` import, no scheduler. The capture that READS a
``_runtime.json`` and stamps the sidecar lives in the ops layer
(:mod:`hpc_agent.ops.submit.hw_facts_capture`); this module only normalizes +
hashes + discloses opaque facts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "FACT_KEYS",
    "STATUS_CAPTURED",
    "STATUS_COULD_NOT_CAPTURE",
    "HwFacts",
    "normalize_facts",
    "hw_sha",
    "resolve_hw_facts",
    "hw_drift_disclosure",
]

#: The fixed placement-fact vocabulary. Every fact is an OPAQUE string; core
#: never interprets one. ``node`` = the exec hostname the scheduler placed the
#: task on; ``cpu_model`` = the CPU generation (``/proc/cpuinfo`` model name);
#: ``partition`` = the scheduler-visible placement (SLURM partition / PBS queue).
#: A run that resolved none of these records could-not-capture; a run that
#: resolved a SUBSET records the subset (partial facts are honest, never faked).
FACT_KEYS: tuple[str, ...] = ("node", "cpu_model", "partition")

#: The hw-capture verdicts stamped on the sidecar's ``hw_status``.
#: ``captured`` — at least one placement fact resolved and ``hw_sha`` was stamped;
#: ``could_not_capture`` — NO fact resolved (an old wheel / a torn runtime read).
#: The second is the honest could-not-capture record the no-silent-caps rule
#: demands (never a silent skip): a later reproduction reads it as "hardware
#: placement unknown", disclosed.
STATUS_CAPTURED = "captured"
STATUS_COULD_NOT_CAPTURE = "could_not_capture"

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class HwFacts:
    """The reduced result of resolving a run's placement facts.

    ``resolved`` True → at least one fact resolved: ``facts`` is the normalized
    ``{fact_key: value}`` subset that was present, and ``sha`` is the additive
    ``hw_sha`` over it. ``resolved`` False → NO fact resolved: ``facts``/``sha``
    are ``None`` and ``status`` is :data:`STATUS_COULD_NOT_CAPTURE` — an honest
    could-not-capture, never a silent skip. ``detail`` is a one-line
    human-readable summary in both cases.
    """

    resolved: bool
    facts: dict[str, str] | None
    sha: str | None
    status: str
    detail: str


def _normalize_value(value: Any) -> str:
    """Reduce one raw fact value to a stable opaque string (or ``""`` if empty).

    Coerces to ``str``, strips, and collapses internal whitespace runs to a single
    space — so ``"Intel(R)  Xeon(R)   Gold"`` and ``"Intel(R) Xeon(R) Gold"`` hash
    the same regardless of the emitter's spacing. Opaque throughout: the value is
    never parsed for hardware meaning. A non-string / blank value normalizes to
    ``""`` (that fact did not resolve).
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return _WS_RE.sub(" ", text.strip())


def normalize_facts(raw: Mapping[str, Any]) -> dict[str, str]:
    """Project *raw* onto the :data:`FACT_KEYS` vocabulary, dropping empty facts.

    Returns a dict carrying ONLY the known fact keys whose normalized value is
    non-empty — a fact the runtime record did not carry (or carried blank) is
    simply absent, never a fabricated placeholder. Order-independent by
    construction (the sha canonicalizes with ``sort_keys``).
    """
    out: dict[str, str] = {}
    for key in FACT_KEYS:
        norm = _normalize_value(raw.get(key))
        if norm:
            out[key] = norm
    return out


def hw_sha(facts: Mapping[str, str]) -> str:
    """Return the additive ``hw_sha`` over a normalized placement-facts mapping.

    Hashes a canonical JSON object over the NORMALIZED facts — sorted keys,
    compact separators — so the digest is stable across whitespace / key-order
    churn. Returns a 64-char lowercase hex string.

    Raises :class:`ValueError` when *facts* is empty — no resolved fact is not a
    hardware identity; the caller must treat that as could-not-capture rather than
    minting a sha over nothing.
    """
    normalized = normalize_facts(facts)
    if not normalized:
        raise ValueError("hw_sha: no placement facts — nothing to hash")
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def resolve_hw_facts(raw: Mapping[str, Any] | None) -> HwFacts:
    """Reduce a raw placement-facts mapping to a normalized :class:`HwFacts`.

    *raw* is the ``{fact_key: value}``-shaped mapping a capture produced (e.g. the
    dispatcher's ``_runtime.json`` projected onto :data:`FACT_KEYS`). When at least
    one fact normalizes non-empty, a ``captured`` :class:`HwFacts` carries the
    normalized subset + its ``hw_sha``. When NONE resolves (``None`` / empty / all
    blank), the return is a could-not-capture :class:`HwFacts`
    (``resolved=False``, ``status=could_not_capture``) — the honest
    no-silent-caps record, never a raise.
    """
    normalized = normalize_facts(raw or {})
    if not normalized:
        return HwFacts(
            resolved=False,
            facts=None,
            sha=None,
            status=STATUS_COULD_NOT_CAPTURE,
            detail=(
                "hardware placement could not be resolved (no node / cpu_model / "
                "partition captured) — hw_sha not stamped, disclosed at verify"
            ),
        )
    sha = hw_sha(normalized)
    present = ", ".join(sorted(normalized))
    return HwFacts(
        resolved=True,
        facts=normalized,
        sha=sha,
        status=STATUS_CAPTURED,
        detail=f"hardware placement resolved ({present}; hw {sha[:12]})",
    )


def hw_drift_disclosure(
    recorded: str | None,
    current: str | None,
    *,
    recorded_facts: Mapping[str, Any] | None = None,
    current_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """DISCLOSE (never gate) how a reproduction's hardware compares to the original's.

    Shaped after the ENV/DATA legs (:func:`hpc_agent.state.env_lock.env_drift_disclosure`):
    a verdict-FREE projection the human reads, NEVER a refusal. Reproducing on a
    different node / CPU generation is a legitimate reproduction — the machinery
    names the hardware as a candidate ATTRIBUTION for any observed divergence,
    rather than calling it nondeterminism.

    * both hw_shas present and equal → ``status="match"`` (placement equivalent —
      strengthens a divergence signal: hardware is ruled OUT as the cause);
    * both present and different → ``status="drifted"`` (the machine moved — a
      candidate attribution for any metric divergence, OFFERED not asserted);
    * either side absent (an old sidecar, or a could-not-capture canary) →
      ``status="unknown"`` — disclosed, never blocking, never fabricated.

    Returns ``{"status", "recorded", "current", "delta"}``. ``recorded``/``current``
    echo the two shas verbatim. ``delta`` (only when both facts mappings are
    supplied and the shas drifted) NAMES the fact keys that differ — the opaque
    per-key attribution the receipt phrase surfaces (``["node"]``, ``["cpu_model",
    "node"]``, …); ``[]`` otherwise.
    """
    rec = str(recorded) if recorded else None
    cur = str(current) if current else None
    if rec is None or cur is None:
        status = "unknown"
    elif rec == cur:
        status = "match"
    else:
        status = "drifted"
    delta: list[str] = []
    if status == "drifted" and recorded_facts is not None and current_facts is not None:
        rn = normalize_facts(recorded_facts)
        cn = normalize_facts(current_facts)
        delta = sorted(k for k in FACT_KEYS if rn.get(k) != cn.get(k))
    return {"status": status, "recorded": rec, "current": cur, "delta": delta}
