"""Cluster-side execution models — domain logic that runs ON the cluster.

This tier holds the execution machinery deployed to and run on the
cluster, as opposed to the laptop-side orchestration in ``ops`` / ``meta``
/ ``infra``. ``mapreduce`` is the first such model — the array-dispatch +
per-wave combine + reduce pipeline every submission is shaped as. Other
well-known compute paradigms (MPI / multi-rank — #293; many-tiny-task
meta-scheduling — #227) join here as siblings, which is why this is a
namespace and not a single ``mapreduce`` package.

Boundary contract: code under ``<model>/templates/`` is deployed to the
cluster and MUST NOT import the framework package (see
``docs/reference/boundary-contract.md``); the "no heavy top-level
imports" lint special-cases those template dirs.
"""
