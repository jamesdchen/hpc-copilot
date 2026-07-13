"""The harness-conformance kit — an executable proof of the harness contract.

This subpackage is the TCK / Web-Platform-Tests artifact for
``docs/internals/harness-contract.md``: a pytest test package SHIPPED IN THE
WHEEL and parameterized by a HARNESS ADAPTER, run as::

    pytest --pyargs hpc_agent.conformance --harness-adapter mypkg.adapter:build

A stranger's harness runs the kit against its own capability providers and
earns (or is refused) the named conformance verdict; our own side is pinned in
CI by the same kit (``docs/design/conformance-kit.md``, D-K5).

**Boundary (enforced, D-K1 / the enforcement map).** ``pytest`` is a runtime
requirement OF RUNNING THE KIT, never of core: the kit package imports pytest
only inside its own test modules and its ``conftest.py``; nothing in
``hpc_agent`` OUTSIDE ``conformance/`` may import from here, and
``conformance/adapter.py`` must import stdlib only (importable without pytest).

**Naming-collision note.** ``hpc_agent.state.conformance*`` (registration
conformance) is an UNRELATED subject; this package is the HARNESS kit.

K1 lands the skeleton — the adapter Protocol, the ``--harness-adapter``
loading + per-capability skip machinery + the report hook (``conftest.py``),
and the fixture-repo builder. The per-capability assertion modules
(``test_capability_*.py``, canonicalization, negotiation, attestation) land in
waves B/C.
"""

from __future__ import annotations

from hpc_agent.conformance.adapter import (
    CAP_BACKGROUNDING,
    CAP_RELAY_ENFORCEMENT,
    CAP_UTTERANCE_LOG,
    CAPABILITIES,
    DEGRADED_TIERS,
    EnforcementOutcome,
    HarnessAdapter,
    WakeEvent,
    declared_capabilities,
    skip_reason_for,
)

__all__ = [
    "CAPABILITIES",
    "CAP_BACKGROUNDING",
    "CAP_RELAY_ENFORCEMENT",
    "CAP_UTTERANCE_LOG",
    "DEGRADED_TIERS",
    "EnforcementOutcome",
    "HarnessAdapter",
    "WakeEvent",
    "declared_capabilities",
    "skip_reason_for",
]
