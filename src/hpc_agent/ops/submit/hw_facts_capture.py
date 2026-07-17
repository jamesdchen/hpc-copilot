"""The canary's HARDWARE-facts capture — U-HW1 (reproducibility program gap #5).

The #5 crisis gap's capture half. Two runs can differ because of the MACHINE —
the node, the CPU generation, the scheduler's placement — and today that variance
is invisible (surfaced only as the determinism fingerprint's n=2
``same_submission`` caveat, never RECORDED). This resolves the placement facts
the dispatcher ALREADY emitted into the canary's per-task ``_runtime.json``
(``node`` / ``cpu_model`` / ``partition``) and reduces them to an additive
``hw_sha`` stamped on the MAIN run's sidecar
(:func:`hpc_agent.state.runs.stamp_run_sidecar_hw_facts`).

**Zero new round-trip (the contrast with the env-lock leg).** Env-lock resolves a
snapshot over a fresh SSH exec; hardware facts need NO new fetch — the dispatcher
writes ``_runtime.json`` next to the task-0 summary, and the double-canary
fingerprint pull already rsyncs that dir home (its ``include`` was widened by the
one filename). This capture only READS the pulled file. That is why the load seam
is a plain file read, not an SSH fetch: the facts already came home.

BEST-EFFORT by contract: capturing hardware facts must NEVER fail a submit whose
canary verified. Every failure (missing/torn runtime read, empty facts, a
pre-U-HW1 wheel that wrote no facts) degrades to a ``could_not_capture`` stamp,
disclosed later at verify/reproduce time. The runtime LOAD is an injected seam
(*load*) so the pure reduce + stamp path is unit-testable without a cluster.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from hpc_agent.state.hw_facts import HwFacts, resolve_hw_facts
from hpc_agent.state.runs import stamp_run_sidecar_hw_facts

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "RUNTIME_SIDECAR_NAME",
    "RuntimeFactsLoad",
    "facts_from_runtime",
    "capture_and_stamp_hw_facts",
]

#: The per-task runtime sidecar the dispatcher writes into each result dir
#: (``execution/mapreduce/dispatch.py`` — kept in lock-step by name only; the
#: dispatcher ships standalone and cannot import this). It carries the placement
#: facts; the fingerprint pull brings it home alongside the task-0 summary.
# MIRROR: execution/mapreduce/dispatch.py::main _runtime.json literal pinned-by tests/ops/submit/test_hw_facts_capture.py::test_runtime_sidecar_name_mirrors_dispatcher  # noqa: E501
RUNTIME_SIDECAR_NAME = "_runtime.json"

#: The dispatcher's ``_runtime.json`` field → the ``state.hw_facts.FACT_KEYS``
#: vocabulary. ``node`` = the exec hostname; ``cpu_model`` = the CPU generation;
#: ``partition`` = the scheduler-visible placement (SLURM partition / PBS queue).
#: A field the runtime record did not carry is simply absent — the reducer drops
#: empty facts, so a partial ``_runtime.json`` yields honest partial facts.
_RUNTIME_FACT_MAP: dict[str, str] = {
    "node": "node",
    "cpu_model": "cpu_model",
    "partition": "partition",
}

#: A load resolves the raw ``_runtime.json`` mapping for a canary (or ``None`` if
#: it did not come home). Injected so the resolve + stamp path is testable without
#: a real pull.
RuntimeFactsLoad = Callable[..., Mapping[str, Any] | None]


def facts_from_runtime(runtime: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project a ``_runtime.json`` payload onto the placement-fact vocabulary.

    Returns a ``{fact_key: raw_value}`` dict carrying only the fields the runtime
    record actually held (the reducer normalizes + drops empties downstream).
    A ``None`` / empty runtime yields ``{}`` → could-not-capture.
    """
    if not runtime:
        return {}
    out: dict[str, Any] = {}
    for rt_key, fact_key in _RUNTIME_FACT_MAP.items():
        if rt_key in runtime:
            out[fact_key] = runtime[rt_key]
    return out


def _load_pulled_runtime(experiment_dir: Path, canary_run_id: str) -> Mapping[str, Any] | None:
    """Default load: read the canary's ``_runtime.json`` from the fingerprint pulls dir.

    Locates the runtime sidecar the fingerprint pull landed under
    ``_aggregated/_fingerprints/_pulls/<canary_run_id>/`` (T3's ``pulls_dir``). A
    missing file (an old wheel, or a pull that did not include it) returns
    ``None`` → the caller records could-not-capture, never a raise.
    """
    from hpc_agent.state.fingerprint_store import pulls_dir

    area = pulls_dir(experiment_dir, canary_run_id)
    hits = sorted(p for p in area.rglob(RUNTIME_SIDECAR_NAME) if p.is_file())
    if not hits:
        return None
    data: Any = json.loads(hits[0].read_text(encoding="utf-8"))
    return data if isinstance(data, Mapping) else None


def capture_and_stamp_hw_facts(
    experiment_dir: Path,
    *,
    run_id: str,
    canary_run_id: str,
    load: RuntimeFactsLoad | None = None,
) -> HwFacts:
    """Reduce the canary's placement facts and stamp ``hw_sha`` on the run sidecar.

    Loads the canary's ``_runtime.json`` (via *load*, or the default pulls-dir
    read), projects it onto the placement-fact vocabulary, reduces it to an
    ``hw_sha``, and stamps it — with the capture ``hw_status`` — on ``run_id``'s
    sidecar. Returns the :class:`~hpc_agent.state.hw_facts.HwFacts`.

    BEST-EFFORT: never raises. A load failure or an empty/absent runtime record
    yields a ``could_not_capture`` :class:`HwFacts`, and the status is STILL
    stamped (no-silent-caps) so a later reproduction reads "hardware placement not
    captured" rather than a silent absence. A missing MAIN sidecar (nothing to
    stamp) is swallowed too — the facts are returned for the caller to log.
    """
    try:
        if load is not None:
            runtime = load(canary_run_id=canary_run_id)
        else:
            runtime = _load_pulled_runtime(experiment_dir, canary_run_id)
    except Exception:  # noqa: BLE001 — best-effort capture never fails the gate
        runtime = None
    facts = resolve_hw_facts(facts_from_runtime(runtime))
    # No sidecar to stamp (or an I/O hiccup) — the facts are still returned so the
    # caller can log; the sidecar simply carries no hw_facts this run.
    with contextlib.suppress(FileNotFoundError, OSError):
        stamp_run_sidecar_hw_facts(
            experiment_dir,
            run_id,
            hw_facts=facts.facts,
            hw_sha=facts.sha,
            hw_status=facts.status,
        )
    return facts
