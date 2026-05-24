"""Back-compat shim — the CLI orchestrator now lives in :mod:`hpc_agent.cli.dispatch`.

The canonical entry point and argparse orchestrator moved to
:mod:`hpc_agent.cli.dispatch` (PR 5c). Per-domain ``cmd_*`` adapters
live in :mod:`hpc_agent.cli.<domain>`. Adapter SDK helpers live in
:mod:`hpc_agent.cli._helpers`.

This module re-exports the public surface so existing imports
(``from hpc_agent.agent_cli import cmd_setup``, the ``hpc-agent-pro``
plugin, tests that pin the legacy paths) keep resolving. New code
should import from the canonical homes directly.
"""

from __future__ import annotations

# ─── adapter SDK ───────────────────────────────────────────────────────────
#
# Helpers + EXIT codes live in :mod:`hpc_agent.cli._helpers` (the
# canonical home). Re-exported here so existing imports keep working —
# the ``hpc-agent-pro`` plugin and a handful of tests do
# ``from hpc_agent.agent_cli import _ok, _load_spec, ...``. New code
# imports from ``hpc_agent.cli._helpers`` directly.
from hpc_agent.cli._helpers import (  # noqa: F401 — re-exported public surface
    _EXIT_CODE_BY_CATEGORY,
    EXIT_CLUSTER_ERROR,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USER_ERROR,
    _add_experiment_dir,
    _add_run_id,
    _add_spec_and_dry_run,
    _emit,
    _err,
    _err_from_hpc,
    _load_spec,
    _meta_idempotent,
    _ok,
    _require_ssh_agent,
    _validate_against_schema,
)

# ─── per-domain cmd_* re-exports (back-compat for tests + plugins) ─────────
#
# Every legacy ``cmd_*`` adapter moved to its per-domain module under
# ``hpc_agent.cli/<module>.py``. We re-export the symbols here so
# existing call sites (``from hpc_agent.agent_cli import cmd_setup``)
# keep resolving. New code imports from ``hpc_agent.cli.<module>``
# directly.
from hpc_agent.cli.aggregate import cmd_aggregate  # noqa: F401

# Re-export the orchestrator surface (entry point + argparse helpers).
from hpc_agent.cli.dispatch import (  # noqa: F401 — re-exported public surface
    _VERB_GROUPS,
    _live_subcommands,
    _print_group_help,
    _strip_verb_group,
    build_parser,
    cmd_logs,
    main,
)
from hpc_agent.cli.lifecycle import (  # noqa: F401
    _preempted_summary_from_sidecar,
    cmd_status,
)
from hpc_agent.cli.recover import (  # noqa: F401
    _VALID_RESUBMIT_CATEGORIES,
    cmd_resubmit,
)
from hpc_agent.cli.setup import (  # noqa: F401
    cmd_capabilities,
    cmd_describe,
    cmd_install_commands,
    cmd_setup,
)
from hpc_agent.cli.spawn import cmd_run  # noqa: F401
from hpc_agent.cli.submit import (  # noqa: F401
    cmd_submit,
    cmd_submit_flow,
    cmd_submit_flow_batch,
)

# Helper re-exports for legacy import paths in tests + the pro plugin.
from hpc_agent.ops.monitor.list_in_flight import _last_status_age_seconds  # noqa: F401
