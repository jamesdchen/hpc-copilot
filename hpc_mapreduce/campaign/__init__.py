"""Closed-loop campaign primitive: stateful iteration of ``.hpc/tasks.py``.

A campaign is a sequence of ``/submit`` invocations that share a
``campaign_id`` tag. The user's ``tasks.py`` reads
:func:`hpc_mapreduce.reduce.history.prior` to learn what prior iterations
of the same campaign produced and decides what to run next.

The framework's surface area is intentionally small:

* the ``HPC_CAMPAIGN_ID`` env var threaded through scheduler templates;
* the per-run sidecar's ``campaign_id`` field (W1.1);
* :func:`hpc_mapreduce.reduce.history.prior` for reading per-iteration
  reduced metrics back (W2.1);
* :func:`hpc_mapreduce.campaign.loop.run_campaign` — the asyncio
  in-flight queue that maintains *concurrency* live submits and stops
  when the user's ``tasks.py`` returns ``total() == 0``.

Strategies (Optuna, RandomSearch, walk-forward, PBT, …) are not
framework citizens: they live as Python libraries the user imports
inside their own ``tasks.py``.
"""

from __future__ import annotations

from hpc_mapreduce.campaign import defaults
from hpc_mapreduce.campaign.dirs import campaign_dir
from hpc_mapreduce.campaign.loop import CampaignResult, run_campaign

__all__ = [
    "CampaignResult",
    "campaign_dir",
    "defaults",
    "run_campaign",
]
