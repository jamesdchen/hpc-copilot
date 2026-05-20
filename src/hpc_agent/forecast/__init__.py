"""hpc_agent.forecast — predictive scheduling primitives.

Submodules are deliberately importer-explicit (no eager re-exports).
The forecast package shares load-time edges with ``planning/`` and
``infra/clusters.py``; eager re-exports here create circular imports
on first ``import hpc_agent``. Reach for the specific submodule:

* :mod:`hpc_agent.forecast.queue_wait_baseline` — diurnal MA predictor.
* :mod:`hpc_agent.forecast.predict_start` — DES floors + LGBM residual.
* :mod:`hpc_agent.forecast.best_submit_window` — best-window primitive.
* :mod:`hpc_agent.forecast.backfill` — sbatch --test-only lattice probe.
* :mod:`hpc_agent.forecast.calibration` — walltime-drift / house-edge.
"""
