"""``capabilities`` primitive — emit the operations catalog + env metadata.

Pure-dispatch primitive: builds the capabilities envelope from
package metadata, the operations catalog, the journal home dir, and
the resolved slash-command skill paths. No SSH, no scheduler, no
filesystem mutations.
"""

from __future__ import annotations

import os
from typing import Any

import hpc_agent
from hpc_agent._internal import session
from hpc_agent._internal.primitive import primitive

# Names of the slash-command skill bundles shipped in the source tree.
# Capabilities reports the absolute path to each present ``SKILL.md``
# so an orchestrator can load the skill content without re-deriving
# the layout.
_SKILL_NAMES = (
    "hpc-submit",
    "hpc-status",
    "hpc-aggregate",
    "hpc-build-executor",
    "hpc-campaign",
    "hpc-classify-axis",
)


def _resolve_skill_paths() -> dict[str, str]:
    # Skills ship as package data inside the ``slash_commands`` package
    # (``slash_commands/skills/<name>/SKILL.md``), so they resolve the
    # same way whether installed from a wheel or run from a checkout.
    # Return only entries that resolve to an existing file so a consumer
    # can rely on every value being a real path.
    from importlib.resources import files as _resource_files

    skills_root = _resource_files("slash_commands") / "skills"
    out: dict[str, str] = {}
    for name in _SKILL_NAMES:
        path = skills_root / name / "SKILL.md"
        if path.is_file():
            out[name] = str(path)
    return out


@primitive(
    name="capabilities",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-agent capabilities",
    agent_facing=True,
)
def capabilities(*, subcommands: list[str]) -> dict[str, Any]:
    """Return the capabilities-envelope data payload.

    *subcommands* is the live list derived from the argparse tree
    (passed in by the CLI adapter so the atom doesn't reach back into
    the dispatcher to walk argparse internals). Everything else —
    version, supported schedulers, schemas dir, journal dir, ssh
    multiplexing flag, slash-command skill paths, required env vars,
    and the operations catalog — is computed here.
    """
    from hpc_agent._internal.operations import operations_catalog
    from hpc_agent.infra.clusters import CLUSTER_YAML_KEYS

    return {
        "version": hpc_agent.__version__,
        "subcommands": list(subcommands),
        "supported_schedulers": ["sge", "slurm"],
        "schemas_dir": str(hpc_agent._PACKAGE_ROOT / "schemas"),
        "journal_dir": str(session.HPC_HOMEDIR),
        "ssh_multiplexing": os.environ.get("HPC_NO_SSH_MULTIPLEX") != "1",
        "skill_paths": _resolve_skill_paths(),
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
