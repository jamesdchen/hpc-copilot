"""hpc_agent.flows — multi-atom workflows (submit, monitor, aggregate, ...).

Submodules are deliberately importer-explicit. Each workflow lives in
its own submodule and registers as a ``@primitive`` with
``verb='workflow'``. Reach for the specific entry point:

* :mod:`hpc_agent.flows.submit_flow`
* :mod:`hpc_agent.flows.monitor_flow`
* :mod:`hpc_agent.flows.aggregate_flow`
* :mod:`hpc_agent.flows.validate_campaign`

The recover/resubmit pipeline moved into the ``ops/recover`` subject
in PR 2.3 — see :mod:`hpc_agent.ops.recover.flow`.

Eager re-exports here are tempting but every workflow imports atoms +
state + infra; importing flows would chain-load most of the package
even when only one workflow is needed.
"""
