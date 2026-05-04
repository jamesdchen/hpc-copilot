"""Deprecation shim for ``python -m hpc_mapreduce <subcommand>``.

Forwards to ``claude_hpc.agent_cli.main`` while emitting a single
``DeprecationWarning`` so callers know to migrate to
``python -m claude_hpc <subcommand>`` (or just ``hpc-mapreduce
<subcommand>`` — the binary entry point hasn't changed).
"""

from __future__ import annotations

import warnings

warnings.warn(
    "`python -m hpc_mapreduce` has been renamed to `python -m claude_hpc`. "
    "Update your invocations; the shim will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

from claude_hpc.agent_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
