"""hpc_agent_pro.forecast — predictive scheduling primitives.

Submodules are deliberately importer-explicit (no eager re-exports).
The forecast package shares load-time edges with ``planning/`` and
``infra/clusters.py``; eager re-exports here create circular imports
on first ``import hpc_agent``. Reach for the specific submodule:

* :mod:`hpc_agent_pro.forecast.queue_wait_baseline` — diurnal MA predictor.
* :mod:`hpc_agent_pro.forecast.predict_start` — DES floors + LGBM residual.
* :mod:`hpc_agent_pro.forecast.best_submit_window` — best-window primitive.
* :mod:`hpc_agent_pro.forecast.backfill` — sbatch --test-only lattice probe.
* :mod:`hpc_agent_pro.forecast.calibration` — walltime-drift / house-edge.
"""
