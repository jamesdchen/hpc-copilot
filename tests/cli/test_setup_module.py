"""Smoke tests for the four Tier 3 verbs hosted in :mod:`hpc_agent.cli.setup`.

Pins:

* The new home (``hpc_agent.cli.setup``) exports each ``cmd_*`` symbol.
* The back-compat re-export from :mod:`hpc_agent.agent_cli` resolves to
  the same callable (so external imports keep working).
* ``register(sub)`` wires the four verbs into the argparse tree built
  by :func:`hpc_agent.cli.parser.build_parser`.

Behavior tests for ``setup`` (preflight marker, dry-run, etc.) live in
``tests/cli/test_setup.py`` and exercise ``cmd_setup`` via the
back-compat re-export — those keep working as a regression net.
"""

from __future__ import annotations

import argparse


def test_cli_setup_module_exports_the_four_cmds() -> None:
    """Pin: the new module is the canonical home for the four cmds."""
    from hpc_agent.cli import setup as setup_mod

    assert callable(setup_mod.cmd_install_commands)
    assert callable(setup_mod.cmd_setup)
    assert callable(setup_mod.cmd_capabilities)
    assert callable(setup_mod.cmd_describe)
    assert callable(setup_mod.register)


def test_agent_cli_reexports_alias_to_cli_setup() -> None:
    """Pin: ``from hpc_agent.agent_cli import cmd_X`` keeps working."""
    from hpc_agent import agent_cli
    from hpc_agent.cli import setup as setup_mod

    # Identity, not just equality — the re-export is the same object so
    # ``mock.patch("hpc_agent.cli.setup.cmd_setup")`` and patching via the
    # legacy ``hpc_agent.agent_cli`` path target the same callable.
    assert agent_cli.cmd_install_commands is setup_mod.cmd_install_commands
    assert agent_cli.cmd_setup is setup_mod.cmd_setup
    assert agent_cli.cmd_capabilities is setup_mod.cmd_capabilities
    assert agent_cli.cmd_describe is setup_mod.cmd_describe


def test_register_wires_the_four_verbs_into_the_parser() -> None:
    """Pin: register() adds capabilities / install-commands / setup / describe."""
    from hpc_agent.cli.parser import build_parser

    parser = build_parser()
    top: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            top = set(action.choices)
            break
    assert {"capabilities", "install-commands", "setup", "describe"} <= top
