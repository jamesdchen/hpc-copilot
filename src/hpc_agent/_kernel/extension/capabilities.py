"""``capabilities`` primitive — emit the operations catalog + env metadata.

Pure-dispatch primitive: builds the capabilities envelope from package
metadata, the operations catalog, and the journal home dir. No SSH,
no scheduler, no filesystem mutations.

Discovery + content fetching are split: ``subcommands`` enumerates the
CLI verbs an orchestrator can drive, and ``operations`` enumerates the
``@primitive``-registered catalog. To fetch the content of a specific
primitive or worker-prompt procedure by name, use ``hpc-agent describe
<name>`` — it returns the body as a JSON envelope, eliminating the
need for callers to reach into package-data filesystem paths.

The ``--full`` flag bypasses the JSON-envelope contract and emits a
plain-text llms-full dump; the dispatcher therefore goes through the
``handler=`` escape hatch instead of the standard
:func:`dispatch_primitive` envelope path.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import hpc_agent
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.cli._helpers import EXIT_OK, _ok
from hpc_agent.state.run_record import HPC_HOMEDIR


def _capabilities_handler(args: argparse.Namespace) -> int:
    """CLI adapter — emits llms-full text on ``--full``, else the envelope.

    A build+dist-keyed disk cache (:mod:`hpc_agent.state.capabilities_cache`)
    short-circuits the catalog projection / llms-full render on the second-and-
    later cold call in a build/env. A hit is re-emitted through the SAME path
    (byte-identical stdout); the cache is disabled on a source checkout, so this
    is a pure pass-through in dev.
    """
    from hpc_agent.state import capabilities_cache

    if getattr(args, "full", False):
        cached_full = capabilities_cache.load("full")
        if isinstance(cached_full, str):
            sys.stdout.write(cached_full)
            sys.stdout.flush()
            return EXIT_OK
        from hpc_agent._kernel.registry.operations import render_llms_full

        text = render_llms_full()
        capabilities_cache.store("full", text)
        sys.stdout.write(text)
        sys.stdout.flush()
        return EXIT_OK

    cached_bare = capabilities_cache.load("bare")
    if isinstance(cached_bare, dict):
        _ok(cached_bare, name="capabilities")
        return EXIT_OK

    from hpc_agent.cli.dispatch import _live_subcommands

    data = capabilities(subcommands=_live_subcommands())
    capabilities_cache.store("bare", data)
    _ok(data, name="capabilities")
    return EXIT_OK


@primitive(
    name="capabilities",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help="Machine-readable feature flags: subcommands, schedulers, schema dirs.",
        args=(
            CliArg(
                "--full",
                action="store_true",
                help=(
                    "Emit a plain-text llms-full dump (catalog + every primitive doc + "
                    "schemas + envelope + boundary contract + cli-spec). Exception to the "
                    "stdout-is-JSON contract; intended for one-shot LLM context loading."
                ),
            ),
        ),
        handler=_capabilities_handler,
    ),
    agent_facing=True,
)
def capabilities(*, subcommands: list[str]) -> dict[str, Any]:
    """Return the capabilities-envelope data payload.

    *subcommands* is the live list derived from the argparse tree
    (passed in by the CLI adapter so the atom doesn't reach back into
    the dispatcher to walk argparse internals). Everything else —
    version, supported schedulers, schemas dir, journal dir, ssh
    multiplexing flag, required env vars, and the operations catalog —
    is computed here. Content for named primitives + procedures is
    fetched via ``hpc-agent describe <name>``.
    """
    from hpc_agent._kernel.registry.operations import operations_bootstrap
    from hpc_agent._kernel.registry.plugins import get_plugin_manifests
    from hpc_agent.infra.backends import registered_backend_names
    from hpc_agent.infra.clusters import CLUSTER_YAML_KEYS

    # #306: the bootstrap envelope carries only the thin per-op row an
    # orchestrator gates on (operations_bootstrap / BOOTSTRAP_FIELDS). The
    # forensic pointers (python / input_schema / output_schema) and the
    # one-line summary stay in the full operations_catalog() projection
    # and are fetched on demand via `find` / `describe` / `--full`.
    operations = operations_bootstrap()

    return {
        "version": hpc_agent.__version__,
        "subcommands": list(subcommands),
        # Derived from the live backend registry (built-ins + any installed
        # plugin backend), not a frozen list — a pure-API plugin backend
        # advertises itself here too (#337).
        "supported_schedulers": sorted(registered_backend_names()),
        "schemas_dir": os.path.join(hpc_agent._PACKAGE_ROOT, "schemas"),
        "journal_dir": str(HPC_HOMEDIR),
        "ssh_multiplexing": os.environ.get("HPC_NO_SSH_MULTIPLEX") != "1",
        "required_env": [
            "SSH_AUTH_SOCK",
            "HPC_JOURNAL_DIR",
            "HPC_CLUSTERS_CONFIG",
        ],
        # B-M4: enumerate the per-cluster yaml keys so a campus user
        # discovering the schema by inspection (rather than reading
        # hpc_agent/infra/clusters.py source) sees every supported field.
        # New fields land here automatically when their validators are
        # added — single source of truth lives next to the validators.
        "cluster_yaml_keys": list(CLUSTER_YAML_KEYS),
        # Item 5: every loaded plugin's PluginManifest, projected as a
        # dict so callers can introspect overlay contributions without
        # importing the plugin distributions themselves. Empty when no
        # plugin is installed or when installed plugins haven't yet
        # declared a manifest (a DeprecationWarning fires in that case).
        "plugins": [m.model_dump(mode="json") for m in get_plugin_manifests().values()],
        "operations": operations,
    }
