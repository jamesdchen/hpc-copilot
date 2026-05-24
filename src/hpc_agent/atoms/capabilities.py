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
"""

from __future__ import annotations

import os
from typing import Any

import hpc_agent
from hpc_agent._internal import session
from hpc_agent._kernel.registry.primitive import primitive


@primitive(
    name="capabilities",
    verb="query",
    side_effects=[],
    idempotent=True,
    # CLI is registered as a Tier 3 verb in :mod:`hpc_agent.cli.setup`
    # (the ``--full`` flag bypasses the JSON-envelope contract, so the
    # adapter is hand-written rather than dispatcher-driven). The atom
    # is registered for the catalog only.
    cli=None,
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
    from hpc_agent._kernel.registry.operations import operations_catalog
    from hpc_agent.infra.clusters import CLUSTER_YAML_KEYS

    return {
        "version": hpc_agent.__version__,
        "subcommands": list(subcommands),
        "supported_schedulers": ["sge", "slurm"],
        "schemas_dir": str(hpc_agent._PACKAGE_ROOT / "schemas"),
        "journal_dir": str(session.HPC_HOMEDIR),
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
        "operations": operations_catalog(),
    }
