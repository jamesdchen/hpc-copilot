"""The pre-detach path gate — learn "hop dead, direct OK" at fire time, not from a corpse.

The 2026-07-30 night incident's third leg. ``submit-s2`` detaches by contract:
the greenlight gate and the drift guards fire SYNCHRONOUSLY, then a durable
background worker owns the canary poll ("The gate and drift guards already
passed synchronously before the detach"). That is right for a slow canary — and
wrong for a dead path. Two detached S2 workers died in ~16s each because the
configured ``ProxyJump`` hop was down, and the cause was only discoverable by a
human reading worker logs after the fact.

The rule this module adds: **whatever a detached worker will discover about the
path in its first seconds, the human learns synchronously at fire time.** Before
the detach, the run's OWN ssh target and the cluster's OWN activation are sensed
through :mod:`hpc_agent.infra.readiness_sensors`, and a discriminated failure
refuses in the synchronous response — naming the cause and, crucially, what the
DIRECT alternative did. Ten seconds, at the keyboard, instead of minutes and a
log dig.

Consult-first (``docs/design/s2-readiness.md`` pillars 1/3)
----------------------------------------------------------

The gate does NOT re-dial what is already known. It first CONSULTS the readiness
ledger under a freshness window (:func:`~hpc_agent.infra.readiness_sensors.consult_readiness`)
and senses only the legs that came back absent or stale. Today the ledger is
in-process, so the practical source of a fresh reading is an L1 sweep
(``net-triage``) that already ran in this invocation; when the durable standing
ledger lands, the same call reads its rows and this gate gets cheaper without
moving. That is the permanent shape: **consult, sense only the stale, then
assert.**

Fail-open, everywhere it matters
--------------------------------

A gate that is a diagnosis layer must never be the reason a healthy submit
refuses. An unresolvable route (``ssh -G`` missing / failing), a sensor that
could not run, or any unexpected error degrades to PASS with a disclosure — the
worker then proceeds exactly as it does today. Only a POSITIVE discriminated
failure refuses. ``HPC_S2_PATH_GATE=0`` disables the gate entirely.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.infra.readiness_sensors import (
    DEFAULT_FRESHNESS_WINDOW_SEC,
    PathReadiness,
    consult_readiness,
    path_remediation,
    read_path_readiness,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["GATE_ENV", "assert_path_clear_for_detach", "gate_enabled", "path_refusal_message"]

#: Set ``HPC_S2_PATH_GATE=0`` to disable the pre-detach path gate entirely.
GATE_ENV = "HPC_S2_PATH_GATE"


def gate_enabled() -> bool:
    """Whether the pre-detach path gate runs (default ON; ``HPC_S2_PATH_GATE=0`` opts out)."""
    return os.environ.get(GATE_ENV, "").strip() != "0"


def path_refusal_message(readiness: PathReadiness, *, run_id: str, verb: str) -> str:
    """The synchronous refusal text — the discriminated cause, named, at fire time.

    Leads with the cause vocabulary shared by ``net-triage``'s ``path_cause`` and
    the staging retry's exhaustion, so the human reads ONE word across all three
    surfaces, then the remediation that names what the direct alternative did.
    """
    return (
        f"{verb} refused BEFORE detaching run {run_id!r}: {readiness.cause} — "
        f"{readiness.sentence or 'the effective path did not verify'}. "
        f"{path_remediation(readiness)} "
        f"This check ran synchronously on purpose: a detached worker would have "
        f"discovered the same thing in its first seconds and died in a log you would "
        f"have had to go read (2026-07-30). Re-run once the path is fixed, or set "
        f"{GATE_ENV}=0 to detach anyway."
    )


def assert_path_clear_for_detach(
    host: str,
    *,
    run_id: str,
    ssh_target: str | None = None,
    activation: str | None = None,
    verb: str = "submit-s2",
    freshness_window_sec: float = DEFAULT_FRESHNESS_WINDOW_SEC,
    reader: Callable[..., PathReadiness] | None = None,
) -> PathReadiness | None:
    """Refuse the detach when THIS run's path is discriminated-dead; else return the read.

    Consult-first: a reading for *host* still inside *freshness_window_sec* is
    reused wholesale and no new connection is opened. Otherwise the stale/absent
    legs are sensed, including the preamble class when *activation* is supplied —
    the command class that actually failed in the field.

    Returns the :class:`PathReadiness` on PASS (so the caller can disclose what it
    learned) or ``None`` when the gate is disabled. Raises
    :class:`hpc_agent.errors.SshUnreachable` — the typed, ``retry_safe`` network
    envelope ``submit-s2`` already declares — on a positive discriminated failure.

    Fail-open on everything else: an unresolved route, a sensor that could not
    run, or any unexpected exception passes with a stderr disclosure. The healthy
    path is byte-identical to the pre-gate behaviour — nothing is added to the
    worker's own sequence, and a PASS returns without touching the spec.
    """
    if not gate_enabled() or not host.strip():
        return None

    fresh = consult_readiness(host, window_sec=freshness_window_sec)
    if fresh is not None:
        readiness = fresh
    else:
        sense = reader or read_path_readiness
        try:
            readiness = sense(
                host,
                ssh_target=ssh_target or host,
                activation=activation,
                freshness_window_sec=freshness_window_sec,
            )
        except Exception as exc:  # noqa: BLE001 — a diagnosis layer never blocks on itself
            print(
                f"[path-gate] readiness sensing for {host} could not run "
                f"({type(exc).__name__}: {exc}); proceeding to detach unguarded.",
                file=sys.stderr,
                flush=True,
            )
            return None

    if readiness.ok:
        return readiness
    raise errors.SshUnreachable(path_refusal_message(readiness, run_id=run_id, verb=verb))
