"""hpc-agent-pro — scheduling-strategy / forecasting plugin for hpc-agent.

This out-of-tree plugin distribution contributes the predictive
scheduling primitives (queue-wait / start-time forecasting, submit
planning, walltime calibration) that the public ``hpc-agent`` package
deliberately ships without. It plugs in through the ``hpc_agent.plugins``
setuptools entry-point group; installing this distribution is the
entire opt-in.

The plugin object lives in :mod:`hpc_agent_pro.plugin`.
"""

from __future__ import annotations

__version__ = "0.1.0"
