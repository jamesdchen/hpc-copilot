#!/usr/bin/env python3
"""Integration check: verify hpc-agent-pro plugs into hpc-agent.

Run this in an environment where BOTH packages are installed. It exits
non-zero if the plugin's primitives fail to register or its CLI
subcommands fail to appear — i.e. if the two packages have drifted out
of compatibility across the contract surface (the @primitive seam, the
hpc_agent.plugins entry point, the shared infra/state modules).

This is the "is the split still clean?" command — CI runs it, and you
can run it by hand before releasing either package.
"""

from __future__ import annotations

import subprocess
import sys

EXPECTED_PRIMITIVES = (
    "score-submit-plan",
    "validate",
    "inspect-cluster",
    "best-submit-window",
    "walltime-drift",
)
EXPECTED_SUBCOMMANDS = (
    "plan-submit",
    "inspect-cluster",
    "predict-queue-wait",
    "best-submit-window",
    "walltime-drift",
)


def main() -> int:
    import hpc_agent

    hpc_agent.register_primitives()
    registry = hpc_agent.get_registry()
    missing = [p for p in EXPECTED_PRIMITIVES if p not in registry]
    if missing:
        print(f"FAIL: plugin primitives absent from the registry: {missing}")
        print("Is hpc-agent-pro installed? Have the packages drifted?")
        return 1
    print(f"OK: {len(registry)} primitives registered with the plugin installed.")

    help_text = subprocess.run(
        ["hpc-agent", "--help"], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout
    missing_cmds = [c for c in EXPECTED_SUBCOMMANDS if c not in help_text]
    if missing_cmds:
        print(f"FAIL: advisory subcommands absent from `hpc-agent --help`: {missing_cmds}")
        return 1
    print("OK: advisory subcommands restored by the plugin.")

    # The plugin's overriding hpc-submit SKILL.md must reach workers
    # via the prompt-renderer's plugin lookup — that's what lets
    # `/submit-hpc` actually run planner-aware steps under a delegated
    # worker, not only in the interactive context.
    from hpc_agent.atoms.spawn_prompt import _skill_body

    _skill_body.cache_clear()
    body = _skill_body("hpc-submit")
    if "score-submit-plan" not in body:
        print(
            "FAIL: hpc-submit skill resolved for workers does not contain the "
            "plugin's planner steps (`score-submit-plan`). The plugin's "
            "slash_command_assets aren't being consulted by spawn_prompt._skill_body."
        )
        return 1
    print("OK: plugin's overriding hpc-submit SKILL.md is visible to workers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
