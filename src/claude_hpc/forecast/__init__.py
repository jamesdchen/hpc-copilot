"""claude_hpc.forecast — predictive scheduling primitives.

Submodules are deliberately importer-explicit (no eager re-exports).
The forecast package shares load-time edges with ``planning/`` and
``infra/clusters.py``; eager re-exports here create circular imports
on first ``import claude_hpc``. Reach for the specific submodule:

* :mod:`claude_hpc.forecast.queue_wait_baseline` — diurnal MA predictor.
* :mod:`claude_hpc.forecast.predict_start` — DES floors + LGBM residual.
* :mod:`claude_hpc.forecast.best_submit_window` — best-window primitive.
* :mod:`claude_hpc.forecast.backfill` — sbatch --test-only lattice probe.
* :mod:`claude_hpc.forecast.calibration` — walltime-drift / house-edge.
"""
