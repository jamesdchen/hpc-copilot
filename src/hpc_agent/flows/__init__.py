"""hpc_agent.flows — multi-atom workflows (submit, monitor, aggregate, ...).

Submodules are deliberately importer-explicit. Each workflow lives in
its own submodule and registers as a ``@primitive`` with
``verb='workflow'``. Reach for the specific entry point:

* :mod:`hpc_agent.ops.submit.flow` (moved to ops/submit/ in the Wave 2 reorg)
* :mod:`hpc_agent.ops.monitor.flow` (moved to ops/monitor/ in PR 3.1)
* :mod:`hpc_agent.ops.aggregate.flow` (moved out of flows in PR 2.2)
* :mod:`hpc_agent.flows.validate_campaign`

The recover/resubmit pipeline moved into the ``ops/recover`` subject
in PR 2.3 — see :mod:`hpc_agent.ops.recover.flow`.

Eager re-exports here are tempting but every workflow imports atoms +
state + infra; importing flows would chain-load most of the package
even when only one workflow is needed.
"""
