"""``hpc-agent`` CLI package — per-domain argparse adapters.

The CLI is decomposed into per-domain modules so each verb's adapter
code (``cmd_*``) sits next to its parser registration (``register(sub)``).
The top-level entry point (``hpc-agent``) flows through :func:`main`
re-exported here.

Layout:

* :mod:`._helpers` — adapter SDK (input/output boundary helpers, used
  by every ``cmd_*`` here and by external plugins via the same import
  path). The underscore is historical; this is contract surface.
* :mod:`.parser` — :func:`build_parser`, the argparse orchestrator that
  calls each domain module's ``register(sub)`` and finally
  ``register_plugin_cli(sub)``.
* :mod:`.dispatch` — :func:`main`, the entry point, plus the
  argv-preprocessor for verb groups. ``pyproject.toml``'s ``hpc-agent``
  script flows through ``hpc_agent.cli.dispatch:main``.
* :mod:`.main` — public re-export alias of :func:`dispatch.main`.

Domain modules (one per CLI section):

* :mod:`.setup` — install-commands + setup + describe + capabilities
  (Tier-3 verbs converted to ``@primitive`` decorators that the
  registry-driven parser picks up; ``cli/setup.py:register`` is now a
  no-op back-compat shim).
* :mod:`.submit` — submit (record) + the submit verb group's helpers.
* :mod:`.lifecycle` — status / monitor / logs / failures helpers.
* :mod:`.aggregate` — aggregate (combine-wave) helpers.
* :mod:`.recover` — resubmit + reconcile.
* :mod:`.setup_actions` — suggest-setup-action + find-prior-run.
* :mod:`.spawn` — ``run`` (workflow spawn pipeline entry point) — the
  one remaining Tier-3 verb without a ``@primitive`` backing.

Every other verb (validators, scaffolds, queries, the campaign verb
group, build-*, plan-throughput, recall, interview, etc.) flows through
``cli/parser.py:_register_from_registry`` — the parser walks the
``@primitive`` registry and emits an argparse subparser for each
``CliShape``. There is no per-verb file for those.

See ``docs/internals/skill-policy.md`` for the broader rule that
shapes the split: pick the *user mental model* axis for surfaces, the
*verb* axis for internals.

Note: :func:`main` is intentionally not re-exported from this package's
``__init__`` to avoid a circular import (``cli/__init__`` →
``cli/dispatch`` → ``cli/_helpers`` triggers ``cli/__init__`` again).
Import it explicitly via ``from hpc_agent.cli.dispatch import main``
(the ``pyproject.toml`` entry-point path).
"""
