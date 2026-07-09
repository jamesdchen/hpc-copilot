"""hpc_agent._kernel.lifecycle — workflow drivers & raw-model-call execution.

The code-driven sequencers (``block_drive``, ``drive``, ``detached``),
``structured`` + ``chat_models`` (the raw model-call boundary), and the
``playbook`` reader. The spawned-worker transport (``run``/``invoke``) was
deleted in the §6 worker removal (``docs/design/history/proving-run-2-hardening.md``
Move 3). The cross-cutting status/category vocabularies that once lived here
moved to ``hpc_agent._kernel.contract.vocabulary``.
"""
