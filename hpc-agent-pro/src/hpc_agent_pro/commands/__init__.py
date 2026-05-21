"""CLI-command-layer primitive wrappers contributed by hpc-agent-pro.

These modules re-decorate plain compute functions that remain in the
public ``hpc-agent`` package (``inspect-cluster``, ``read-runtime-prior``)
with ``@primitive`` so the plugin owns their registry entry.
"""
