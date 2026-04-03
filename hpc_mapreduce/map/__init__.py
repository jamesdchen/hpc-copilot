"""Map-phase modules: execution context and task dispatch."""

from hpc_mapreduce.map.context import MapContext, collect_outputs, map_context

__all__ = ["MapContext", "map_context", "collect_outputs"]
