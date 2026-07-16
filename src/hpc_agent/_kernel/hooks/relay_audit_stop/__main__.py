"""``python -m hpc_agent._kernel.hooks.relay_audit_stop`` dispatch.

Since the Stop-hook fusion (``stop_multiplex``), the INSTALLED Stop entry no
longer runs this package directly — the fused multiplexer imports
:func:`.build_hook_output` and calls it in-process alongside the other two Stop
guards (one interpreter start for all three, #288). This ``__main__`` is kept so
the standalone ``python -m hpc_agent._kernel.hooks.relay_audit_stop`` invocation
still works (direct debugging; the conformance adapter's honest needle
declaration), and because the module path stays a load-bearing NEEDLE: it is
listed as an argument of the fused command, so ``agent_assets``'s capability probe
and re-find matcher keep resolving it. Running a PACKAGE with ``-m`` executes this
submodule, so it forwards to the entry callable.
"""

from __future__ import annotations

from . import main

if __name__ == "__main__":  # pragma: no cover - exercised via the harness / -m
    raise SystemExit(main())
