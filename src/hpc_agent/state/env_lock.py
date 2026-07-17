"""The environment-lock snapshot — U-ENV1 (reproducibility program, 2026-07-17).

The #2 crisis gap: **environment drift is invisible today.** ``env_hash``
(:func:`hpc_agent.state.run_sha.compute_env_hash`) captures the DECLARED
activation inputs (modules / conda source / runtime) at submit time, but it is
never COMPARED in any gate, and it says nothing about the RESOLVED environment —
a conda env mutated under the same name, or a silent package bump, reproduces
"clean".

This module is the RESOLVED-environment leg. The canary (which already runs a
real task under the run's env) emits a resolved-environment SNAPSHOT — the
``pip freeze`` lines / the lockfile / ``python -V`` + key package versions — and
this reduces it to an additive ``env_lock_sha`` stamped on the run sidecar. A
later reproduction that resolves a DIFFERENT env is DISCLOSED, never gated
(echoing the DATA leg's ``_data_drift_disclosure``): reproducing under a bumped
dependency is a legitimate, interesting reproduction; the machinery NAMES the
environment as the moved dimension rather than mislabeling it nondeterminism.

Agnosticism (the boundary test): core hashes OPAQUE snapshot text and never
parses a package format or names a library — ``pip freeze`` / a lockfile / a
``python -V`` blob are all opaque line-sets keyed by an opaque ``source`` tag.

Pure — no SSH, no ``_wire`` import, no scheduler. The SSH capture that FEEDS a
snapshot in lives in the ops layer
(:mod:`hpc_agent.ops.submit.env_lock_capture`); this module only resolves +
hashes + discloses opaque text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "SOURCE_ORDER",
    "STATUS_CAPTURED",
    "STATUS_COULD_NOT_CAPTURE",
    "EnvLockSnapshot",
    "env_lock_sha",
    "resolve_env_lock",
    "env_drift_disclosure",
]

#: The resolution PREFERENCE order for the resolved-environment snapshot. The
#: canary tries each in turn and the FIRST that resolves non-empty wins — a
#: ``pip freeze`` (the fullest resolved dependency set) is preferred to a
#: lockfile, which is preferred to a ``python -V`` + key-package fallback. The
#: chosen ``source`` tag is folded into the sha so two envs whose ``pip freeze``
#: and ``python_env`` blobs happen to normalize identically never collide.
SOURCE_ORDER: tuple[str, ...] = ("pip_freeze", "lockfile", "python_env")

#: The env-lock capture verdicts stamped on the sidecar's ``env_lock_status``.
#: ``captured`` — a snapshot resolved and ``env_lock_sha`` was stamped;
#: ``could_not_capture`` — the env could NOT be resolved (no snapshot). The
#: second is the honest could-not-capture record the no-silent-caps rule demands
#: (never a silent skip): a later reproduction reads it as "environment identity
#: unknown", disclosed.
STATUS_CAPTURED = "captured"
STATUS_COULD_NOT_CAPTURE = "could_not_capture"


@dataclass(frozen=True)
class EnvLockSnapshot:
    """The reduced result of resolving a run's environment snapshot.

    ``resolved`` True → a snapshot resolved: ``source`` names which
    (:data:`SOURCE_ORDER` member) and ``sha`` is the additive ``env_lock_sha``.
    ``resolved`` False → the env could NOT be resolved: ``source``/``sha`` are
    ``None`` and ``status`` is :data:`STATUS_COULD_NOT_CAPTURE` — an honest
    could-not-capture, never a silent skip. ``detail`` is a one-line
    human-readable summary in both cases.
    """

    resolved: bool
    source: str | None
    sha: str | None
    status: str
    detail: str


def _normalize_lines(text: str) -> list[str]:
    """Split *text* into a stable, order-insensitive line-set for hashing.

    Strips each line, drops blanks and whole-line ``#`` comments (a lockfile's
    provenance headers are not environment identity), and sorts — so the same
    resolved set of packages hashes the same regardless of the emitter's line
    order. Opaque throughout: no line is parsed for package meaning.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return sorted(lines)


def env_lock_sha(source: str, payload: str) -> str:
    """Return the additive ``env_lock_sha`` over a resolved-environment snapshot.

    Hashes a canonical JSON object ``{"source": <source>, "lines": <sorted
    normalized lines>}`` — sorted keys, compact separators — so the digest is
    stable across whitespace / line-order churn and folds in the ``source`` tag
    (a ``pip_freeze`` blob and a ``python_env`` blob that normalize identically
    still get distinct shas). Returns a 64-char lowercase hex string.

    Raises :class:`ValueError` when *payload* normalizes to NO lines — an empty
    snapshot is not a resolved environment; the caller must treat that as
    could-not-capture rather than minting a sha over nothing.
    """
    lines = _normalize_lines(payload)
    if not lines:
        raise ValueError("env_lock_sha: empty snapshot — nothing to hash")
    canonical = json.dumps(
        {"source": source, "lines": lines}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def resolve_env_lock(
    *,
    pip_freeze: str | None = None,
    lockfile: str | None = None,
    python_env: str | None = None,
) -> EnvLockSnapshot:
    """Resolve the FIRST available snapshot per :data:`SOURCE_ORDER` → a reduced sha.

    Each argument is the raw text a capture produced for that source (``None`` or
    blank = that source did not resolve). The first source in
    :data:`SOURCE_ORDER` whose text yields a non-empty normalized line-set wins;
    the rest are ignored. When NONE resolves, the return is a could-not-capture
    :class:`EnvLockSnapshot` (``resolved=False``, ``status=could_not_capture``) —
    the honest no-silent-caps record, never a raise.

    * ``pip_freeze`` — the run env's ``pip freeze`` (or ``uv pip freeze``) output.
    * ``lockfile`` — a lockfile's raw text (``uv.lock`` / a ``requirements`` lock).
    * ``python_env`` — a ``python -V`` line plus key-package versions, the minimal
      fallback when neither of the above is available.
    """
    candidates: dict[str, str | None] = {
        "pip_freeze": pip_freeze,
        "lockfile": lockfile,
        "python_env": python_env,
    }
    for source in SOURCE_ORDER:
        text = candidates.get(source)
        if not text or not text.strip():
            continue
        try:
            sha = env_lock_sha(source, text)
        except ValueError:
            continue
        return EnvLockSnapshot(
            resolved=True,
            source=source,
            sha=sha,
            status=STATUS_CAPTURED,
            detail=f"environment resolved via {source} (env_lock {sha[:12]})",
        )
    return EnvLockSnapshot(
        resolved=False,
        source=None,
        sha=None,
        status=STATUS_COULD_NOT_CAPTURE,
        detail=(
            "environment could not be resolved (no pip freeze / lockfile / python "
            "version captured) — env_lock not stamped, disclosed at verify"
        ),
    )


def env_drift_disclosure(recorded: str | None, current: str | None) -> dict[str, Any]:
    """DISCLOSE (never gate) how a reproduction's env_lock compares to the original's.

    Shaped after the DATA leg's
    :func:`hpc_agent.ops.reproduce_run._data_drift_disclosure`: a verdict-FREE
    projection the human reads, NEVER a refusal. Reproducing under a bumped
    dependency set is a legitimate reproduction — the machinery names the
    environment as the moved dimension rather than calling it nondeterminism.

    * both env_locks present and equal → ``status="match"``;
    * both present and different → ``status="drifted"`` (the silent-package-bump /
      mutated-conda-env class, now attributed);
    * either side absent (an old sidecar with no field, or a could-not-capture
      canary) → ``status="unknown"`` — disclosed, never blocking, never fabricated.

    Returns ``{"status", "recorded", "current"}`` with the two shas echoed
    verbatim (``recorded`` = the original's, ``current`` = the reproduction's).
    """
    rec = str(recorded) if recorded else None
    cur = str(current) if current else None
    if rec is None or cur is None:
        status = "unknown"
    elif rec == cur:
        status = "match"
    else:
        status = "drifted"
    return {"status": status, "recorded": rec, "current": cur}
