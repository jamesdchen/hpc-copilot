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
* :mod:`.main` — :func:`main`, the entry point. ``pyproject.toml``'s
  ``hpc-agent`` script flows through ``hpc_agent.agent_cli:main``
  which re-exports this for back-compat.

Domain modules (one per CLI section):

* :mod:`.setup` — install + capabilities + describe + preflight +
  validate-campaign (introspection + validators).
* :mod:`.submit` — submit + submit-flow + submit-flow-batch +
  build-submit-spec + summarize-submit-plan + verify-canary.
* :mod:`.lifecycle` — status + monitor-flow + monitor-summary +
  decide-monitor-arm + logs + failures.
* :mod:`.aggregate` — aggregate + aggregate-flow + cluster-reduce +
  verify-aggregation-complete.
* :mod:`.recover` — resubmit + reconcile.
* :mod:`.discover` — discover + discover-reducers + clusters-list +
  clusters-describe + list-in-flight + load-context + find-prior-run +
  suggest-setup-action + plan-throughput.
* :mod:`.template` — build-executor + build-template + build-tasks-py +
  export-package + axes-init + classify-axis.
* :mod:`.memory` — interview + recall.
* :mod:`.spawn` — run (workflow spawn pipeline entry point).
* :mod:`.campaign` — campaign verb group (init/list/status/replay/
  converged/budget/advance/health).

See ``docs/internals/skill-policy.md`` for the broader rule that
shapes the split: pick the *user mental model* axis for surfaces, the
*verb* axis for internals.

Note: :func:`main` is intentionally not re-exported from this package's
``__init__`` to avoid a circular import (``cli/__init__`` →
``cli/main`` → ``agent_cli`` → ``cli/_helpers`` triggers ``cli/__init__``
again). Import it explicitly via ``from hpc_agent.cli.main import
main`` or via the back-compat alias ``from hpc_agent.agent_cli import
main`` (the ``pyproject.toml`` entry-point path).
"""
