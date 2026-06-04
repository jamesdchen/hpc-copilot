"""Central typed recovery registry — see ``recovery/registry.py``."""

from __future__ import annotations

from hpc_agent.recovery.registry import (
    PORTED_KINDS,
    REGISTRY,
    RecoveryKind,
    RecoveryMenu,
    RecoveryOption,
    all_kinds,
    menu_for,
    remediation_for,
)

__all__ = [
    "REGISTRY",
    "PORTED_KINDS",
    "RecoveryKind",
    "RecoveryMenu",
    "RecoveryOption",
    "all_kinds",
    "menu_for",
    "remediation_for",
]
