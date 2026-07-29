"""hpc_agent.ops.queue — the run-queue subject (Phase 1 of the run queue).

The two decisions the fleet was missing: *what runs next* (a durable arrival
ledger) and *where it runs* (placement). See
``docs/plans/run-queue-placement-2026-07-28.md`` §1–§3, §6.

* :mod:`hpc_agent.ops.queue.run` — ``queue-run`` (mutate, UNGATED:
  enqueueing spends nothing; gates bind at the cluster boundary later).
* :mod:`hpc_agent.ops.queue.status` — ``queue-status`` (query): intake
  items joined to PROJECTIONS over the existing run stores.
* :mod:`hpc_agent.ops.queue.advance` — ``queue-advance`` (query): pure
  placement authority — reads only, returns a decision, writes nothing.
* :mod:`hpc_agent.ops.queue.dispatch` — ``queue-dispatch`` (workflow, Phase 2):
  the ACTOR — consumes the placement decision and starts each item's normal
  lifecycle. The queue's only write authority.
* :mod:`hpc_agent.ops.queue.maintenance` — the JANITOR the actor runs on a
  dispatching tick (§8 S12): compact the ledger, prune unreferenced terminals.
* :mod:`hpc_agent.ops.queue.chain` — CHAIN-DISPATCH, the WAKE EDGE (§5): a
  retiring run's own driver ticks the actor once. Not a verb — a hook the two
  driver terminal seats call, fire-and-forget.

Leaf module names follow the ``ops/clusters/{list,describe}.py`` precedent
(subject in the directory, leaf in the filename); the verb names stay the
ungrouped hyphenated ``queue-*``, so no ``CliShape(group=...)`` is involved.

The substrate is deliberately outside this subject: the intake store
(:mod:`hpc_agent.state.queue_intake`) and the shared occupies-a-slot predicate
(:mod:`hpc_agent.state.queue_occupancy`) live under ``state/`` because
``campaign-advance`` must import the latter too, and a subject cannot import
another subject.

Eager re-exports are avoided so importing one queue verb doesn't pull its
siblings into the registry registration path.
"""
