"""Contract: the host package names no specific plugin distribution.

``hpc-agent`` discovers plugins generically through the
``hpc_agent.plugins`` entry-point group and resolves their assets by
``getattr`` + convention (see
:mod:`hpc_agent._kernel.registry.plugins`). Nothing in the host runtime
should hard-code a particular plugin's package or distribution name —
the moment it does, that one plugin is privileged over every other and
the extension seam is a fiction.

This is the regression guard for the schema-lookup coupling that the
plugin-schema-hook change removed: ``cli/_helpers`` used to iterate a
literal ``"hpc_agent_pro.schemas"`` so only that one plugin's input
schemas were ever found. The fix routes through
``plugins.plugin_schema_roots()``; this test keeps any split-off
plugin's distribution name from creeping back into a host module.

The guard is a denylist of concrete plugin distribution names (the
``pro`` plugin lives in its own repository now). It deliberately does
*not* match the host's own ``hpc_agent_<word>`` internal identifiers
(``hpc_agent_version`` and friends) — those are core, not plugins. A
placeholder used purely to document the naming convention
(``hpc_agent_myplugin`` / ``hpc-agent-myplugin``) is fine and is not in
the denylist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "hpc_agent"

# Distribution / import names of plugins that have been split out of this
# repository and must never be referenced by the host core. Both the
# distribution form (``hpc-agent-pro``) and the import form
# (``hpc_agent_pro``) are forbidden. Extend this set if another plugin is
# split off.
_FORBIDDEN_PLUGIN_NAMES: tuple[str, ...] = (
    "hpc_agent_pro",
    "hpc-agent-pro",
)

_FORBIDDEN_RE = re.compile("|".join(re.escape(name) for name in _FORBIDDEN_PLUGIN_NAMES))


def _python_sources() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def test_src_tree_exists() -> None:
    """Guard against the glob silently matching nothing (wrong root)."""
    assert _python_sources(), f"no python sources under {_SRC_ROOT}"


@pytest.mark.parametrize("path", _python_sources(), ids=lambda p: str(p.name))
def test_host_module_names_no_split_off_plugin(path: Path) -> None:
    """No host module references a split-off plugin distribution by name."""
    offending: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in _FORBIDDEN_RE.findall(line):
            offending.append((lineno, match))
    rel = path.relative_to(_SRC_ROOT.parents[1])
    assert not offending, (
        f"{rel} references a split-off plugin distribution by name "
        f"(the host must stay plugin-agnostic): "
        + "; ".join(f"line {n}: {tok!r}" for n, tok in offending)
        + ". Route through the generic plugin seam in "
        "hpc_agent._kernel.registry.plugins instead of naming the plugin."
    )
