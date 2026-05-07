"""claude_hpc.flows — multi-atom workflows (submit, monitor, aggregate, ...).

Submodules are deliberately importer-explicit. Each workflow lives in
its own submodule and registers as a ``@primitive`` with
``verb='workflow'``. Reach for the specific entry point:

* :mod:`claude_hpc.flows.submit_flow`
* :mod:`claude_hpc.flows.monitor_flow`
* :mod:`claude_hpc.flows.aggregate_flow`
* :mod:`claude_hpc.flows.resubmit_flow`
* :mod:`claude_hpc.flows.validate_campaign`

Eager re-exports here are tempting but every workflow imports atoms +
state + infra; importing flows would chain-load most of the package
even when only one workflow is needed.
"""
