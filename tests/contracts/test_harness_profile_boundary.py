"""The harness-activation profile is MECHANISM, never AUTHORIZATION — pinned.

The load-bearing doctrine of the activation program (plan §5-R3 / premortem D6):
installing a profile grants ZERO trust. Two STRUCTURAL pins make the named
failure shape — a self-asserted ``capabilities:`` manifest some code reads as
truth — impossible by construction, rather than merely discouraged by prose:

1. **Frozen, CLOSED field set (D6a).** :class:`HarnessProfile` (and each
   descriptor) is a frozen dataclass whose field set is equality-pinned here, so
   adding ANY capability-shaped field (``capabilities`` / ``provides`` /
   ``grants`` / ``trust`` / ``conformant``) goes RED. The type cannot carry a
   self-assertion.
2. **Consumer-trace (D6b).** No module in the trust path (gates / verify /
   journal / conformance) imports the profile module or reads the profile /
   renderer / singleton names. "No code reads 'profile installed' as 'capability
   present'" is a fired AST test, not a sentence. Capability presence is proven
   only by BEHAVIOR (the conformance kit's ``declared == detected == behaved``)
   and read only from the DETECTED settings-seam
   (:mod:`hpc_agent.ops.harness_capabilities`).
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from hpc_agent.harness_profile import (
    HarnessProfile,
    HookDescriptor,
    McpServerDescriptor,
    StopMultiplexDescriptor,
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "hpc_agent"

# Names that would turn the profile from a mechanism DESCRIPTION into a
# self-asserted capability CLAIM — the exact forbidden failure shape (D6).
_FORBIDDEN_FIELD_SUBSTRINGS = ("capabilit", "provides", "grant", "trust", "conformant")

# The ONLY modules allowed to reference the profile: its own definition and the
# install ENGINE that renders it. The install path is NOT a trust surface — no
# gate reads it. When U-PROFILE-VERB lands, its query + wire modules are added
# here DELIBERATELY (a read verb reporting mechanism is still not a trust read).
_PROFILE_OWNER_MODULES = {
    "harness_profile.py",
    "agent_assets.py",
}

_PROFILE_NAMES = {"HarnessProfile", "ClaudeCodeProfile", "CLAUDE_CODE_PROFILE"}


# ── D6a: the closed, frozen field set ────────────────────────────────────────


def test_harness_profile_field_set_is_closed() -> None:
    """The profile's fields are EXACTLY the mechanism-description set — no more."""
    fields = {f.name for f in dataclasses.fields(HarnessProfile)}
    assert fields == {"hook_descriptors", "stop_hook", "mcp_server", "asset_package"}


def test_hook_descriptor_field_set_is_closed() -> None:
    """A hook descriptor carries needle + neutral event/matcher/prefilter only."""
    fields = {f.name for f in dataclasses.fields(HookDescriptor)}
    assert fields == {"needle", "event", "tool_class", "prefilter"}


@pytest.mark.parametrize(
    "profile_type",
    [HarnessProfile, HookDescriptor, StopMultiplexDescriptor, McpServerDescriptor],
)
def test_no_capability_shaped_field_exists(profile_type: type) -> None:
    """No field name reads as a self-asserted capability claim (the named D6 shape)."""
    for f in dataclasses.fields(profile_type):
        lowered = f.name.lower()
        offenders = [s for s in _FORBIDDEN_FIELD_SUBSTRINGS if s in lowered]
        assert not offenders, (
            f"{profile_type.__name__}.{f.name} is capability-shaped ({offenders}) — "
            "the profile must describe MECHANISM (providers to wire), never CLAIM a "
            "capability. Capability presence is proven by behavior, not self-assertion."
        )


@pytest.mark.parametrize(
    "profile_type",
    [HarnessProfile, HookDescriptor, StopMultiplexDescriptor, McpServerDescriptor],
)
def test_profile_types_are_frozen(profile_type: type) -> None:
    """Every profile type is a FROZEN dataclass — a caller cannot mutate a field."""
    params = getattr(profile_type, "__dataclass_params__", None)
    assert params is not None and params.frozen, f"{profile_type.__name__} must be frozen"


def test_capabilities_kwarg_is_unconstructable() -> None:
    """Passing a self-asserted ``capabilities=`` field is a TypeError (impossible
    by construction, not merely unused)."""
    with pytest.raises(TypeError):
        HarnessProfile(  # type: ignore[call-arg]
            hook_descriptors=(),
            stop_hook=StopMultiplexDescriptor("n", ()),
            mcp_server=McpServerDescriptor("s", "m", ()),
            asset_package="pkg",
            capabilities=["relay"],
        )


# ── D6b: the consumer-trace — nothing in the trust path reads the profile ────


def _profile_referencing_modules() -> dict[str, list[str]]:
    """Every src module that imports the profile module or references a profile name.

    Returns ``{module_relpath: [reasons]}`` — an import of ``hpc_agent.harness_profile``
    or a Name/attribute use of any of :data:`_PROFILE_NAMES`.
    """
    hits: dict[str, list[str]] = {}
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        reasons: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "hpc_agent.harness_profile":
                reasons.append(f"from hpc_agent.harness_profile import ... (line {node.lineno})")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "hpc_agent.harness_profile":
                        reasons.append(f"import hpc_agent.harness_profile (line {node.lineno})")
            elif isinstance(node, ast.Name) and node.id in _PROFILE_NAMES:
                reasons.append(f"name {node.id} (line {node.lineno})")
            elif isinstance(node, ast.Attribute) and node.attr in _PROFILE_NAMES:
                reasons.append(f"attr {node.attr} (line {node.lineno})")
        if reasons:
            hits[rel] = reasons
    return hits


def test_only_the_install_engine_reads_the_profile() -> None:
    """No trust-path (or any non-owner) module references the profile — the pin
    the whole activation program lives on (D6b). A new reader must be added to
    :data:`_PROFILE_OWNER_MODULES` deliberately (and justify it is not a trust read)."""
    referencing = {Path(rel).name for rel in _profile_referencing_modules() if Path(rel).name}
    leaked = sorted(referencing - _PROFILE_OWNER_MODULES)
    assert not leaked, (
        f"module(s) outside the install engine reference the activation profile: {leaked}. "
        "Installing a profile grants NO trust — no gate/verify/journal/conformance code may "
        "read profile-presence as capability-presence (premortem D6). If this is a legitimate "
        "new renderer/consumer, add it to _PROFILE_OWNER_MODULES with justification."
    )
