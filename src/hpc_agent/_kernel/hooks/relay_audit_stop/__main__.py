"""``python -m hpc_agent._kernel.hooks.relay_audit_stop`` dispatch.

The hook-entry needle ``agent_assets`` writes into users' ``settings.json`` is
``python -m hpc_agent._kernel.hooks.relay_audit_stop`` (see
:func:`hpc_agent.agent_assets._build_relay_audit_command`). Running a PACKAGE
with ``-m`` executes this ``__main__`` submodule, so it must forward to the
entry callable — the module path stays byte-identical to the installed needle.
"""

from __future__ import annotations

from . import main

if __name__ == "__main__":  # pragma: no cover - exercised via the harness / -m
    raise SystemExit(main())
