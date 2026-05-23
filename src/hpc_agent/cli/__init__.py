"""Per-verb-group CLI adapter modules.

This package hosts argparse adapter functions (the ``cmd_*`` shims) split
out from :mod:`hpc_agent.agent_cli` so each verb group can evolve without
forcing merge conflicts on a single 3k-line module. The argparse parser
wiring (``set_defaults(func=...)``) currently still lives in
``agent_cli.build_parser``; this package only owns the function bodies.
"""
