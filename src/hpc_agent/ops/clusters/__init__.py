"""hpc_agent.ops.clusters — cluster-config query subject.

Both members are pure-dispatch primitives reading ``clusters.yaml`` and
projecting it to the operations-envelope shape.

* :mod:`hpc_agent.ops.clusters.list` — ``clusters-list``.
* :mod:`hpc_agent.ops.clusters.describe` — ``clusters-describe``.

Eager re-exports are avoided so importing one cluster query doesn't
pull the other into the registry registration path.
"""
