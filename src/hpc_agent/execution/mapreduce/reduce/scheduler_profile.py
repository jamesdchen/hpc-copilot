"""Scheduler-profile RESOLVER (Phase 3).

Extracted from :mod:`status` for navigability and shape: that module had
regrown past 1,400 LOC (its own rollup extraction happened at 752 LOC),
and this ~240-line block is the one part of it that is NOT reduce-phase
work. :func:`~hpc_agent.execution.mapreduce.reduce.status.detect_scheduler`
answers "which family?" (a string); the resolver here answers the richer
question "give me the concrete, validated, REGISTERED SchedulerProfile to
drive this run, and PIN it so every later reader agrees". It seeds from
the spine's golden profiles, honours an operator's pinned
``scheduler_profile`` from clusters.yaml, registers the result via the
backend registry, and (for an unknown family) defines the LLM-authoring +
canary-validate seam.

This is *infra/backends-shaped*, not reduce-phase-shaped — its contract
tests already live under ``tests/infra/backends/`` — and it is a candidate
to migrate under ``hpc_agent.infra.backends`` once the deployed reporter's
module-load footprint no longer has to stay minimal. It lives here (a
sibling of :mod:`status`, like :mod:`rollup`) for now so that
``status``'s top-level import graph does not eagerly pull the
``infra.backends`` package: every spine import below is LAZY (inside the
function that needs it) exactly as it was in ``status``, preserving the
deployed-reporter closure invariant that ``status`` documents (it ships
without ``infra/io``; the pin writer runs client-side only).

The spine module ``hpc_agent.infra.backends.profile`` (SchedulerProfile,
SLURM_PROFILE, SGE_PROFILE) and ``register_profile`` may not exist yet
during the parallel refactor; everything here imports them lazily and
raises a clear error only on the paths that actually need them.

The public entry points (:func:`resolve_scheduler_profile`,
:func:`pin_scheduler_profile`) are re-exported from :mod:`status` so the
``hpc_agent.execution.mapreduce.reduce.status`` import path continues to
hold.
"""

from __future__ import annotations

__all__ = [
    "pin_scheduler_profile",
    "resolve_scheduler_profile",
]

import json
from pathlib import Path

_KNOWN_SCHEDULER_FAMILIES = frozenset({"slurm", "sge", "pbspro", "torque"})


def _golden_profile_for_family(family: str):
    """Return the spine's golden ``SchedulerProfile`` for a known family.

    ``family`` is one of :data:`_KNOWN_SCHEDULER_FAMILIES`
    (slurm / sge / pbspro / torque). Imports the spine lazily so this module
    stays importable before the spine lands. Raises ``NotImplementedError``
    if the spine isn't present yet (this path is only reached for a known
    family, where a golden profile is expected to exist), and ``ValueError``
    for an unrecognised family.
    """
    try:
        from hpc_agent.infra.backends.profile import (
            PBSPRO_PROFILE,
            SGE_PROFILE,
            SLURM_PROFILE,
            TORQUE_PROFILE,
        )
    except ImportError as exc:  # pragma: no cover — spine not present yet
        raise NotImplementedError(
            "spine module hpc_agent.infra.backends.profile is not available "
            "yet; golden profiles (SLURM/SGE/PBSPRO/TORQUE) are required "
            "to resolve a known scheduler family."
        ) from exc
    golden = {
        "slurm": SLURM_PROFILE,
        "sge": SGE_PROFILE,
        "pbspro": PBSPRO_PROFILE,
        "torque": TORQUE_PROFILE,
    }
    fam = family.strip().lower()
    try:
        return golden[fam]
    except KeyError:
        raise ValueError(f"no golden profile for scheduler family {family!r}") from None


def _register(profile):
    """Register *profile* with the backend registry (idempotent).

    Thin lazy wrapper over the spine's ``register_profile`` so callers in
    this module don't repeat the import dance. ``register_profile`` keys on
    ``profile.name`` and is documented as idempotent, so calling it again
    for an already-registered profile is a no-op rebind. Returns the
    backend class ``register_profile`` produced, or ``None`` if the spine
    isn't present yet.
    """
    try:
        from hpc_agent.infra.backends import register_profile
    except ImportError:  # pragma: no cover — spine not present yet
        # TODO(Phase-3): spine's register_profile not present during the
        # parallel refactor. Skip registration; the deterministic resolve +
        # pin still works so downstream readers can re-resolve later.
        return None
    return register_profile(profile, remote=True)


def pin_scheduler_profile(meta_path: str | Path, profile) -> None:
    """PIN a resolved ``SchedulerProfile`` into ``experiment_meta.json``.

    Writes ``profile.to_dict()`` under the top-level key
    ``"scheduler_profile"`` in the ``experiment_meta.json`` at *meta_path*,
    merging into any existing content (so the ``backend`` hint that
    ``detect_scheduler`` reads is preserved). This is the durable record
    that makes the resolved profile authoritative for every later reader of
    the experiment — status, recovery, aggregation — instead of each one
    re-detecting and possibly disagreeing.

    *meta_path* may point either at the ``experiment_meta.json`` file
    itself or at the directory that should contain it; a directory is
    joined with the canonical filename. The parent directory is created if
    needed. A malformed pre-existing file is treated as empty rather than
    crashing the pin.

    NOTE (ownership): nothing in this package currently *writes*
    ``experiment_meta.json`` — it is only read (by ``detect_scheduler``).
    The experiment-setup owner that materialises that file must call this
    helper (or write the ``scheduler_profile`` key itself) right after it
    resolves the profile. See the resolver docstring and the agent report.
    """
    p = Path(meta_path)
    if p.is_dir() or (not p.suffix and not p.exists()):
        p = p / "experiment_meta.json"
    existing: dict = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8")) or {}
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing["scheduler_profile"] = profile.to_dict()
    # Keep the legacy ``backend`` family hint in sync so the cheap
    # detect_scheduler substring path still agrees with the pinned profile.
    family = getattr(profile, "family", None) or getattr(profile, "name", None)
    if isinstance(family, str) and family:
        existing.setdefault("backend", family)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic + durable: a torn experiment_meta.json reads as absent, silently
    # dropping the profile pin (and the merged ``backend`` hint) that every later
    # reader depends on to avoid the sacct heuristic (bug-sweep #61, generator
    # G12). indent=2/sort_keys=True bytes are identical to the prior write.
    # Imported lazily: this module ships in the DEPLOYED reporter closure,
    # which does not carry infra/io — the pin writer runs client-side only.
    from hpc_agent.infra.io import atomic_write_json

    atomic_write_json(p, existing)


def _read_pinned_profile_dict(result_dir: str | Path | None) -> dict | None:
    """Return a previously-pinned ``scheduler_profile`` dict, if any.

    Walks *result_dir* and its ancestors for ``experiment_meta.json`` (the
    same search ``detect_scheduler`` uses) and returns the stored
    ``scheduler_profile`` mapping, or ``None`` when absent/unreadable.
    """
    if result_dir is None:
        return None
    candidate: Path | None = Path(result_dir)
    seen: set[Path] = set()
    while candidate is not None and candidate not in seen:
        seen.add(candidate)
        meta_path = candidate / "experiment_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                pinned = meta.get("scheduler_profile")
                if isinstance(pinned, dict):
                    return pinned
            except (json.JSONDecodeError, OSError):
                pass
            return None
        parent = candidate.parent
        candidate = parent if parent != candidate else None
    return None


def _author_profile_for_unknown_family(
    scheduler: str,
    *,
    cfg: dict | None = None,
    result_dir: str | Path | None = None,
    probe=None,
    llm=None,
):
    """An unrecognised scheduler family has no curated grammar — fail loudly.

    The framework ships curated profiles for slurm / sge / pbspro / torque,
    selected deterministically by detection. A scheduler outside those
    families is NOT auto-authored at runtime: a synthesised profile would
    have no fast, reliable verifier, and the curated families already cover
    the common ground. The supported escape hatches are *data* (pin a
    ``scheduler_profile`` in clusters.yaml) or *code* (add a curated family
    + engine grammar). The ``probe``/``llm`` parameters are retained for
    signature back-compat but are unused.
    """
    from hpc_agent import errors as _errors

    raise _errors.SpecInvalid(
        f"scheduler {scheduler!r} is not a known family "
        "(slurm/sge/pbspro/torque) and no 'scheduler_profile' is pinned. "
        "Pin a SchedulerProfile dict in clusters.yaml, or add a curated "
        "family (engine grammar) — unknown schedulers are not auto-authored."
    )


def _is_golden_profile(profile) -> bool:
    """True when *profile* is just the unmodified golden default for its family.

    A standard slurm/sge cluster resolves to the golden profile, which has
    nothing worth persisting — ``detect_scheduler`` already agrees via the
    cheap ``backend`` hint, and writing the golden script bodies would bloat
    ``clusters.yaml`` / ``experiment_meta.json``. Only *custom* profiles get
    pinned.
    """
    try:
        return bool(profile == _golden_profile_for_family(profile.family))
    except (ValueError, NotImplementedError, AttributeError):
        return False


def _pin_resolved(profile, *, result_dir, cluster_name) -> None:
    """Persist a freshly-resolved CUSTOM profile per the unified rule.

    Always pins to ``experiment_meta.json`` under *result_dir* (the per-run
    source of truth that recover/status read); ADDITIONALLY caches it into a
    writable ``clusters.yaml`` entry when *cluster_name* is given (so the
    next experiment on that cluster skips re-resolution). Both writes are
    best-effort. Golden (unmodified) profiles are skipped — there is nothing
    custom to record.
    """
    import contextlib

    if _is_golden_profile(profile):
        return
    if result_dir is not None:
        with contextlib.suppress(OSError):
            pin_scheduler_profile(result_dir, profile)
    if cluster_name:
        try:
            from hpc_agent.infra.clusters import write_back_scheduler_profile

            write_back_scheduler_profile(cluster_name, profile.to_dict())
        except Exception:  # noqa: BLE001 — caching is strictly best-effort
            pass


def resolve_scheduler_profile(
    scheduler: str,
    *,
    cfg: dict | None = None,
    result_dir: str | Path | None = None,
    cluster_name: str | None = None,
    probe=None,
    llm=None,
):
    """Resolve, register, and (when possible) PIN a ``SchedulerProfile``.

    The single entry point Phase 3 exposes for turning a scheduler name +
    cluster config into the concrete profile that drives a run. Resolution
    order:

    1. **Operator pin in cfg** — if *cfg* carries a ``scheduler_profile``
       dict (a clusters.yaml entry, already round-trip-validated by
       ``ClusterConfig``), build it via ``SchedulerProfile.from_dict``,
       register it, and return it. This wins over the golden family
       profile so an operator can override/augment the defaults. NO LLM.
    2. **Previously pinned in experiment_meta.json** — if no cfg pin but a
       prior resolve wrote a ``scheduler_profile`` under *result_dir*,
       rehydrate it via ``from_dict`` + register + return. Keeps every
       later reader agreeing with the first resolve. NO LLM.
    3. **Known family** (``slurm``/``sge``) with no pin — return the
       spine's golden profile and register it (idempotent). NO LLM.
    4. **Unknown family** with no pin — raises ``SpecInvalid``. Unknown
       schedulers are NOT auto-authored at runtime; the escape hatches are
       *data* (pin a ``scheduler_profile`` in clusters.yaml) or *code* (add
       a curated family). See ``_author_profile_for_unknown_family``.

    When a profile is resolved by a non-pinned path (3 or 4), the unified
    pin rule applies: it is ALWAYS written to ``experiment_meta.json`` under
    *result_dir* (the per-run source of truth recover/status read), and
    ADDITIONALLY cached into a writable ``clusters.yaml`` entry when
    *cluster_name* is given (so the next experiment skips re-resolution).
    Both writes are best-effort.

    Args:
      scheduler: the scheduler family/name (e.g. ``"slurm"``).
      cfg: the cluster config dict for this run (may carry the pin).
      result_dir: a per-run/per-task dir used to locate (and write)
        ``experiment_meta.json`` for the pin.
      cluster_name: the clusters.yaml key for this cluster; when set, the
        resolved profile is cached back into the writable clusters.yaml.
      probe: optional callable for live binary detection (unknown-family
        seam only).
      llm: optional LLM handle for profile authoring (unknown-family seam
        only).

    Returns:
      The resolved ``SchedulerProfile`` (registered under its ``name``).
    """
    cfg = cfg or {}
    fam = (scheduler or "").strip().lower()

    # 1. Operator pin in cfg wins outright.
    cfg_pin = cfg.get("scheduler_profile")
    if isinstance(cfg_pin, dict) and cfg_pin:
        from hpc_agent.infra.backends.profile import SchedulerProfile

        profile = SchedulerProfile.from_dict(cfg_pin)
        _register(profile)
        return profile

    # 2. Previously pinned in experiment_meta.json (durable agreement).
    meta_pin = _read_pinned_profile_dict(result_dir)
    if isinstance(meta_pin, dict) and meta_pin:
        from hpc_agent.infra.backends.profile import SchedulerProfile

        profile = SchedulerProfile.from_dict(meta_pin)
        _register(profile)
        return profile

    # 3. Known family with no pin → deterministic golden seed.
    if fam in _KNOWN_SCHEDULER_FAMILIES:
        profile = _golden_profile_for_family(fam)
        _register(profile)
        _pin_resolved(profile, result_dir=result_dir, cluster_name=cluster_name)
        return profile

    # 4. Unknown family → live LLM-authoring + canary seam, then pin.
    profile = _author_profile_for_unknown_family(
        scheduler, cfg=cfg, result_dir=result_dir, probe=probe, llm=llm
    )
    _pin_resolved(profile, result_dir=result_dir, cluster_name=cluster_name)
    return profile
