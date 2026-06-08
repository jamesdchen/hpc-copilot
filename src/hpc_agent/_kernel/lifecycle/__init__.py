"""hpc_agent._kernel.lifecycle — delegated-worker & raw-model-call execution.

The funnel and transports for getting a request to a model: ``run``
(``run_workflow``), ``invoke`` (the spawned-worker ``WorkerInvoker``),
``structured`` + ``chat_models`` (the raw model-call boundary), and the
``playbook`` reader. The cross-cutting status/category vocabularies that
once lived here moved to ``hpc_agent._kernel.contract.vocabulary``.
"""
