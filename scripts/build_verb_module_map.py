"""Generate / check the CLI single-verb fast-path module map.

``register_primitives()`` imports ~100 modules of Pydantic models on every
``hpc-agent`` invocation — roughly half the process's cold-start cost (see
``docs/internals/`` perf notes). But the overwhelmingly common invocation runs
ONE known verb, which needs only the module that defines it. The fast path in
``hpc_agent.cli.dispatch.main`` imports just that module — and to know which
module a verb lives in *without* importing everything first, it reads this
generated map.

The map is SELF-HEALING: a stale or missing entry only makes the fast path fall
back to the full ``register_primitives`` walk (a perf regression, never a
correctness bug), so this generator is a hygiene/perf gate, not a
correctness-critical SoT. Regenerate after adding/renaming an ungrouped verb::

    uv run python scripts/build_verb_module_map.py            # diff
    uv run python scripts/build_verb_module_map.py --check    # CI/perf gate
    uv run python scripts/build_verb_module_map.py --write    # apply

Mapped verbs are **ungrouped** and EITHER handler-less OR a handler primitive
that opts into the fast path via ``CliShape.fast_path_safe`` (rank 13). Excluded
(they simply fall back to the full path): verb-grouped commands (``campaign
status``, ``clusters list``) and registry-introspecting ``handler=`` primitives
(``capabilities`` / ``describe`` read the WHOLE registry, which the fast path
leaves unpopulated). ``install-commands`` is the first ``fast_path_safe``
handler — its body copies bundled assets and never walks the registry, so it is
byte-identical on the fast path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Core-only view: a plugin's verbs are resolved at runtime (and the fast path
# disables itself when any plugin is installed), so they don't belong in this
# committed core map. Must precede the first hpc_agent import (cached plugins).
os.environ.setdefault("HPC_AGENT_DISABLE_PLUGINS", "1")

_TARGET = (
    Path(__file__).resolve().parent.parent / "src" / "hpc_agent" / "cli" / "_verb_module_map.py"
)

_HEADER = '''\
# ruff: noqa: E501 — generated dict literal; one (verb, module) row per line.
"""Generated verb → defining-module map for the CLI single-verb fast path.

DO NOT EDIT BY HAND. Regenerate with::

    uv run python scripts/build_verb_module_map.py --write

Maps each ungrouped, handler-less verb to ``(primitive_name, module_path)`` so
``hpc_agent.cli.dispatch.main`` can import only that module (instead of the full
``register_primitives`` walk) before dispatching it. Self-healing: a stale entry
just falls back to the full path. See ``scripts/build_verb_module_map.py``.
"""

from __future__ import annotations

VERB_MODULE_MAP: dict[str, tuple[str, str]] = {
'''

_FOOTER = "}\n"


def _render() -> str:
    from hpc_agent._kernel.registry.primitive import register_primitives
    from hpc_agent.cli._dispatch import CliShape, _leaf_verb

    register_primitives()
    from hpc_agent._kernel.registry.primitive import get_registry

    rows: dict[str, tuple[str, str]] = {}
    for name, meta in get_registry().items():
        shape = meta.cli
        if not isinstance(shape, CliShape):
            continue
        # Grouped verbs nest under a parent subparser — always the full path.
        if shape.group is not None:
            continue
        # Handler primitives take the full path UNLESS they opt in via
        # ``fast_path_safe``: ``capabilities`` / ``describe`` read the WHOLE
        # registry (which the fast path leaves unpopulated), but a self-contained
        # handler like ``install-commands`` is byte-identical on the fast path
        # (``dispatch_primitive`` routes handler primitives fine). See rank 13.
        if shape.handler is not None and not shape.fast_path_safe:
            continue
        verb = _leaf_verb(name, shape)
        module = getattr(meta.func, "__module__", None)
        if not module:
            continue
        if verb in rows and rows[verb] != (name, module):
            raise SystemExit(
                f"verb collision building fast-path map: {verb!r} maps to both "
                f"{rows[verb]} and {(name, module)}"
            )
        rows[verb] = (name, module)

    lines = [_HEADER]
    for verb in sorted(rows):
        name, module = rows[verb]
        lines.append(f"    {verb!r}: ({name!r}, {module!r}),\n")
    lines.append(_FOOTER)
    return "".join(lines)


def main() -> int:
    write = "--write" in sys.argv
    check = "--check" in sys.argv
    rendered = _render()
    current = _TARGET.read_text(encoding="utf-8") if _TARGET.exists() else ""
    if rendered == current:
        if not write and not check:
            print("verb-module map up to date")
        return 0
    if write:
        _TARGET.write_text(rendered, encoding="utf-8")
        rel = _TARGET.relative_to(Path.cwd()) if _TARGET.is_relative_to(Path.cwd()) else _TARGET
        print(f"wrote {rel}")
        return 0
    print(
        "verb-module map is stale; run "
        "`uv run python scripts/build_verb_module_map.py --write` to regenerate.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
