"""Closed-loop campaign primitive: tagged sequence of ``/submit`` invocations.

A campaign is a sequence of submits sharing a ``campaign_id`` tag. The
user's ``tasks.py`` reads :func:`hpc_mapreduce.reduce.history.prior` to
learn what prior iterations of the same campaign produced and decides
what to run next.

The framework's surface is intentionally tiny:

* ``HPC_CAMPAIGN_ID`` env var threaded through scheduler templates;
* the per-run sidecar's ``campaign_id`` field;
* :func:`hpc_mapreduce.reduce.history.prior` for reading per-iteration
  reduced metrics back;
* :func:`campaign_dir` for strategy libraries that want to drop their
  state files (Optuna SQLite, PBT checkpoints, etc.) under a canonical
  per-campaign path.

The "loop" itself is just repeated ``/submit-hpc campaign_id=<slug>``
invocations from the slash-command surface — there is no asyncio
driver, no ``run_campaign`` callable. Concurrency is opt-in by firing
more submits before earlier ones finish; the cluster scheduler runs
them in parallel. Strategies (Optuna, RandomSearch, walk-forward,
PBT, …) live as Python libraries the user imports inside their own
``tasks.py``.
"""

from __future__ import annotations

from hpc_mapreduce.campaign.dirs import campaign_dir

__all__ = ["campaign_dir"]
