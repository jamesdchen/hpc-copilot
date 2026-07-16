"""CI lint: every loaded hpc-agent plugin's MANIFEST reconciles with what it actually ships.

Item 5 added :class:`PluginManifest` as the explicit declaration of a
plugin's overlay contributions (primitive names, worker-prompt
overlays, CLI registration). This lint walks every loaded plugin and
verifies the manifest's declarations match the runtime reality:

* every ``MANIFEST.primitives`` name is present in the operations
  catalog after registration;
* every ``MANIFEST.worker_prompt_overlays`` workflow has a matching
  ``<workflow>.md`` under the plugin's ``worker_prompt_assets`` root;
* ``MANIFEST.cli_register`` is consistent with whether the plugin
  exposes a callable ``register_cli`` attribute.

A plugin without a manifest is reported as a separate warning (the
host loader already emits ``DeprecationWarning`` for the same case at
runtime) but does not fail the lint — Item 5 ships the manifest as
informational metadata, not a hard requirement, for the first release.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from hpc_agent._kernel.registry.plugins import (  # noqa: E402
    get_plugin_manifests,
    load_plugins,
    plugin_worker_prompt_roots,
)
from hpc_agent._kernel.registry.primitive import get_registry, register_primitives  # noqa: E402


def main() -> int:
    register_primitives()
    registry = get_registry()
    manifests = get_plugin_manifests()
    loaded = load_plugins()

    violations: list[str] = []

    if not loaded:
        print("no hpc-agent plugins installed — lint is a no-op")
        return 0

    plugin_by_module: dict[str, object] = {}
    for plugin in loaded:
        manifest = getattr(plugin, "MANIFEST", None)
        if manifest is None:
            continue
        plugin_by_module[manifest.name] = plugin

    for name, manifest in manifests.items():
        for prim_name in manifest.primitives:
            if prim_name not in registry:
                violations.append(
                    f"plugin {name!r}: manifest declares primitive "
                    f"{prim_name!r}, but it is not in the live registry "
                    "(did register_primitives() get called? did the module "
                    "fail to import?)"
                )

        plugin = plugin_by_module.get(name)
        cli_register_attr = getattr(plugin, "register_cli", None) if plugin is not None else None
        if manifest.cli_register and not callable(cli_register_attr):
            violations.append(
                f"plugin {name!r}: manifest says cli_register=True but "
                "the plugin object exposes no callable register_cli "
                "attribute."
            )
        if not manifest.cli_register and callable(cli_register_attr):
            violations.append(
                f"plugin {name!r}: manifest says cli_register=False but "
                "the plugin object exposes a callable register_cli "
                "attribute that the host loader will invoke."
            )
        # A plugin cannot reshape a core verb without the register_cli hook
        # (the sole seam handed the argparse subparsers). A non-empty
        # ``reshapes_core_verbs`` therefore implies ``cli_register=True`` — the
        # fast-path gate (rank 13) trusts this declaration to keep core verbs
        # fast, so an inconsistent pair must fail the lint.
        if manifest.reshapes_core_verbs and not manifest.cli_register:
            violations.append(
                f"plugin {name!r}: manifest declares "
                f"reshapes_core_verbs={tuple(manifest.reshapes_core_verbs)!r} "
                "but cli_register=False — a plugin cannot reshape a core verb "
                "without a register_cli hook."
            )

    overlay_roots = plugin_worker_prompt_roots()
    advertised_overlays: set[str] = set()
    for manifest in manifests.values():
        advertised_overlays.update(manifest.worker_prompt_overlays)
    for overlay in advertised_overlays:
        if not any(
            (root / f"{overlay}.md").is_file()  # type: ignore[union-attr]
            for root in overlay_roots
        ):
            violations.append(
                f"manifest declares worker-prompt overlay {overlay!r} but "
                f"no installed plugin's worker_prompt_assets contains "
                f"{overlay}.md"
            )

    if violations:
        print("ERROR: plugin manifest reconciliation failed:")
        for v in violations:
            print(f"  {v}")
        return 1

    print(f"plugin manifests OK ({len(manifests)} manifest(s), {len(loaded)} loaded plugin(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
